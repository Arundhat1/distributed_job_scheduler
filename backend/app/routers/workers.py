from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models import Job, JobExecution, User, Worker
from app.schemas import (
    ClaimRequest,
    ExecutionResultIn,
    JobOut,
    WorkerHeartbeatIn,
    WorkerOut,
    WorkerRegister,
)
from app.services.job_service import claim_jobs, record_result, start_execution
from app.websocket_manager import ws_manager

router = APIRouter(prefix="/api/v1/workers", tags=["workers"])


# STRIPPED User dependency: Worker-facing endpoint
@router.post("/register", response_model=WorkerOut, status_code=201)
async def register_worker(data: WorkerRegister, db: AsyncSession = Depends(get_db)):
    worker = Worker(
        name=data.name,
        hostname=data.hostname,
        pid=data.pid,
        queues=",".join(data.queues),
        concurrency_limit=data.concurrency_limit,
        last_heartbeat_at=datetime.now(timezone.utc),
    )
    db.add(worker)
    await db.commit()
    await db.refresh(worker)
    await ws_manager.broadcast({"type": "worker_registered", "worker_id": worker.id, "name": worker.name})
    return worker


# STRIPPED User dependency: Worker-facing endpoint
@router.post("/{worker_id}/heartbeat", response_model=WorkerOut)
async def heartbeat(worker_id: int, data: WorkerHeartbeatIn, db: AsyncSession = Depends(get_db)):
    worker = (await db.execute(select(Worker).where(Worker.id == worker_id))).scalar_one_or_none()
    if not worker:
        raise HTTPException(404, "Worker not found")
    worker.status = data.status
    worker.current_job_count = data.active_jobs
    worker.last_heartbeat_at = datetime.now(timezone.utc)
    from app.models import WorkerHeartbeat

    db.add(WorkerHeartbeat(worker_id=worker.id, status=data.status, active_jobs=data.active_jobs, cpu_percent=data.cpu_percent, memory_mb=data.memory_mb))
    await db.commit()
    await db.refresh(worker)
    return worker


# STRIPPED User dependency: Worker-facing endpoint
@router.post("/claim", response_model=list[JobOut])
async def claim(data: ClaimRequest, db: AsyncSession = Depends(get_db)):
    """
    Called by worker processes on every poll cycle. See
    app/services/job_service.claim_jobs for the atomicity guarantee
    (SELECT ... FOR UPDATE SKIP LOCKED).
    """
    jobs = await claim_jobs(db, data.worker_id, data.queue_names, data.max_jobs)
    if jobs:
        await ws_manager.broadcast({"type": "jobs_claimed", "worker_id": data.worker_id, "job_ids": [j.id for j in jobs]})
    return jobs


# STRIPPED User dependency: Worker-facing endpoint
@router.post("/{worker_id}/jobs/{job_id}/start", response_model=JobOut)
async def start_job(worker_id: int, job_id: int, db: AsyncSession = Depends(get_db)):
    job = (await db.execute(select(Job).where(Job.id == job_id, Job.claimed_by_worker_id == worker_id))).scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found or not claimed by this worker")
    await start_execution(db, job, worker_id)
    await ws_manager.broadcast({"type": "job_started", "job_id": job.id, "worker_id": worker_id})
    return job


# STRIPPED User dependency: Worker-facing endpoint
@router.post("/{worker_id}/jobs/{job_id}/result", response_model=JobOut)
async def report_result(worker_id: int, job_id: int, data: ExecutionResultIn, db: AsyncSession = Depends(get_db)):
    job = (await db.execute(select(Job).where(Job.id == job_id, Job.claimed_by_worker_id == worker_id))).scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found or not claimed by this worker")
    execution = (
        await db.execute(
            select(JobExecution)
            .where(JobExecution.job_id == job_id, JobExecution.attempt_number == job.attempt_count)
        )
    ).scalar_one_or_none()
    if not execution:
        raise HTTPException(409, "No in-progress execution found for this job/attempt")

    updated = await record_result(db, job, execution, data)
    await ws_manager.broadcast({"type": "job_finished", "job_id": job.id, "status": updated.status.value})
    return updated


# KEPT User dependency: Frontend dashboard calls this
@router.get("", response_model=list[WorkerOut])
async def list_workers(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    return (await db.execute(select(Worker).order_by(Worker.registered_at.desc()))).scalars().all()