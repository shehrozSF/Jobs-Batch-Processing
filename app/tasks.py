"""The unit of work a worker runs: one posting -> one validated result."""

import logging
import random
import time

from app.config import get_settings
from app.db import DEAD, PAUSED, QUEUED, RUNNING, SUCCEEDED, Job, new_session
from app.extract import ExtractionFailed, Usage, extract
from app.limiter import RateLimiter
from app.llm import CerebrasClient, FatalError, RetryableError

log = logging.getLogger(__name__)


def backoff_seconds(attempt: int, retry_after: float | None = None) -> float:
    """Exponential backoff with jitter. Jitter matters here: a batch's jobs all
    start together, and without it they would all retry at the same moment."""
    if retry_after is not None:
        return retry_after
    return min(60.0, 5.0 * 2 ** (attempt - 1)) * random.uniform(0.5, 1.5)


def default_client(settings) -> CerebrasClient:
    from app.queue import get_redis

    return CerebrasClient(settings, limiter=RateLimiter(get_redis(), settings.requests_per_minute))


def process_job(job_id: int, *, client=None, sleep=time.sleep) -> str:
    """Run one job to a final status and return it. Never raises for a model
    failure, so RQ has nothing to retry: retries happen here, where the
    attempt count and error are recorded on the job row."""
    settings = get_settings()
    client = client or default_client(settings)

    with new_session() as session:
        job = session.get(Job, job_id)
        if job is None or job.status != QUEUED:
            return job.status if job else "missing"  # already handled; safe to re-deliver
        if job.batch.halted_reason:
            job.status = PAUSED
            session.commit()
            return PAUSED

        job.status = RUNNING
        session.commit()

        for attempt in range(1, settings.max_attempts + 1):
            job.attempts += 1
            usage = Usage()
            try:
                extraction = extract(job.body, client, usage)
            except RetryableError as exc:
                _record(job, usage, settings)
                job.error = str(exc)
                session.commit()
                if attempt < settings.max_attempts:
                    delay = backoff_seconds(attempt, exc.retry_after)
                    log.warning("job %s: %s; retrying in %.0fs", job_id, exc, delay)
                    sleep(delay)
                    continue
                job.status = DEAD
            except FatalError as exc:
                # Every other job would hit the same wall. Halt the batch; the
                # remaining jobs pause instead of burning requests.
                _record(job, usage, settings)
                job.error = str(exc)
                job.status = PAUSED
                job.batch.halted_reason = str(exc)
                log.error("job %s: halting batch %s: %s", job_id, job.batch_id, exc)
            except ExtractionFailed as exc:
                _record(job, exc.usage, settings)
                job.error = str(exc)
                job.status = DEAD
            else:
                _record(job, extraction.usage, settings)
                job.result = extraction.posting.model_dump()
                job.error = None
                job.status = SUCCEEDED
            break

        session.commit()
        log.info("job %s -> %s", job_id, job.status)
        return job.status


def _record(job: Job, usage: Usage, settings) -> None:
    job.model_calls += usage.calls
    job.input_tokens += usage.input_tokens
    job.output_tokens += usage.output_tokens
    job.cost_usd += (
        usage.input_tokens * settings.input_cost_per_mtok
        + usage.output_tokens * settings.output_cost_per_mtok
    ) / 1_000_000
