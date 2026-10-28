"""MCP tool service entry point: python -m alloyiq.mcp_server

Private Streamable HTTP at http://mcp:8100/mcp (service token + audience). Reads only the
config tables (instruments, provider mappings) using the worker database role.
"""

from __future__ import annotations

import uvicorn

from alloyiq.config import get_settings
from alloyiq.mcp_server.app import build_app
from alloyiq.observability.logging import configure_logging
from alloyiq.repositories.db import Database
from alloyiq.tools.factory import build_tool_service


def main() -> None:
    settings = get_settings()
    configure_logging(
        settings.log_level, service="mcp", deployment_version=settings.deployment_version
    )
    if not settings.database_worker_url:
        raise SystemExit("DATABASE_WORKER_URL is not configured")
    db = Database(settings.database_worker_url.get_secret_value(), pool_size=5)
    app = build_app(
        build_tool_service(settings, db),
        token=settings.mcp_service_token.get_secret_value(),
        allowed_hosts=settings.mcp_allowed_hosts,
    )
    uvicorn.run(
        app, host=settings.mcp_host, port=settings.mcp_port, log_config=None, access_log=False
    )


if __name__ == "__main__":
    main()
