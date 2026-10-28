"""Request correlation middleware (D066).

Written as plain ASGI rather than BaseHTTPMiddleware so it never buffers
streaming responses (the SSE endpoints arrive in phase 5).
"""

from __future__ import annotations

import re
import time
import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from alloyiq.observability.logging import get_logger

REQUEST_ID_HEADER = b"x-request-id"
# A public correlation header is accepted only if it is a plain UUID (D066).
_UUID_RE = re.compile(
    rb"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

log = get_logger("alloyiq.access")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(REQUEST_ID_HEADER)
        request_id = (
            incoming.decode() if incoming and _UUID_RE.match(incoming) else str(uuid.uuid4())
        )
        scope.setdefault("state", {})["request_id"] = request_id

        status = 500
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                headers.append((REQUEST_ID_HEADER, request_id.encode()))
                message["headers"] = headers
            await send(message)

        with structlog.contextvars.bound_contextvars(request_id=request_id):
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                log.info(
                    "request",
                    method=scope["method"],
                    path=scope["path"],
                    status=status,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                )
