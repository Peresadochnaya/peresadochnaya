"""Bound caller latency and concurrent blocking I/O without queued late calls."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

_EXECUTOR=ThreadPoolExecutor(max_workers=12,thread_name_prefix='traffic-io')
_SLOTS=threading.BoundedSemaphore(12)


def run_bounded(function,deadline,clock=time.monotonic):
    remaining=deadline-clock()
    if remaining<=0: raise TimeoutError('search_deadline')
    if not _SLOTS.acquire(blocking=False): raise TimeoutError('traffic_workers_busy')
    def work():
        if clock()>=deadline: raise TimeoutError('search_deadline')
        return function()
    try:
        future=_EXECUTOR.submit(work)
    except BaseException:
        _SLOTS.release(); raise
    future.add_done_callback(lambda _: _SLOTS.release())
    try:
        return future.result(timeout=remaining)
    except TimeoutError:
        future.cancel()
        raise
