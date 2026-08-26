import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal, get_db
from app.deps import get_current_user
from app.models import Job, JobStatus, User, Worker, WorkerStatus
from app.security import decode_access_token
from app.websocket_manager import ws_manager

logger = logging.getLogger("dashboard-ws")

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


async def _authenticate_ws(token: str | None) -> User | None:
    """
    Mirrors app.deps.get_current_user's checks, but WebSocket handshakes
    don't carry an Authorization header the way REST requests do (browser
    WebSocket clients can't set custom headers), so the token travels as a
    query param instead. Same JWT, same validation — just a different
    transport for it.
    """
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload or "sub" not in payload:
        return None
    async with AsyncSessionLocal() as db:
        user = (await db.execute(select(User).where(User.id == int(payload["sub"])))).scalar_one_or_none()
    return user if user and user.is_active else None


@router.websocket("/ws")
async def dashboard_ws(
    websocket: WebSocket,
    token: str | None = Query(default=None),
    since: int | None = Query(default=None, description="Replay events with id > since from the in-memory backlog before streaming live."),
):
    """
    Live feed of job/worker lifecycle events. Requires the same JWT as
    every REST endpoint, passed as ?token=... since browser WebSocket
    clients cannot set an Authorization header on the handshake request.

    Connection lifecycle:
      1. Validate token BEFORE accept() — an invalid/missing token gets a
         policy-violation close (4401) without ever joining the broadcast
         set, so unauthenticated clients never observe live data even
         momentarily.
      2. If ?since=<id> is present, replay any buffered events newer than
         that id (best-effort — see websocket_manager's consistency model).
      3. Stream live events until the client disconnects or a keep-alive
         ping stops arriving.
    """
    user = await _authenticate_ws(token)
    if not user:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="unauthenticated")
        return

    await ws_manager.connect(websocket)
    logger.info("dashboard ws connected user_id=%s since=%s", user.id, since)
    try:
        if since is not None:
            for envelope in ws_manager.replay_since(since):
                await websocket.send_json(envelope)
        while True:
            await websocket.receive_text()  # keep-alive ping from client; content unused
    except WebSocketDisconnect:
        pass
    finally:
        await ws_manager.disconnect(websocket)
        logger.info("dashboard ws disconnected user_id=%s", user.id)