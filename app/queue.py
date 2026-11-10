from functools import lru_cache

from redis import Redis
from rq import Queue

from app.config import get_settings

# Generous: a job may wait for the rate limiter and back off between retries.
JOB_TIMEOUT_SECONDS = 15 * 60


@lru_cache
def get_redis() -> Redis:
    return Redis.from_url(get_settings().redis_url)


def get_queue() -> Queue:
    return Queue(get_settings().queue_name, connection=get_redis())


def enqueue_jobs(job_ids: list[int]) -> None:
    """Only the job id goes on the queue. The posting, status and results live
    in Postgres, so losing Redis loses pending work items, never data."""
    queue = get_queue()
    for job_id in job_ids:
        queue.enqueue("app.tasks.process_job", job_id, job_timeout=JOB_TIMEOUT_SECONDS)
