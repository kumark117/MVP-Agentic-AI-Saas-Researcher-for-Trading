"""MCP tool service entry point: python -m alloyiq.mcp_server

Phase 0 stub: serves only GET /health on the private compose network so the
topology can be verified. The FastMCP Streamable HTTP app at /mcp, with the
service token and the five read-only tools, replaces it in phase 3.
"""

from __future__ import annotations

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from alloyiq.config import get_settings
from alloyiq.observability.logging import configure_logging


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "phase": "0-stub"})


def create_app() -> Starlette:
    return Starlette(routes=[Route("/health", health)])


def main() -> None:
    settings = get_settings()
    configure_logging(
        settings.log_level, service="mcp", deployment_version=settings.deployment_version
    )
    uvicorn.run(create_app(), host=settings.mcp_host, port=settings.mcp_port, log_config=None)


if __name__ == "__main__":
    main()
