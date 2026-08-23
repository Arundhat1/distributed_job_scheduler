from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models import Job, JobStatus, User, Worker, WorkerStatus
from app.websocket_manager import ws_manager

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])


@router.get("/summary")
async def dashboard_summary(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    counts = dict((await db.execute(select(Job.status, func.count(Job.id)).group_by(Job.status))).all())
    worker_counts = dict((await db.execute(select(Worker.status, func.count(Worker.id)).group_by(Worker.status))).all())

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    hourly = (
        await db.execute(
            select(func.date_trunc("hour", Job.completed_at).label("hour"), func.count(Job.id))
            .where(Job.status == JobStatus.COMPLETED, Job.completed_at >= since)
            .group_by("hour")
            .order_by("hour")
        )
    ).all()

    return {
        "job_counts": {status.value if hasattr(status, "value") else status: count for status, count in counts.items()},
        "worker_counts": {status.value if hasattr(status, "value") else status: count for status, count in worker_counts.items()},
        "throughput_by_hour": [{"hour": str(h), "count": c} for h, c in hourly],
        "active_workers": worker_counts.get(WorkerStatus.IDLE, 0) + worker_counts.get(WorkerStatus.BUSY, 0),
    }


@router.websocket("/ws")
async def dashboard_ws(websocket: WebSocket):
    """
    Live feed of job/worker lifecycle events (see websocket_manager.broadcast
    calls throughout the routers). No auth token verification is done in
    this minimal example over the WS handshake itself — a production
    build would validate a short-lived token passed as a query param
    before calling ws_manager.connect().
    """
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()  # keep-alive ping from client; we don't act on content
    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)