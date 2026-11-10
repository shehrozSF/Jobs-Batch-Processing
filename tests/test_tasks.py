from datetime import timedelta

import pytest
from sqlalchemy import update

from app.db import DEAD, PAUSED, RUNNING, SUCCEEDED, Batch, Job, utcnow
from app.llm import FatalError, RetryableError
from app.service import STALE_AFTER, create_batch, job_ids, progress, requeue
from app.tasks import process_job
from tests.conftest import FakeClient, valid_posting


@pytest.fixture
def batch(session):
    return create_batch(session, "t", [("a", "posting a"), ("b", "posting b")])


def first_job(session, batch):
    return job_ids(session, batch.id)[0]


def reload(session, job_id) -> Job:
    session.expire_all()
    return session.get(Job, job_id)


def no_sleep(_seconds):
    pass


def test_success_stores_the_result_tokens_and_cost(session, batch):
    job_id = first_job(session, batch)
    assert process_job(job_id, client=FakeClient([valid_posting()])) == SUCCEEDED

    job = reload(session, job_id)
    assert job.result["company"] == "Acme"
    assert (job.model_calls, job.input_tokens, job.output_tokens) == (1, 1000, 200)
    assert job.cost_usd == pytest.approx((1000 * 0.35 + 200 * 0.75) / 1e6)


def test_a_temporary_error_is_retried(session, batch):
    job_id = first_job(session, batch)
    client = FakeClient([RetryableError("503"), valid_posting()])
    assert process_job(job_id, client=client, sleep=no_sleep) == SUCCEEDED
    assert reload(session, job_id).attempts == 2


def test_out_of_attempts_goes_to_the_dead_letter_list(session, batch):
    job_id = first_job(session, batch)
    client = FakeClient([RetryableError("503")] * 3)
    assert process_job(job_id, client=client, sleep=no_sleep) == DEAD

    job = reload(session, job_id)
    assert job.attempts == 3
    assert "503" in job.error


def test_invalid_output_is_dead_lettered_without_queue_retries(session, batch):
    # Retrying the same posting would get the same invalid answer.
    job_id = first_job(session, batch)
    client = FakeClient([valid_posting(confidence=4)] * 2)
    assert process_job(job_id, client=client) == DEAD

    job = reload(session, job_id)
    assert job.model_calls == 2  # both calls were paid for and are counted
    assert "confidence" in job.error


def test_a_fatal_error_halts_the_batch_without_more_calls(session, batch):
    first, second = job_ids(session, batch.id)
    process_job(first, client=FakeClient([FatalError("HTTP 402: billing")]))

    # The second job must not reach the model at all.
    assert process_job(second, client=FakeClient([])) == PAUSED
    session.expire_all()
    assert "402" in session.get(Batch, batch.id).halted_reason


def test_requeue_lifts_the_halt_and_the_jobs_run_again(session, batch):
    first, second = job_ids(session, batch.id)
    process_job(first, client=FakeClient([FatalError("HTTP 402")]))
    process_job(second, client=FakeClient([]))

    assert sorted(requeue(session, batch.id)) == [first, second]
    assert process_job(first, client=FakeClient([valid_posting()])) == SUCCEEDED
    assert session.get(Batch, batch.id).halted_reason is None


def test_a_job_stranded_by_a_dead_worker_can_be_retried(session, batch):
    first, second = job_ids(session, batch.id)
    stale = utcnow() - STALE_AFTER - timedelta(minutes=1)
    session.execute(update(Job).where(Job.id == first).values(status=RUNNING, updated_at=stale))
    # Running, but recently: a live worker may still be on it. Leave it alone.
    session.execute(update(Job).where(Job.id == second).values(status=RUNNING))
    session.commit()

    assert progress(session, batch.id)["retryable"] == 1
    assert requeue(session, batch.id) == [first]


def test_a_redelivered_job_is_not_run_twice(session, batch):
    job_id = first_job(session, batch)
    process_job(job_id, client=FakeClient([valid_posting()]))
    assert process_job(job_id, client=FakeClient([])) == SUCCEEDED
