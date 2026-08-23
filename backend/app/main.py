import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.config import get_settings
from app.database import AsyncSessionLocal, Base, engine
from app.routers import auth, dashboard, jobs, projects, queues, workers
from app.services.scheduler_service import run_tick

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("scheduler-api")
settings = get_settings()

limiter = Limiter(key_func=get_remote_address, default_limits=["300/minute"])


async def _embedded_scheduler_loop():
    """
    Runs the scheduler tick inside the API process so `docker compose up`
    with a single container still promotes delayed/cron jobs. In a real
    deployment this loop is disabled (EMBEDDED_SCHEDULER=false) in favor
    of the standalone scripts/run_scheduler.py process so scheduling
    doesn't compete with API request handling and can be scaled/restarted
    independently.
    """
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await run_tick(db)
        except Exception:
            logger.exception("scheduler tick failed")
        await asyncio.sleep(settings.scheduler_tick_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)  # dev convenience; use Alembic migrations in production
    task = asyncio.create_task(_embedded_scheduler_loop())
    logger.info("API started, embedded scheduler loop running")
    yield
    task.cancel()


app = FastAPI(title="Distributed Job Scheduler API", version="1.0.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000
    logger.info("%s %s -> %s (%.1fms)", request.method, request.url.path, response.status_code, duration_ms)
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail, "code": str(exc.status_code)})


@app.get("/health")
async def health():
    return {"status": "ok"}


app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(queues.router)
app.include_router(jobs.router)
app.include_router(jobs.dlq_router)
app.include_router(jobs.cron_router)
app.include_router(workers.router)
app.include_router(dashboard.router)