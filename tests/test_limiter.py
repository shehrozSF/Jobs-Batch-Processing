import time

import fakeredis

from app.limiter import RateLimiter


def test_calls_are_spaced_by_the_interval():
    limiter = RateLimiter(fakeredis.FakeRedis(), requests_per_minute=600)  # one per 100ms
    start = time.monotonic()
    for _ in range(3):
        limiter.acquire()
    # The first call is free; the next two each wait one interval.
    assert time.monotonic() - start >= 0.18


def test_workers_sharing_redis_share_the_limit():
    redis = fakeredis.FakeRedis()
    worker_a = RateLimiter(redis, requests_per_minute=600)
    worker_b = RateLimiter(redis, requests_per_minute=600)
    worker_a.acquire()
    start = time.monotonic()
    worker_b.acquire()  # a different worker still has to wait its turn
    assert time.monotonic() - start >= 0.08
