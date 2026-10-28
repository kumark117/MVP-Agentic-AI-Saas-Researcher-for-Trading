"""Worker process entry point: python -m alloyiq.worker

Runs the periodic loops (email, dummy payments, expiry, recovery) and the job runner
daemon. Shuts down cleanly on SIGTERM / Ctrl+C: leases are not renewed, so another
worker can take over after they expire (fenced recovery).
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import socket
import uuid
from collections.abc import Awaitable, Callable

from alloyiq.config import get_settings
from alloyiq.observability.logging import configure_logging, get_logger
from alloyiq.worker.loops import build_worker


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

    worker_id = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
    parts = build_worker(settings, worker_id)
    log.info(
        "worker_started",
        worker_id=worker_id,
        loops=[n for n, *_ in parts.loops],
        daemons=[n for n, _ in parts.daemons],
    )
    tasks = [
        asyncio.create_task(_periodic(name, fn, every, stop), name=name)
        for name, fn, every in parts.loops
    ] + [asyncio.create_task(_daemon(name, fn, stop), name=name) for name, fn in parts.daemons]
    await stop.wait()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for cleanup in parts.cleanup:
        with contextlib.suppress(Exception):
            await cleanup()
    log.info("worker_stopped", worker_id=worker_id)


async def _periodic(
    name: str, fn: Callable[[], Awaitable[object]], every: float, stop: asyncio.Event
) -> None:
    log = get_logger(__name__)
    while not stop.is_set():
        try:
            await fn()
        except Exception:
            log.exception("loop_failed", loop=name)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every)


async def _daemon(
    name: str, fn: Callable[[asyncio.Event], Awaitable[None]], stop: asyncio.Event
) -> None:
    log = get_logger(__name__)
    while not stop.is_set():
        try:
            await fn(stop)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("daemon_failed", daemon=name)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=5)


if __name__ == "__main__":
    asyncio.run(run())
