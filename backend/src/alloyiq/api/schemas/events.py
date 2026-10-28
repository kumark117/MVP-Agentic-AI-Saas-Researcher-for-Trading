"""SSE event envelope and payloads (D084-D090; build prompt section 14).

Event IDs are "{scope_uuid}:{seq}". Payloads are at most 32 KB and never contain hidden
model reasoning, credentials or another account's data. Every billing-relevant payload
carries the simulation discriminator (D113).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from alloyiq.api.schemas.common import BillingStatus, Money, RunStatus, SimulationFields

SCHEMA_VERSION: Literal["1.0"] = "1.0"
MAX_PAYLOAD_BYTES = 32 * 1024


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ------------------------------------------------------------------ run stream payloads
class RunQueued(_Payload):
    status: Literal["queued"] = "queued"
    billing_status: BillingStatus
    reserved_amount: Money


class RunStarted(_Payload):
    started_at: datetime
    queue_ms: int
    recovering: bool = False


class NodeStarted(_Payload):
    node_id: str
    attempt: int


class ToolStarted(_Payload):
    node_id: str
    tool_call_id: UUID
    tool_name: str
    argument_summary: str


class ToolCompleted(_Payload):
    node_id: str
    tool_call_id: UUID
    tool_name: str
    duration_ms: int
    observation_summary: str
    evidence_ids: list[UUID]
    validation_status: Literal["passed", "failed", "quarantined"]


class ToolFailed(_Payload):
    node_id: str
    tool_call_id: UUID
    tool_name: str
    duration_ms: int | None
    error_code: str
    retry: int


class TokenCategoriesPayload(_Payload):
    input_tokens: int | None
    output_tokens: int | None
    cache_read_input_tokens: int | None
    cache_creation_input_tokens: int | None


class LlmCompleted(_Payload):
    node_id: str
    call_id: UUID
    provider: Literal["anthropic"] = "anthropic"
    model_id: str | None
    duration_ms: int | None
    usage_status: Literal["actual", "unknown", "estimated"]
    token_categories: TokenCategoriesPayload | None
    stop_reason: str | None


class ValidationCompleted(_Payload):
    boundary: str
    subject_id: str
    verdict: Literal["pass", "fail", "quarantine"]
    reasons: list[str]


class ActivityAppended(_Payload):
    agent: str | None
    action: str
    tool_name: str | None = None
    summary: str = Field(max_length=2000)
    elapsed_ms: int | None = None
    retry: int = 0
    validation_status: str | None = None


class RunCancelRequested(_Payload):
    cancel_requested_at: datetime
    notice: str = "Provider work already started may still incur real platform cost."


class ReportReady(_Payload):
    report_id: UUID
    signal: Literal["BUY", "SELL", "HOLD", "NO_SIGNAL"]
    complete: bool


class BillingUpdated(SimulationFields):
    model_config = ConfigDict(extra="forbid", frozen=True)

    billing_status: BillingStatus
    settled_charge: Money | None
    released_amount: Money
    pending_amount: Money


class RunFinished(_Payload):
    status: RunStatus
    terminal_at: datetime
    report_available: bool
    billing_status: BillingStatus
    outcome_reason: str | None


class StreamResetRequired(_Payload):
    reason: Literal["buffer_overflow", "retention_expired", "server_restart"]
    latest_event_id: str | None


# ------------------------------------------------------------------ account stream payloads
class RunUpdated(_Payload):
    run_id: UUID
    status: RunStatus
    billing_status: BillingStatus


class BalanceUpdated(SimulationFields):
    model_config = ConfigDict(extra="forbid", frozen=True)

    available: Money
    reserved: Money
    pending_settlement: Money


class ExportReady(_Payload):
    export_id: UUID
    status: Literal["ready", "failed"]


class SessionRevoked(_Payload):
    session_id: UUID
    reason: str


RUN_EVENTS: dict[str, type[BaseModel]] = {
    "run.queued": RunQueued,
    "run.started": RunStarted,
    "node.started": NodeStarted,
    "tool.started": ToolStarted,
    "tool.completed": ToolCompleted,
    "tool.failed": ToolFailed,
    "llm.completed": LlmCompleted,
    "validation.completed": ValidationCompleted,
    "activity.appended": ActivityAppended,
    "run.cancel_requested": RunCancelRequested,
    "report.ready": ReportReady,
    "billing.updated": BillingUpdated,
    "run.finished": RunFinished,
    "stream.reset_required": StreamResetRequired,
}

ACCOUNT_EVENTS: dict[str, type[BaseModel]] = {
    "run.updated": RunUpdated,
    "balance.updated": BalanceUpdated,
    "export.ready": ExportReady,
    "security.session_revoked": SessionRevoked,
    "stream.reset_required": StreamResetRequired,
}


class EventEnvelope(BaseModel):
    """The JSON carried in an SSE `data:` field (D085)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    event_id: str = Field(pattern=r"^[0-9a-f-]{36}:\d+$")
    sequence: int = Field(ge=1)
    run_id: UUID | None = None
    occurred_at: datetime
    request_id: str | None = None
    payload: dict[str, object]


def validate_payload(
    scope: Literal["run", "account"], event_type: str, payload: object
) -> dict[str, object]:
    """Validate and normalise an event payload; raises on unknown type or bad shape."""
    registry = RUN_EVENTS if scope == "run" else ACCOUNT_EVENTS
    model = registry.get(event_type)
    if model is None:
        raise ValueError(f"unknown {scope} event type: {event_type}")
    obj = payload if isinstance(payload, model) else model.model_validate(payload)
    data: dict[str, object] = obj.model_dump(mode="json")
    import json

    if len(json.dumps(data, separators=(",", ":")).encode()) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"{event_type} payload exceeds 32 KB")
    return data
