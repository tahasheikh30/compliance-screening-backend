"""
Bounded work lane with fast refusal.

Screening is the expensive route. Running it on the shared AnyIO thread pool lets a burst occupy every
thread, so even /api/me and /api/health wait behind it. Here it gets a dedicated pool and a bounded queue:
when both are full the request is refused immediately with a 503 and Retry-After. A quick, honest
"busy" is better for the caller and for the server than a request that hangs and then times out.
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

from starlette.concurrency import run_in_threadpool  # noqa: F401  (kept for callers that want the shared pool)

from app import config
from app.errors import AppError


class Lane:
    def __init__(self, workers: int, max_queue: int, name: str = "work"):
        self.workers = workers
        self.capacity = workers + max_queue
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        self._lock = threading.Lock()
        self._in_flight = 0     # running plus waiting

    @property
    def in_flight(self) -> int:
        return self._in_flight

    async def run(self, fn, *args, **kwargs):
        with self._lock:
            if self._in_flight >= self.capacity:
                raise AppError(503, "SERVER_BUSY", "The server is busy right now.",
                               "Wait a few seconds and try again.", headers={"Retry-After": "3"})
            self._in_flight += 1
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._executor, lambda: fn(*args, **kwargs))
        finally:
            with self._lock:
                self._in_flight -= 1


screen_lane = Lane(config.SCREEN_WORKERS, config.SCREEN_MAX_QUEUE, "screen")
