"""LLMGateway port (D028, D095; build prompt section 9.1). No I/O here.

The gateway reports exactly what the provider returned: model used, stop reason, refusal
category, measured usage (or None when unknown) and the provider request ID. Retry and
fallback policy live in the caller (workflows.model_calls), never inside the SDK.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from alloyiq.billing.rates import TokenUsage

Effort = Literal["low", "medium", "high", "xhigh", "max"]
LLMErrorCode = Literal[
    "overloaded",  # 529
    "rate_limited",  # 429
    "timeout",
    "unavailable",  # connection / 5xx
    "bad_request",  # 400-class: never retried
    "interrupted",  # a stream broke after bytes arrived: usage unknown
    "not_configured",
]


@dataclass(frozen=True, slots=True)
class LLMRequest:
    model: str
    max_tokens: int
    system: list[dict[str, Any]]  # stable, versioned blocks (cacheable prefix)
    messages: list[dict[str, Any]]
    effort: Effort
    tools: list[dict[str, Any]] = field(default_factory=list)
    output_schema: dict[str, Any] | None = None
    stream: bool = False
    cache: bool = False  # only when the pinned rate card prices cache read and write
    metadata: dict[str, str] = field(default_factory=dict)  # run_id, node_id, call_key


@dataclass(frozen=True, slots=True)
class ToolUse:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LLMResponse:
    model_used: str
    stop_reason: str | None
    refusal_category: str | None
    text: str | None
    tool_uses: list[ToolUse]
    content: list[dict[str, Any]]  # assistant content to append verbatim on the next turn
    usage: TokenUsage | None
    provider_request_id: str | None
    message_id: str | None
    duration_ms: int
    ttft_ms: int | None


class LLMError(Exception):
    def __init__(
        self,
        code: LLMErrorCode,
        message: str,
        *,
        response_started: bool = False,
        retry_after_s: float | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code: LLMErrorCode = code
        self.response_started = response_started
        self.retry_after_s = retry_after_s
        self.provider_request_id = provider_request_id

    @property
    def retryable_before_response(self) -> bool:
        """D095: only 429/529/connection failures before any response bytes."""
        return not self.response_started and self.code in (
            "overloaded",
            "rate_limited",
            "unavailable",
        )


class LLMGateway(Protocol):
    async def complete(self, request: LLMRequest, *, timeout_s: float) -> LLMResponse: ...
