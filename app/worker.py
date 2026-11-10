"""Start a worker:  python -m app.worker

Run several to process a batch in parallel; the shared rate limiter keeps
them under the vendor's limit together.
"""

import logging
import os

from rq import SimpleWorker, Worker
from rq.timeouts import TimerDeathPenalty

from app.queue import get_queue, get_redis


class WindowsWorker(SimpleWorker):
    # Windows has no fork() and no SIGALRM, which RQ's default worker and
    # job timeout rely on. Run jobs in-process with a thread-based timeout.
    death_penalty_class = TimerDeathPenalty


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    worker_class = WindowsWorker if os.name == "nt" else Worker
    worker_class([get_queue()], connection=get_redis()).work()


if __name__ == "__main__":
    main()
