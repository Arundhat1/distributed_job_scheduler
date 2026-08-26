from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz import get_project_or_403, get_queue_or_403
from app.database import get_db
from app.deps import get_current_user
from app.models import Job, JobExecution, JobStatus, Queue, RetryPolicy, User
from app.schemas import QueueCreate, QueueOut, QueueStats, QueueUpdate
from app.websocket_manager import ws_manager

router = APIRouter(prefix="/api/v1/projects/{project_id}/queues", tags=["queues"])


@router.get("", response_model=list[QueueOut])
async def list_queues(project_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    await get_project_or_403(db, user, project_id)
    return (await db.execute(select(Queue).where(Queue.project_id == project_id))).scalars().all()


@router.post("", response_model=QueueOut, status_code=201)
async def create_queue(project_id: int, data: QueueCreate, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    await get_project_or_403(db, user, project_id, require_write=True)

    retry_policy_id = None
    if data.retry_policy:
        rp = RetryPolicy(**data.retry_policy.model_dump())
        db.add(rp)
        await db.flush()
        retry_policy_id = rp.id

    queue = Queue(
        project_id=project_id,
        name=data.name,
        priority=data.priority,
        max_concurrency=data.max_concurrency,
        default_retry_policy_id=retry_policy_id,
    )
    db.add(queue)
    await db.commit()
    await db.refresh(queue)
    return queue


@router.patch("/{queue_id}", response_model=QueueOut)
async def update_queue(project_id: int, queue_id: int, data: QueueUpdate, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    queue = await get_queue_or_403(db, user, queue_id, require_write=True)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(queue, field, value)
    await db.commit()
    await db.refresh(queue)
    return queue


@router.post("/{queue_id}/pause", response_model=QueueOut)
async def pause_queue(project_id: int, queue_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    queue = await get_queue_or_403(db, user, queue_id, require_write=True)
    queue.is_paused = True
    await db.commit()
    await db.refresh(queue)
    await ws_manager.broadcast("queue_paused", {"queue_id": queue.id, "queue_name": queue.name})
    return queue


@router.post("/{queue_id}/resume", response_model=QueueOut)
async def resume_queue(project_id: int, queue_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    queue = await get_queue_or_403(db, user, queue_id, require_write=True)
    queue.is_paused = False
    await db.commit()
    await db.refresh(queue)
    await ws_manager.broadcast("queue_resumed", {"queue_id": queue.id, "queue_name": queue.name})
    return queue


@router.get("/{queue_id}/stats", response_model=QueueStats)
async def queue_stats(project_id: int, queue_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    await get_queue_or_403(db, user, queue_id)

    counts_by_status = dict(
        (
            await db.execute(
                select(Job.status, func.count(Job.id)).where(Job.queue_id == queue_id).group_by(Job.status)
            )
        ).all()
    )

    avg_duration = (
        await db.execute(
            select(func.avg(JobExecution.duration_ms))
            .join(Job, Job.id == JobExecution.job_id)
            .where(Job.queue_id == queue_id, JobExecution.duration_ms.is_not(None))
        )
    ).scalar()

    since = datetime.now(timezone.utc) - timedelta(hours=1)
    throughput = (
        await db.execute(
            select(func.count(Job.id)).where(
                Job.queue_id == queue_id, Job.status == JobStatus.COMPLETED, Job.completed_at >= since
            )
        )
    ).scalar_one()

    return QueueStats(
        queue_id=queue_id,
        queued=counts_by_status.get(JobStatus.QUEUED, 0),
        scheduled=counts_by_status.get(JobStatus.SCHEDULED, 0),
        running=counts_by_status.get(JobStatus.RUNNING, 0),
        completed=counts_by_status.get(JobStatus.COMPLETED, 0),
        failed=counts_by_status.get(JobStatus.FAILED, 0),
        dead_letter=counts_by_status.get(JobStatus.DEAD_LETTER, 0),
        avg_duration_ms=float(avg_duration) if avg_duration is not None else None,
        throughput_last_hour=throughput,
    )