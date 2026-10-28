"""MCPToolGateway: the worker's client for the private MCP service (D028, D040).

Before dispatch it checks the tool name against the registry; after the call it validates
the envelope (schema, tool name, tool_call_id echo, status/error consistency). HTTP success
or parseable JSON alone is never treated as a successful tool call.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from pydantic import ValidationError

from alloyiq.tools.schemas import (
    TOOL_DEADLINE_S,
    TOOL_VERSIONS,
    FxArgs,
    InstrumentArgs,
    NewsArgs,
    ToolEnvelope,
    ToolError,
    ToolErrorCode,
)
from alloyiq.tools.service import ToolService


class ToolSession(Protocol):
    async def call(self, tool: str, args: dict[str, Any]) -> ToolEnvelope: ...


def _failed(tool: str, call_id: UUID, code: ToolErrorCode, message: str) -> ToolEnvelope:
    return ToolEnvelope(
        tool_call_id=call_id,
        tool=tool,
        tool_version=TOOL_VERSIONS.get(tool, "?"),
        status="error",
        fetched_at=datetime.now(UTC),
        error=ToolError(code=code, message=message[:300]),
    )


def validate_envelope(tool: str, call_id: UUID, raw: object) -> ToolEnvelope:
    try:
        env = ToolEnvelope.model_validate(raw)
    except ValidationError:
        return _failed(tool, call_id, "INVALID_RESPONSE", "tool result failed schema validation")
    if env.tool != tool or env.tool_call_id != call_id:
        return _failed(tool, call_id, "INVALID_RESPONSE", "tool result does not match the call")
    if env.status == "ok" and (env.data is None or env.error is not None):
        return _failed(tool, call_id, "INVALID_RESPONSE", "ok result without data")
    if env.status == "error" and env.error is None:
        return _failed(tool, call_id, "INVALID_RESPONSE", "error result without error detail")
    return env


def _call_id(tool: str, args: dict[str, Any]) -> UUID:
    if tool not in TOOL_VERSIONS:
        raise ValueError(f"tool {tool!r} is not in the registry")
    return UUID(str(args["tool_call_id"]))


class InProcessSession:
    """Calls ToolService directly (tests, fixture mode). Same validation as the HTTP path."""

    def __init__(self, service: ToolService) -> None:
        self.service = service

    async def call(self, tool: str, args: dict[str, Any]) -> ToolEnvelope:
        call_id = _call_id(tool, args)
        try:
            if tool == "get_fx_rate":
                env = await self.service.get_fx_rate(FxArgs.model_validate(args))
            elif tool == "search_news":
                env = await self.service.search_news(NewsArgs.model_validate(args))
            else:
                method = getattr(self.service, tool)
                env = await method(InstrumentArgs.model_validate(args))
        except ValidationError:
            return _failed(tool, call_id, "VALIDATION_FAILED", "tool arguments failed validation")
        return validate_envelope(tool, call_id, env.model_dump(mode="json"))


class _McpSession:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def call(self, tool: str, args: dict[str, Any]) -> ToolEnvelope:
        call_id = _call_id(tool, args)
        try:
            result = await self.client.call_tool(
                tool,
                {k: str(v) if isinstance(v, UUID) else v for k, v in args.items()},
                read_timeout_seconds=TOOL_DEADLINE_S + 4,
            )
        except TimeoutError:
            return _failed(tool, call_id, "PROVIDER_UNAVAILABLE", "MCP call timed out")
        except Exception as exc:  # protocol or transport failure
            return _failed(
                tool, call_id, "PROVIDER_UNAVAILABLE", f"MCP call failed: {type(exc).__name__}"
            )
        if getattr(result, "is_error", False):
            return _failed(tool, call_id, "VALIDATION_FAILED", "MCP server rejected the call")
        return validate_envelope(tool, call_id, getattr(result, "structured_content", None))


class MCPToolGateway:
    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self._headers = {"Authorization": f"Bearer {token}", "X-MCP-Audience": "alloyiq-tools"}

    @asynccontextmanager
    async def session(self) -> AsyncIterator[ToolSession]:
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client

        http = create_mcp_http_client(headers=self._headers)
        async with http, Client(streamable_http_client(self.url, http_client=http)) as client:
            yield _McpSession(client)


class InProcessToolGateway:
    def __init__(self, service: ToolService) -> None:
        self.service = service

    @asynccontextmanager
    async def session(self) -> AsyncIterator[ToolSession]:
        yield InProcessSession(self.service)
