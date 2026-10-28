"""Health endpoints (Design section 15).

/health/live reports process liveness only. /health/ready reports whether new
work can be admitted safely; it is internal (Caddy does not route it) and
returns 503 when the database is unavailable. Neither exposes secrets or a
dependency inventory.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from alloyiq.api.envelope import Envelope, ok
from alloyiq.api.errors import ApiError, ErrorCode
from alloyiq.observability.logging import get_logger

# Raises on failure; returns None when the dependency can serve requests.
ReadinessProbe = Callable[[], Awaitable[None]]

log = get_logger(__name__)


class HealthStatus(BaseModel):
    status: Literal["ok"]


def build_health_router(readiness_probe: ReadinessProbe) -> APIRouter:
    router = APIRouter(prefix="/health", tags=["health"])

    @router.get("/live", response_model=Envelope[HealthStatus])
    async def live(request: Request) -> Envelope[HealthStatus]:
        return ok(request, HealthStatus(status="ok"))

    @router.get(
        "/ready",
        response_model=Envelope[HealthStatus],
        responses={503: {"description": "Not safe to admit new work"}},
    )
    async def ready(request: Request) -> Envelope[HealthStatus]:
        try:
            await readiness_probe()
        except Exception as exc:
            log.warning("readiness_failed", error_type=type(exc).__name__)
            raise ApiError(
                503,
                ErrorCode.PROVIDER_UNAVAILABLE,
                "Not ready",
                "The service cannot admit new work right now.",
                headers={"Retry-After": "5"},
            ) from None
        return ok(request, HealthStatus(status="ok"))

    return router
