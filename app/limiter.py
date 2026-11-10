import time

from redis import Redis


class RateLimiter:
    """Spaces model calls evenly across every worker process.

    Each call must claim a Redis key that lives for one interval
    (60 / requests_per_minute seconds). SET NX is atomic, so only one worker
    can claim it at a time, however many workers run. The rest wait for it to
    expire. That keeps the whole fleet under the vendor's limit instead of
    each worker pacing itself and the total going over.
    """

    def __init__(self, redis: Redis, requests_per_minute: float, key: str = "ratelimit:llm"):
        self.redis = redis
        self.key = key
        self.interval_ms = max(1, int(60_000 / requests_per_minute))

    def acquire(self) -> None:
        while not self.redis.set(self.key, "1", nx=True, px=self.interval_ms):
            wait_ms = self.redis.pttl(self.key)
            time.sleep(max(wait_ms, 20) / 1000)
