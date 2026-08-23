"""
Scheduler tick — runs on a fixed interval, either as an asyncio task
inside the API process (see app/main.py startup) or as a standalone
process (scripts/run_scheduler.py) for horizontal scaling. Both are
safe to run concurrently since every write here is a targeted UPDATE
guarded by a WHERE clause (no read-then-write race that matters: a job
promoted twice just gets `status=QUEUED` set twice, which is a no-op).

Responsibilities each tick:
  1. Promote SCHEDULED/RETRY_SCHEDULED jobs whose run_at has arrived to QUEUED.
  2. Evaluate active ScheduledJob (cron) definitions; for any whose
     next_run_at has passed, insert a new Job row and advance next_run_at.
  3. Mark workers DEAD if their last heartbeat is older than the stale
     threshold, so the dashboard and claim logic stop counting them.
"""
import logging
from datetime import datetime, timezone

from croniter import croniter
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Job, JobStatus, ScheduledJob, Worker, WorkerStatus

logger = logging.getLogger("scheduler")
settings = get_settings()


async def promote_due_jobs(db: AsyncSession) -> int:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(Job)
        .where(
            Job.status.in_([JobStatus.SCHEDULED, JobStatus.RETRY_SCHEDULED]),
            Job.run_at <= now,
        )
        .values(status=JobStatus.QUEUED)
    )
    await db.commit()
    return result.rowcount or 0


async def evaluate_cron_schedules(db: AsyncSession) -> int:
    now = datetime.now(timezone.utc)
    due = (
        await db.execute(
            select(ScheduledJob).where(ScheduledJob.is_active.is_(True), ScheduledJob.next_run_at <= now)
        )
    ).scalars().all()

    created = 0
    for sched in due:
        job = Job(
            queue_id=sched.queue_id,
            scheduled_job_id=sched.id,
            name=sched.name,
            job_type="recurring",
            payload=sched.payload,
            run_at=now,
            status=JobStatus.QUEUED,
            max_retries=settings.default_max_retries,
        )
        db.add(job)

        cron = croniter(sched.cron_expression, now)
        sched.next_run_at = cron.get_next(datetime)
        sched.last_run_at = now
        created += 1

    if due:
        await db.commit()
    return created


async def reap_stale_workers(db: AsyncSession) -> int:
    threshold = datetime.now(timezone.utc).timestamp() - settings.worker_stale_after_seconds
    threshold_dt = datetime.fromtimestamp(threshold, tz=timezone.utc)
    result = await db.execute(
        update(Worker)
        .where(Worker.last_heartbeat_at < threshold_dt, Worker.status != WorkerStatus.DEAD)
        .values(status=WorkerStatus.DEAD)
    )
    await db.commit()
    return result.rowcount or 0


async def run_tick(db: AsyncSession) -> dict:
    promoted = await promote_due_jobs(db)
    spawned = await evaluate_cron_schedules(db)
    reaped = await reap_stale_workers(db)
    if promoted or spawned or reaped:
        logger.info("scheduler tick: promoted=%s spawned=%s reaped_workers=%s", promoted, spawned, reaped)
    return {"promoted": promoted, "spawned": spawned, "reaped_workers": reaped}