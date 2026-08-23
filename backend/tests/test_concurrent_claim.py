"""
The single most important reliability guarantee in this system: under
concurrent claim attempts, every QUEUED job is claimed by exactly one
worker. This test creates N jobs and fires M concurrent claim() calls,
each on its own DB connection (to genuinely exercise row-level locking
rather than being serialized by a shared session), then asserts the
claimed sets are disjoint and their union covers every job exactly once.
"""
import asyncio
from random import sample

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Job, JobStatus, Worker
from app.schemas import JobCreate
from app.services.job_service import claim_jobs, create_job
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.asyncio


async def test_concurrent_claims_never_double_claim_a_job(db_session, sample_queue):
    sample_queue.max_concurrency = 100  # remove concurrency as a confound; we're testing claim exclusivity
    await db_session.commit()

    num_jobs = 30
    for i in range(num_jobs):
        await create_job(db_session, sample_queue, JobCreate(name=f"job{i}"))
    await db_session.commit()
    workers = [Worker(name = f"test-worker-{i}", hostname=f"test-host-{i}",pid = 10000+i, queues =sample_queue.name,concurrency_limit=5,) for i in range(10)]
    
    db_session.add_all(workers)
    await db_session.commit()
    worker_ids = [worker.id for worker in workers]
    engine = create_async_engine(TEST_DATABASE_URL, pool_size=10)
    session_maker = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def worker_claim(worker_id: int):
        async with session_maker() as session:
            return await claim_jobs(session, worker_id=worker_id, queue_names=[sample_queue.name], max_jobs=5)

    results = await asyncio.gather(*[worker_claim(worker_id) for worker_id in worker_ids])
    await engine.dispose()

    claimed_ids = [job.id for jobs in results for job in jobs]
    assert len(claimed_ids) == num_jobs, "every job should have been claimed exactly once across all workers"
    assert len(set(claimed_ids)) == num_jobs, "no job should be claimed by more than one worker"

    all_jobs = (await db_session.execute(select(Job).where(Job.queue_id == sample_queue.id))).scalars().all()
    assert all(j.status == JobStatus.CLAIMED for j in all_jobs)