"""
Database schema (SQLAlchemy 2.0 declarative models).

Design notes (see docs/ER_DIAGRAM.md and docs/DESIGN_DECISIONS.md for the
full rationale) summarized inline next to each table:

  * Every table uses a surrogate BIGINT identity primary key. Natural keys
    (email, queue name+project, idempotency_key) get separate UNIQUE
    constraints instead of being used as PKs, so FK columns stay narrow
    and stable even if a business key changes.
  * Ownership chain Organization -> Project -> Queue -> Job cascades on
    delete, because child rows have no meaning without their parent.
    Job -> JobExecution -> JobLog also cascades (execution history is
    only ever viewed through its job). Worker -> WorkerHeartbeat cascades
    for the same reason.
  * DeadLetterEntry does NOT cascade-delete when a Job is removed in the
    normal lifecycle; it is written once a job is permanently failed and
    is treated as an audit record independent of the job's own retention.
  * The (queue_id, status, priority, run_at) composite index on jobs is
    the single most important index in the schema: it is exactly the
    predicate the worker's claim query filters and orders by.
"""
import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------

class OrgRole(str, enum.Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class JobType(str, enum.Enum):
    IMMEDIATE = "immediate"
    DELAYED = "delayed"
    SCHEDULED = "scheduled"   # run once at a specific future timestamp
    RECURRING = "recurring"   # spawned from a ScheduledJob cron definition
    BATCH = "batch"           # part of a Batch group


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    SCHEDULED = "scheduled"       # waiting for run_at (delayed/scheduled jobs)
    CLAIMED = "claimed"           # atomically locked by a worker, not yet running
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"             # attempt failed, may still be retried
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTER = "dead_letter"   # permanently failed, moved to DLQ
    CANCELLED = "cancelled"


class ExecutionStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class RetryStrategy(str, enum.Enum):
    FIXED = "fixed"
    LINEAR = "linear"
    EXPONENTIAL = "exponential"


class WorkerStatus(str, enum.Enum):
    IDLE = "idle"
    BUSY = "busy"
    DRAINING = "draining"   # graceful shutdown in progress
    DEAD = "dead"           # heartbeat missed past stale threshold


class LogLevel(str, enum.Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


# --------------------------------------------------------------------------
# Identity / tenancy
# --------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    memberships: Mapped[list["OrgMembership"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    memberships: Mapped[list["OrgMembership"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    projects: Mapped[list["Project"]] = relationship(back_populates="organization", cascade="all, delete-orphan")


class OrgMembership(Base):
    """Join table giving a User a role within an Organization (RBAC)."""
    __tablename__ = "org_memberships"
    __table_args__ = (UniqueConstraint("user_id", "organization_id", name="uq_membership_user_org"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    role: Mapped[OrgRole] = mapped_column(Enum(OrgRole), default=OrgRole.MEMBER, nullable=False)

    user: Mapped["User"] = relationship(back_populates="memberships")
    organization: Mapped["Organization"] = relationship(back_populates="memberships")


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_project_org_name"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    organization: Mapped["Organization"] = relationship(back_populates="projects")
    queues: Mapped[list["Queue"]] = relationship(back_populates="project", cascade="all, delete-orphan")


# --------------------------------------------------------------------------
# Queue configuration
# --------------------------------------------------------------------------

class RetryPolicy(Base):
    """
    Reusable retry configuration. Referenced by Queue (as a default) and
    optionally overridden per-Job. Kept as its own table (rather than
    inline columns on Queue/Job) so the same policy can be shared and
    audited independently of the entities that use it.
    """
    __tablename__ = "retry_policies"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    strategy: Mapped[RetryStrategy] = mapped_column(Enum(RetryStrategy), default=RetryStrategy.EXPONENTIAL)
    base_delay_seconds: Mapped[int] = mapped_column(Integer, default=5)
    multiplier: Mapped[float] = mapped_column(Float, default=2.0)   # used by exponential/linear
    max_delay_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)

    __table_args__ = (CheckConstraint("max_retries >= 0", name="ck_retry_max_retries_nonneg"),)


class Queue(Base):
    __tablename__ = "queues"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_queue_project_name"),
        Index("ix_queue_project_paused", "project_id", "is_paused"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0)          # higher = served first
    max_concurrency: Mapped[int] = mapped_column(Integer, default=5)   # max jobs running at once for this queue
    is_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    default_retry_policy_id: Mapped[int | None] = mapped_column(ForeignKey("retry_policies.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    project: Mapped["Project"] = relationship(back_populates="queues")
    default_retry_policy: Mapped["RetryPolicy | None"] = relationship()
    jobs: Mapped[list["Job"]] = relationship(back_populates="queue", cascade="all, delete-orphan")


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------

class Batch(Base):
    """Groups a set of jobs submitted together (batch job type)."""
    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    queue_id: Mapped[int] = mapped_column(ForeignKey("queues.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    total_jobs: Mapped[int] = mapped_column(Integer, default=0)
    completed_jobs: Mapped[int] = mapped_column(Integer, default=0)
    failed_jobs: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Job(Base):
    """
    A single unit of work. Recurring jobs are represented by a
    ScheduledJob "template" row that periodically inserts new Job rows
    (job_type=RECURRING) — the Job table itself never has to represent
    "infinite" recurrence, which keeps the claim query simple.
    """
    __tablename__ = "jobs"
    __table_args__ = (
        # This is the index the worker's claim query hits on every poll.
        Index("ix_jobs_claim", "queue_id", "status", "priority", "run_at"),
        Index("ix_jobs_batch", "batch_id"),
        UniqueConstraint("queue_id", "idempotency_key", name="uq_job_queue_idempotency"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    queue_id: Mapped[int] = mapped_column(ForeignKey("queues.id", ondelete="CASCADE"), index=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id", ondelete="SET NULL"), nullable=True)
    scheduled_job_id: Mapped[int | None] = mapped_column(ForeignKey("scheduled_jobs.id", ondelete="SET NULL"), nullable=True)
    retry_policy_id: Mapped[int | None] = mapped_column(ForeignKey("retry_policies.id", ondelete="SET NULL"), nullable=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    job_type: Mapped[JobType] = mapped_column(Enum(JobType), default=JobType.IMMEDIATE)
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.QUEUED, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)

    priority: Mapped[int] = mapped_column(Integer, default=0)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=300)

    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)

    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)

    claimed_by_worker_id: Mapped[int | None] = mapped_column(ForeignKey("workers.id", ondelete="SET NULL"), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    queue: Mapped["Queue"] = relationship(back_populates="jobs")
    retry_policy: Mapped["RetryPolicy | None"] = relationship()
    executions: Mapped[list["JobExecution"]] = relationship(back_populates="job", cascade="all, delete-orphan", order_by="JobExecution.attempt_number")


class ScheduledJob(Base):
    """
    Cron "template" that the scheduler service evaluates on every tick.
    When croniter says next_run_at has passed, the scheduler inserts a
    new Job row (job_type=RECURRING) referencing this template and
    advances next_run_at. Kept separate from Job so pausing/editing a
    recurring definition never has to touch historical Job rows.
    """
    __tablename__ = "scheduled_jobs"
    __table_args__ = (Index("ix_scheduled_jobs_next_run", "is_active", "next_run_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    queue_id: Mapped[int] = mapped_column(ForeignKey("queues.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    cron_expression: Mapped[str] = mapped_column(String(120), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    queue: Mapped["Queue"] = relationship()


# --------------------------------------------------------------------------
# Execution / observability
# --------------------------------------------------------------------------

class Worker(Base):
    __tablename__ = "workers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    queues: Mapped[str] = mapped_column(String(500))  # comma-separated queue names this worker polls
    concurrency_limit: Mapped[int] = mapped_column(Integer, default=5)
    status: Mapped[WorkerStatus] = mapped_column(Enum(WorkerStatus), default=WorkerStatus.IDLE, index=True)
    current_job_count: Mapped[int] = mapped_column(Integer, default=0)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    heartbeats: Mapped[list["WorkerHeartbeat"]] = relationship(back_populates="worker", cascade="all, delete-orphan")


class WorkerHeartbeat(Base):
    """
    Time-series row appended on every heartbeat tick. Kept append-only
    and separate from Worker (which stores only the latest snapshot) so
    the dashboard can chart worker health over time without losing
    history; a periodic housekeeping job would prune rows older than a
    retention window in a real deployment.
    """
    __tablename__ = "worker_heartbeats"
    __table_args__ = (Index("ix_heartbeat_worker_time", "worker_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    worker_id: Mapped[int] = mapped_column(ForeignKey("workers.id", ondelete="CASCADE"), index=True)
    status: Mapped[WorkerStatus] = mapped_column(Enum(WorkerStatus))
    active_jobs: Mapped[int] = mapped_column(Integer, default=0)
    cpu_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    memory_mb: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    worker: Mapped["Worker"] = relationship(back_populates="heartbeats")


class JobExecution(Base):
    """One row per attempt of a Job. Retry history = all rows for a job_id."""
    __tablename__ = "job_executions"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number", name="uq_execution_job_attempt"),
        Index("ix_execution_job", "job_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    worker_id: Mapped[int | None] = mapped_column(ForeignKey("workers.id", ondelete="SET NULL"), nullable=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[ExecutionStatus] = mapped_column(Enum(ExecutionStatus), default=ExecutionStatus.RUNNING)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_stacktrace: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    job: Mapped["Job"] = relationship(back_populates="executions")
    logs: Mapped[list["JobLog"]] = relationship(back_populates="execution", cascade="all, delete-orphan")


class JobLog(Base):
    """Structured log lines emitted during a single execution attempt."""
    __tablename__ = "job_logs"
    __table_args__ = (Index("ix_joblog_execution_time", "job_execution_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_execution_id: Mapped[int] = mapped_column(ForeignKey("job_executions.id", ondelete="CASCADE"), index=True)
    level: Mapped[LogLevel] = mapped_column(Enum(LogLevel), default=LogLevel.INFO)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    execution: Mapped["JobExecution"] = relationship(back_populates="logs")


class DeadLetterEntry(Base):
    """
    Written once a Job exhausts max_retries. Stores a payload snapshot
    so the DLQ remains inspectable/replayable even if the source Job
    row is later purged by a retention job.
    """
    __tablename__ = "dead_letter_entries"
    __table_args__ = (Index("ix_dlq_queue_time", "queue_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="RESTRICT"), index=True)
    queue_id: Mapped[int] = mapped_column(ForeignKey("queues.id", ondelete="CASCADE"), index=True)
    payload_snapshot: Mapped[dict] = mapped_column(JSON)
    final_error: Mapped[str] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    requeued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)