"""Worker process entry point: python -m alloyiq.worker

Runs the periodic loops (email delivery now; job claiming, sweepers and schedulers are
added by later phases through worker.loops). Shuts down cleanly on SIGTERM / Ctrl+C.
"""

from __future__ import annotations

import asyncio
import signal
import socket

from alloyiq.config import get_settings
from alloyiq.observability.logging import configure_logging, get_logger
from alloyiq.worker.loops import build_loops


async def run() -> None:
    settings = get_settings()
    configure_logging(
        settings.log_level, service="worker", deployment_version=settings.deployment_version
    )
    log = get_logger(__name__)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows event loop; Ctrl+C still raises KeyboardInterrupt
            pass

    worker_id = socket.gethostname()
    loops, cleanup = build_loops(settings, worker_id)
    log.info("worker_started", worker_id=worker_id, loops=[n for n, *_ in loops])
    tasks = [
        asyncio.create_task(_periodic(name, fn, every, stop), name=name)
        for name, fn, every in loops
    ]
    await stop.wait()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await cleanup()
    log.info("worker_stopped", worker_id=worker_id)


async def _periodic(name: str, fn, every: float, stop: asyncio.Event) -> None:  # type: ignore[no-untyped-def]
    log = get_logger(__name__)
    while not stop.is_set():
        try:
            await fn()
        except Exception:
            log.exception("loop_failed", loop=name)
        try:
            await asyncio.wait_for(stop.wait(), timeout=every)
        except TimeoutError:
            pass


if __name__ == "__main__":
    asyncio.run(run())
