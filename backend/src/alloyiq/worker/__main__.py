"""Worker process entry point: python -m alloyiq.worker

Phase 0 stub. It starts, logs a heartbeat and shuts down cleanly on SIGTERM
(compose stop) or Ctrl+C. Job claiming, leases and fencing arrive in phase 5.
"""

from __future__ import annotations

import asyncio
import signal
import socket

from alloyiq.config import get_settings
from alloyiq.observability.logging import configure_logging, get_logger

HEARTBEAT_S = 30.0


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
    log.info("worker_started", worker_id=worker_id, phase="0-stub")
    while not stop.is_set():
        log.info("worker_heartbeat", worker_id=worker_id)
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_S)
        except TimeoutError:
            continue
    log.info("worker_stopped", worker_id=worker_id)


if __name__ == "__main__":
    asyncio.run(run())
