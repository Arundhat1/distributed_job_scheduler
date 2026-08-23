import math

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.authz import get_queue_or_403
from app.database import get_db
from app.deps import PaginationParams, get_current_user
from app.models import DeadLetterEntry, Job, JobStatus, ScheduledJob, User
from app.schemas import (
    BatchJobCreate,
    DeadLetterOut,
    JobCreate,
    JobDetailOut,
    JobOut,
    Page,
    ScheduledJobCreate,
    ScheduledJobOut,
)
from app.services.job_service import create_job

router = APIRouter(prefix="/api/v1/queues/{queue_id}/jobs", tags=["jobs"])
dlq_router = APIRouter(prefix="/api/v1/queues/{queue_id}/dead-letter", tags=["dead-letter-queue"])
cron_router = APIRouter(prefix="/api/v1/queues/{queue_id}/scheduled-jobs", tags=["recurring-jobs"])


@router.post("", response_model=JobOut, status_code=201)
async def submit_job(queue_id: int, data: JobCreate, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    queue = await get_queue_or_403(db, user, queue_id, require_write=True)

    if data.job_type == "recurring" and not data.cron_expression:
        raise HTTPException(422, "cron_expression is required for recurring jobs")
    if data.job_type in ("delayed", "scheduled") and not data.run_at:
        raise HTTPException(422, "run_at is required for delayed/scheduled jobs")

    if data.idempotency_key:
        existing = (
            await db.execute(
                select(Job).where(Job.queue_id == queue_id, Job.idempotency_key == data.idempotency_key)
            )
        ).scalar_one_or_none()
        if existing:
            return existing  # idempotent replay: return the already-accepted job, do not duplicate

    job = await create_job(db, queue, data)
    await db.commit()
    await db.refresh(job)
    return job


@router.post("/batch", response_model=list[JobOut], status_code=201)
async def submit_batch(queue_id: int, data: BatchJobCreate, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    from app.models import Batch

    queue = await get_queue_or_403(db, user, queue_id, require_write=True)
    batch = Batch(queue_id=queue_id, name=data.batch_name, total_jobs=len(data.jobs))
    db.add(batch)
    await db.flush()

    jobs = []
    for job_data in data.jobs:
        job_data.batch_id = batch.id
        job_data.job_type = "batch"
        jobs.append(await create_job(db, queue, job_data))
    await db.commit()
    for j in jobs:
        await db.refresh(j)
    return jobs


@router.get("", response_model=Page[JobOut])
async def list_jobs(
    queue_id: int,
    status_filter: JobStatus | None = Query(None, alias="status"),
    job_type: str | None = None,
    pagination: PaginationParams = Depends(),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await get_queue_or_403(db, user, queue_id)

    filters = [Job.queue_id == queue_id]
    if status_filter:
        filters.append(Job.status == status_filter)
    if job_type:
        filters.append(Job.job_type == job_type)

    total = (await db.execute(select(func.count(Job.id)).where(*filters))).scalar_one()
    items = (
        await db.execute(
            select(Job)
            .where(*filters)
            .order_by(Job.created_at.desc())
            .offset(pagination.offset)
            .limit(pagination.page_size)
        )
    ).scalars().all()

    return Page(
        items=items,
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        pages=math.ceil(total / pagination.page_size) if total else 0,
    )


@router.get("/{job_id}", response_model=JobDetailOut)
async def get_job(queue_id: int, job_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    await get_queue_or_403(db, user, queue_id)
    job = (
        await db.execute(
            select(Job).where(Job.id == job_id, Job.queue_id == queue_id).options(selectinload(Job.executions))
        )
    ).scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@router.post("/{job_id}/cancel", response_model=JobOut)
async def cancel_job(queue_id: int, job_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    await get_queue_or_403(db, user, queue_id, require_write=True)
    job = (await db.execute(select(Job).where(Job.id == job_id, Job.queue_id == queue_id))).scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status in (JobStatus.COMPLETED, JobStatus.DEAD_LETTER, JobStatus.CANCELLED):
        raise HTTPException(409, f"Cannot cancel a job in status {job.status}")
    job.status = JobStatus.CANCELLED
    await db.commit()
    await db.refresh(job)
    return job


# ---------------- Dead Letter Queue ----------------

@dlq_router.get("", response_model=list[DeadLetterOut])
async def list_dead_letters(queue_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    await get_queue_or_403(db, user, queue_id)
    return (
        await db.execute(select(DeadLetterEntry).where(DeadLetterEntry.queue_id == queue_id).order_by(DeadLetterEntry.created_at.desc()))
    ).scalars().all()


@dlq_router.post("/{entry_id}/requeue", response_model=JobOut)
async def requeue_dead_letter_entry(queue_id: int, entry_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    from app.services.job_service import requeue_dead_letter

    await get_queue_or_403(db, user, queue_id, require_write=True)
    entry = (
        await db.execute(select(DeadLetterEntry).where(DeadLetterEntry.id == entry_id, DeadLetterEntry.queue_id == queue_id))
    ).scalar_one_or_none()
    if not entry:
        raise HTTPException(404, "Dead letter entry not found")
    return await requeue_dead_letter(db, entry)


# ---------------- Recurring (cron) job definitions ----------------

@cron_router.post("", response_model=ScheduledJobOut, status_code=201)
async def create_scheduled_job(queue_id: int, data: ScheduledJobCreate, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    from croniter import croniter
    from datetime import datetime, timezone

    await get_queue_or_403(db, user, queue_id, require_write=True)
    if not croniter.is_valid(data.cron_expression):
        raise HTTPException(422, "Invalid cron expression")

    now = datetime.now(timezone.utc)
    next_run = croniter(data.cron_expression, now).get_next(datetime)
    sched = ScheduledJob(queue_id=queue_id, name=data.name, cron_expression=data.cron_expression, payload=data.payload, timezone=data.timezone, next_run_at=next_run)
    db.add(sched)
    await db.commit()
    await db.refresh(sched)
    return sched


@cron_router.get("", response_model=list[ScheduledJobOut])
async def list_scheduled_jobs(queue_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    await get_queue_or_403(db, user, queue_id)
    return (await db.execute(select(ScheduledJob).where(ScheduledJob.queue_id == queue_id))).scalars().all()


@cron_router.post("/{scheduled_job_id}/pause", response_model=ScheduledJobOut)
async def pause_scheduled_job(queue_id: int, scheduled_job_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    await get_queue_or_403(db, user, queue_id, require_write=True)
    sched = (await db.execute(select(ScheduledJob).where(ScheduledJob.id == scheduled_job_id, ScheduledJob.queue_id == queue_id))).scalar_one_or_none()
    if not sched:
        raise HTTPException(404, "Scheduled job not found")
    sched.is_active = False
    await db.commit()
    await db.refresh(sched)
    return sched