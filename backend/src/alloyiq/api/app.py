"""FastAPI application factory.

Run with: uvicorn alloyiq.api.app:create_app --factory
Dependencies are passed in explicitly so tests can inject fakes.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from alloyiq import __version__
from alloyiq.adapters.postgres_probe import postgres_probe
from alloyiq.api.errors import install_error_handlers
from alloyiq.api.health import ReadinessProbe, build_health_router
from alloyiq.api.middleware import RequestContextMiddleware
from alloyiq.config import Settings, get_settings
from alloyiq.observability.logging import configure_logging

API_PREFIX = "/api/v1"


def create_app(
    settings: Settings | None = None,
    *,
    readiness_probe: ReadinessProbe | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(
        settings.log_level, service="api", deployment_version=settings.deployment_version
    )
    if readiness_probe is None:
        url = settings.database_url.get_secret_value() if settings.database_url else None
        readiness_probe = postgres_probe(url)

    app = FastAPI(
        title="AlloyIQ API",
        version=__version__,
        openapi_url=f"{API_PREFIX}/openapi.json",
        # Interactive docs only in dev; the committed OpenAPI file is the contract (D110).
        docs_url=None if settings.is_prod else f"{API_PREFIX}/docs",
        redoc_url=None,
    )
    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware)

    v1 = APIRouter(prefix=API_PREFIX)
    v1.include_router(build_health_router(readiness_probe))
    app.include_router(v1)
    return app
