"""
Job lifecycle state machine and the atomic claim algorithm.

    QUEUED/SCHEDULED --(scheduler promotes when run_at<=now)--> QUEUED
    QUEUED --(worker claims)--> CLAIMED --(worker starts)--> RUNNING
    RUNNING --success--> COMPLETED
    RUNNING --failure, attempts remaining--> RETRY_SCHEDULED --(scheduler promotes)--> QUEUED
    RUNNING --failure, attempts exhausted--> DEAD_LETTER (+ DeadLetterEntry row)

Atomic claiming
----------------
Two workers must never run the same job. We guarantee this with
PostgreSQL's `SELECT ... FOR UPDATE SKIP LOCKED`:

  1. Row-lock up to N candidate QUEUED jobs for a queue (ordered by queue
     priority, then job priority, then run_at — oldest/highest priority
     first). SKIP LOCKED means a second worker's concurrent SELECT simply
     skips rows already locked by the first worker instead of blocking,
     so throughput scales with worker count instead of serializing on a
     single mutex.
  2. While still holding the lock, UPDATE those exact rows to CLAIMED and
     stamp claimed_by_worker_id/claimed_at, then COMMIT. The lock is only
     held for the duration of this tiny transaction, not the job's
     execution time.

Per-queue concurrency (`max_concurrency`) is enforced by first counting
jobs already in CLAIMED/RUNNING for that queue and only claiming up to
the remaining slots — this keeps a single busy queue from starving
others when a worker polls multiple queues.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DeadLetterEntry,
    ExecutionStatus,
    Job,
    JobExecution,
    JobStatus,
    Queue,
    RetryPolicy,
    RetryStrategy,
)
from app.schemas import ExecutionResultIn, JobCreate
from app.services.retry import compute_delay_seconds


def _initial_status(run_at: datetime | None) -> JobStatus:
    now = datetime.now(timezone.utc)
    if run_at and run_at > now:
        return JobStatus.SCHEDULED
    return JobStatus.QUEUED


async def create_job(db: AsyncSession, queue: Queue, data: JobCreate) -> Job:
    run_at = data.run_at or datetime.now(timezone.utc)
    job = Job(
        queue_id=queue.id,
        batch_id=data.batch_id,
        retry_policy_id=queue.default_retry_policy_id,
        name=data.name,
        job_type=data.job_type,
        payload=data.payload,
        priority=data.priority,
        run_at=run_at,
        timeout_seconds=data.timeout_seconds,
        max_retries=data.max_retries,
        idempotency_key=data.idempotency_key,
        status=_initial_status(run_at),
    )
    db.add(job)
    await db.flush()
    return job


async def _remaining_capacity(db: AsyncSession, queue_id: int, max_concurrency: int) -> int:
    in_flight = (
        await db.execute(
            select(func.count(Job.id)).where(
                Job.queue_id == queue_id,
                Job.status.in_([JobStatus.CLAIMED, JobStatus.RUNNING]),
            )
        )
    ).scalar_one()
    return max(0, max_concurrency - in_flight)


async def claim_jobs(db: AsyncSession, worker_id: int, queue_names: list[str], max_jobs: int) -> list[Job]:
    """Atomically claim up to `max_jobs` across the given queues for this worker."""
    queues = (
        await db.execute(
            select(Queue).where(Queue.name.in_(queue_names), Queue.is_paused.is_(False))
        )
    ).scalars().all()
    # Highest priority queue first so a high-priority queue never starves behind a low one.
    queues.sort(key=lambda q: q.priority, reverse=True)

    claimed: list[Job] = []
    remaining_budget = max_jobs

    for queue in queues:
        if remaining_budget <= 0:
            break
        capacity = await _remaining_capacity(db, queue.id, queue.max_concurrency)
        take = min(capacity, remaining_budget)
        if take <= 0:
            continue

        now = datetime.now(timezone.utc)
        candidates = (
            await db.execute(
                select(Job)
                .where(Job.queue_id == queue.id, Job.status == JobStatus.QUEUED, Job.run_at <= now)
                .order_by(Job.priority.desc(), Job.run_at.asc())
                .limit(take)
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()

        for job in candidates:
            job.status = JobStatus.CLAIMED
            job.claimed_by_worker_id = worker_id
            job.claimed_at = now
            claimed.append(job)

        remaining_budget -= len(candidates)

    if claimed:
        await db.commit()
        for job in claimed:
            await db.refresh(job)
    return claimed


async def start_execution(db: AsyncSession, job: Job, worker_id: int) -> JobExecution:
    job.status = JobStatus.RUNNING
    job.attempt_count += 1
    execution = JobExecution(
        job_id=job.id,
        worker_id=worker_id,
        attempt_number=job.attempt_count,
        status=ExecutionStatus.RUNNING,
    )
    db.add(execution)
    await db.commit()
    await db.refresh(execution)
    return execution


async def record_result(db: AsyncSession, job: Job, execution: JobExecution, result: ExecutionResultIn) -> Job:
    """Apply a worker-reported execution outcome and drive the job's next state."""
    now = datetime.now(timezone.utc)
    execution.status = result.status
    execution.finished_at = now
    execution.result = result.result
    execution.error_message = result.error_message
    execution.error_stacktrace = result.error_stacktrace
    if execution.started_at:
        started = execution.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        execution.duration_ms = int((now - started).total_seconds() * 1000)

    if result.status == ExecutionStatus.SUCCEEDED:
        job.status = JobStatus.COMPLETED
        job.completed_at = now
        await db.commit()
        return job

    # failure or timeout -> retry or dead-letter
    policy = None
    if job.retry_policy_id:
        policy = (await db.execute(select(RetryPolicy).where(RetryPolicy.id == job.retry_policy_id))).scalar_one_or_none()

    max_retries = policy.max_retries if policy else job.max_retries
    if job.attempt_count <= max_retries:
        strategy = policy.strategy if policy else RetryStrategy.EXPONENTIAL
        base_delay = policy.base_delay_seconds if policy else 5
        multiplier = policy.multiplier if policy else 2.0
        max_delay = policy.max_delay_seconds if policy else 3600

        delay = compute_delay_seconds(strategy, job.attempt_count, base_delay, multiplier, max_delay)
        next_run = now + timedelta(seconds=delay)

        job.status = JobStatus.RETRY_SCHEDULED
        job.run_at = next_run
        execution.next_retry_at = next_run
        await db.commit()
        return job

    # exhausted retries -> dead letter
    job.status = JobStatus.DEAD_LETTER
    db.add(
        DeadLetterEntry(
            job_id=job.id,
            queue_id=job.queue_id,
            payload_snapshot=job.payload,
            final_error=result.error_message or "Unknown error",
            attempt_count=job.attempt_count,
        )
    )
    await db.commit()
    return job


async def requeue_dead_letter(db: AsyncSession, entry: DeadLetterEntry) -> Job:
    """Operator-triggered replay of a permanently failed job as a fresh job."""
    original = (await db.execute(select(Job).where(Job.id == entry.job_id))).scalar_one()
    new_job = Job(
        queue_id=original.queue_id,
        retry_policy_id=original.retry_policy_id,
        name=f"{original.name} (retry)",
        job_type=original.job_type,
        payload=entry.payload_snapshot,
        priority=original.priority,
        run_at=datetime.now(timezone.utc),
        timeout_seconds=original.timeout_seconds,
        max_retries=original.max_retries,
        status=JobStatus.QUEUED,
    )
    db.add(new_job)
    entry.requeued_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(new_job)
    return new_job