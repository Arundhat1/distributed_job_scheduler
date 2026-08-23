"""
Tests that exercise atomic claiming rely on real PostgreSQL row locking
(SELECT ... FOR UPDATE SKIP LOCKED), which SQLite cannot emulate, so
these fixtures point at a real Postgres instance rather than an
in-memory DB. Run `docker compose -f docker-compose.test.yml up -d`
(or point TEST_DATABASE_URL at any scratch Postgres database) before
running pytest. See docs/SETUP.md.
"""
import asyncio
import os

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Organization, Project, Queue, Worker

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://scheduler:scheduler@localhost:5433/scheduler_test"
)


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with session_maker() as session:
        yield session

    await engine.dispose()


@pytest_asyncio.fixture
async def sample_queue(db_session):
    org = Organization(name="Test Org")
    db_session.add(org)
    await db_session.flush()

    project = Project(organization_id=org.id, name="Test Project")
    db_session.add(project)
    await db_session.flush()

    queue = Queue(project_id=project.id, name="test-queue", priority=0, max_concurrency=2)
    db_session.add(queue)
    await db_session.commit()
    await db_session.refresh(queue)
    return queue

@pytest_asyncio.fixture
async def sample_worker(db_session, sample_queue):
    worker = Worker(
        name="test-worker",
        hostname="test-host",
        pid=10001,
        queues=sample_queue.name,
        concurrency_limit=5,
    )
    db_session.add(worker)
    await db_session.commit()
    await db_session.refresh(worker)
    return worker