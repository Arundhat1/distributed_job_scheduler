"""
Standalone scheduler process — the horizontally-independent counterpart
to app.main's embedded loop. Run this instead of (or in addition to,
it's idempotent-safe) the embedded loop in a real deployment:

    python scripts/run_scheduler.py
"""
import asyncio
import logging

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.services.scheduler_service import run_tick

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("scheduler-process")
settings = get_settings()


async def main():
    logger.info("scheduler process started, tick=%ss", settings.scheduler_tick_seconds)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await run_tick(db)
        except Exception:
            logger.exception("scheduler tick failed")
        await asyncio.sleep(settings.scheduler_tick_seconds)


if __name__ == "__main__":
    asyncio.run(main())