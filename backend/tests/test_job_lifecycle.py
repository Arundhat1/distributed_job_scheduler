from datetime import datetime, timedelta, timezone

import pytest

from app.models import DeadLetterEntry, ExecutionStatus, JobStatus, RetryPolicy, RetryStrategy
from app.schemas import ExecutionResultIn, JobCreate
from app.services.job_service import claim_jobs, create_job, record_result, start_execution

pytestmark = pytest.mark.asyncio


async def test_immediate_job_is_created_as_queued(db_session, sample_queue):
    job = await create_job(db_session, sample_queue, JobCreate(name="send_email", payload={"to": "a@b.com"}))
    await db_session.commit()
    assert job.status == JobStatus.QUEUED


async def test_future_run_at_creates_scheduled_status(db_session, sample_queue):
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    job = await create_job(db_session, sample_queue, JobCreate(name="delayed", run_at=future))
    await db_session.commit()
    assert job.status == JobStatus.SCHEDULED


async def test_claim_moves_job_to_claimed_and_stamps_worker(db_session, sample_queue,sample_worker):
    job = await create_job(db_session, sample_queue, JobCreate(name="job1"))
    await db_session.commit()

    claimed = await claim_jobs(db_session, worker_id=sample_worker.id, queue_names=[sample_queue.name], max_jobs=5)
    assert len(claimed) == 1
    assert claimed[0].id == job.id
    assert claimed[0].status == JobStatus.CLAIMED
    assert claimed[0].claimed_by_worker_id == sample_worker.id


async def test_claim_respects_queue_max_concurrency(db_session, sample_queue, sample_worker):
    # sample_queue has max_concurrency=2
    for i in range(5):
        await create_job(db_session, sample_queue, JobCreate(name=f"job{i}"))
    await db_session.commit()

    claimed = await claim_jobs(db_session, worker_id=sample_worker.id, queue_names=[sample_queue.name], max_jobs=10)
    assert len(claimed) == 2  # capped by queue.max_concurrency, not max_jobs


async def test_successful_execution_marks_job_completed(db_session, sample_queue, sample_worker):
    job = await create_job(db_session, sample_queue, JobCreate(name="job1"))
    await db_session.commit()
    claimed = await claim_jobs(db_session, worker_id=sample_worker.id, queue_names=[sample_queue.name], max_jobs=1)
    execution = await start_execution(db_session, claimed[0], worker_id=sample_worker.id)

    updated = await record_result(db_session, claimed[0], execution, ExecutionResultIn(status=ExecutionStatus.SUCCEEDED, result={"ok": True}))
    assert updated.status == JobStatus.COMPLETED
    assert updated.completed_at is not None


async def test_failed_execution_schedules_retry_when_attempts_remain(db_session, sample_queue,sample_worker):
    job = await create_job(db_session, sample_queue, JobCreate(name="job1", max_retries=3))
    await db_session.commit()
    claimed = await claim_jobs(db_session, worker_id=sample_worker.id, queue_names=[sample_queue.name], max_jobs=1)
    execution = await start_execution(db_session, claimed[0], worker_id=sample_worker.id)

    updated = await record_result(db_session, claimed[0], execution, ExecutionResultIn(status=ExecutionStatus.FAILED, error_message="boom"))
    assert updated.status == JobStatus.RETRY_SCHEDULED
    assert updated.run_at > datetime.now(timezone.utc)


async def test_failure_after_max_retries_moves_to_dead_letter(db_session, sample_queue, sample_worker):
    job = await create_job(db_session, sample_queue, JobCreate(name="job1", max_retries=0))
    await db_session.commit()
    claimed = await claim_jobs(db_session, worker_id=sample_worker.id, queue_names=[sample_queue.name], max_jobs=1)
    execution = await start_execution(db_session, claimed[0], worker_id=sample_worker.id)

    updated = await record_result(db_session, claimed[0], execution, ExecutionResultIn(status=ExecutionStatus.FAILED, error_message="fatal"))
    assert updated.status == JobStatus.DEAD_LETTER

    from sqlalchemy import select
    dlq_entry = (await db_session.execute(select(DeadLetterEntry).where(DeadLetterEntry.job_id == job.id))).scalar_one_or_none()
    assert dlq_entry is not None
    assert dlq_entry.final_error == "fatal"


async def test_retry_policy_on_queue_overrides_job_defaults(db_session, sample_queue, sample_worker):
    policy = RetryPolicy(name="fast-fixed", strategy=RetryStrategy.FIXED, base_delay_seconds=7, multiplier=1.0, max_delay_seconds=100, max_retries=1)
    db_session.add(policy)
    await db_session.flush()
    sample_queue.default_retry_policy_id = policy.id
    await db_session.commit()

    job = await create_job(db_session, sample_queue, JobCreate(name="job1"))
    await db_session.commit()
    claimed = await claim_jobs(db_session, worker_id=sample_worker.id, queue_names=[sample_queue.name], max_jobs=1)
    execution = await start_execution(db_session, claimed[0], worker_id=sample_worker.id)

    updated = await record_result(db_session, claimed[0], execution, ExecutionResultIn(status=ExecutionStatus.FAILED, error_message="x"))
    assert updated.status == JobStatus.RETRY_SCHEDULED
    delta = (updated.run_at - datetime.now(timezone.utc)).total_seconds()
    assert 5 < delta <= 8  # ~7s fixed delay from the queue's policy, not job default (5s)