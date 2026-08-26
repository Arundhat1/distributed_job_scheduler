from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from .models import ExecutionStatus, JobStatus, JobType, LogLevel, OrgRole, RetryStrategy, WorkerStatus

T = TypeVar("T")


# ---------- generic pagination envelope ----------

class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int
    pages: int


# ---------- auth ----------

class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str
    organization_name: str


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    full_name: str
    created_at: datetime


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ---------- projects ----------

class ProjectCreate(BaseModel):
    name: str
    description: str | None = None


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    organization_id: int
    name: str
    description: str | None
    created_at: datetime


# ---------- retry policy ----------

class RetryPolicyCreate(BaseModel):
    name: str
    strategy: RetryStrategy = RetryStrategy.EXPONENTIAL
    base_delay_seconds: int = Field(default=5, ge=0)
    multiplier: float = Field(default=2.0, ge=1.0)
    max_delay_seconds: int = Field(default=3600, ge=1)
    max_retries: int = Field(default=3, ge=0)


class RetryPolicyOut(RetryPolicyCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int


# ---------- queues ----------

class QueueCreate(BaseModel):
    name: str
    priority: int = 0
    max_concurrency: int = Field(default=5, ge=1)
    retry_policy: RetryPolicyCreate | None = None


class QueueUpdate(BaseModel):
    priority: int | None = None
    max_concurrency: int | None = Field(default=None, ge=1)
    is_paused: bool | None = None


class QueueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int
    name: str
    priority: int
    max_concurrency: int
    is_paused: bool
    created_at: datetime
    updated_at: datetime


class QueueStats(BaseModel):
    queue_id: int
    queued: int
    scheduled: int
    running: int
    completed: int
    failed: int
    dead_letter: int
    avg_duration_ms: float | None
    throughput_last_hour: int


# ---------- jobs ----------

class JobCreate(BaseModel):
    name: str
    job_type: JobType = JobType.IMMEDIATE
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
    run_at: datetime | None = None          # required for delayed/scheduled
    cron_expression: str | None = None      # required for recurring
    timeout_seconds: int = 300
    max_retries: int = 3
    idempotency_key: str | None = None
    batch_id: int | None = None


class BatchJobCreate(BaseModel):
    batch_name: str
    jobs: list[JobCreate] = Field(min_length=1)


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    queue_id: int
    name: str
    job_type: JobType
    status: JobStatus
    payload: dict[str, Any]
    priority: int
    run_at: datetime
    attempt_count: int
    max_retries: int
    claimed_by_worker_id: int | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class JobExecutionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    job_id: int
    worker_id: int | None
    attempt_number: int
    status: ExecutionStatus
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    error_message: str | None
    next_retry_at: datetime | None


class JobLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    level: LogLevel
    message: str
    created_at: datetime


class JobDetailOut(JobOut):
    executions: list[JobExecutionOut] = []


# ---------- workers ----------

class WorkerRegister(BaseModel):
    name: str
    hostname: str
    pid: int
    queues: list[str]
    concurrency_limit: int = 5


class WorkerHeartbeatIn(BaseModel):
    status: WorkerStatus
    active_jobs: int
    cpu_percent: float | None = None
    memory_mb: float | None = None


class WorkerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    hostname: str
    status: WorkerStatus
    queues: str
    concurrency_limit: int
    current_job_count: int
    registered_at: datetime
    last_heartbeat_at: datetime | None


class ClaimRequest(BaseModel):
    worker_id: int
    queue_names: list[str]
    max_jobs: int = 1


class ExecutionResultIn(BaseModel):
    status: ExecutionStatus
    result: dict[str, Any] | None = None
    error_message: str | None = None
    error_stacktrace: str | None = None


# ---------- scheduled jobs (cron) ----------

class ScheduledJobCreate(BaseModel):
    name: str
    cron_expression: str
    payload: dict[str, Any] = Field(default_factory=dict)
    timezone: str = "UTC"


class ScheduledJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    queue_id: int
    name: str
    cron_expression: str
    is_active: bool
    next_run_at: datetime
    last_run_at: datetime | None


# ---------- dead letter queue ----------

class DeadLetterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    job_id: int
    queue_id: int
    final_error: str
    attempt_count: int
    created_at: datetime
    requeued_at: datetime | None


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
    code: str | None = None