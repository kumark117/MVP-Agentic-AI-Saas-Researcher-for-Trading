"""Structured JSON logging (build prompt section 17).

Correlation fields are bound with structlog contextvars, so every log line
emitted while handling a request carries its request_id. Later phases bind
run_id, node_id, call_key and the rest the same way.
"""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: str = "INFO", *, service: str, deployment_version: str) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(service=service, deployment_version=deployment_version)


def get_logger(name: str) -> structlog.typing.FilteringBoundLogger:
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger(name)
    return logger
