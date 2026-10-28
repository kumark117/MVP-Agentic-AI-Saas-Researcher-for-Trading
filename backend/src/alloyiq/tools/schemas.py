"""MCP tool contracts, shared by the MCP server and the MCPToolGateway client (D035, D037).

Arguments name catalogue instrument IDs only; neither an LLM nor a browser can choose a
provider symbol or URL. Every result is a ToolEnvelope; failures carry a typed error and
no invented data.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

TOOL_SCHEMA_VERSION: Literal["1.0"] = "1.0"
TOOL_VERSIONS = {
    "get_quote": "1.0.0",
    "get_history": "1.0.0",
    "get_technical_indicators": "1.0.0",
    "get_fx_rate": "1.0.0",
    "search_news": "1.0.0",
}
MAX_TOOL_PAYLOAD_BYTES = 64 * 1024
TOOL_DEADLINE_S = 8.0

Horizon = Literal["intraday", "swing", "long_term"]
ToolErrorCode = Literal[
    "PROVIDER_UNAVAILABLE",
    "RATE_LIMITED",
    "PROVIDER_ERROR",
    "INVALID_RESPONSE",
    "NOT_CONFIGURED",
    "INSUFFICIENT_DATA",
    "UNSUPPORTED_INSTRUMENT",
    "VALIDATION_FAILED",
    "EGRESS_BLOCKED",
    "PAYLOAD_TOO_LARGE",
]
FreshnessState = Literal["fresh", "delayed", "stale", "market_closed", "unknown"]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_call_id: UUID


class InstrumentArgs(_Args):
    instrument_id: UUID
    horizon: Horizon


class FxArgs(_Args):
    base: Literal["USD"] = "USD"
    quote: Literal["INR"] = "INR"


class NewsArgs(InstrumentArgs):
    # Optional focus words appended to a server-built query; letters, digits and spaces only.
    focus: str | None = Field(default=None, max_length=80, pattern=r"^[A-Za-z0-9 ,.'-]*$")


class ToolError(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: ToolErrorCode
    message: str
    retryable: bool = False


class ToolEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = TOOL_SCHEMA_VERSION
    tool_call_id: UUID
    tool: str
    tool_version: str
    status: Literal["ok", "error"]
    data: dict[str, Any] | None = None
    error: ToolError | None = None
    source: str | None = None
    observed_at: datetime | None = None
    fetched_at: datetime
    freshness_state: FreshnessState = "unknown"
    market_state: Literal["open", "closed", "not_applicable", "unknown"] = "unknown"
    unit: str | None = None
    currency: str | None = None
    instrument_id: UUID | None = None
    data_mode: Literal["exchange_live", "exchange_delayed", "reference"] | None = None
    provenance_hash: str | None = None
    warnings: list[str] = Field(default_factory=list)
