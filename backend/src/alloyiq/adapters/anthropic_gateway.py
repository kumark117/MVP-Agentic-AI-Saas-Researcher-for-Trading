"""AnthropicLLMGateway: Claude through the official SDK (build prompt section 9.1; D095).

Request rules for the current models (Opus 5.5, Sonnet 5.5, Haiku 5.5):
- adaptive thinking (never budget_tokens or disabled thinking); depth via output_config.effort,
  always set explicitly because model defaults differ;
- no temperature/top_p/top_k, no assistant prefill, no forced tool_choice (auto + strict tools);
- structured output via output_config.format (json_schema), re-validated by our models;
- max_retries=0: retry and fallback policy belong to the caller, which only retries before
  any response bytes and only on models in the estimate's approved list. The server-side
  `fallbacks` parameter is deliberately NOT used: it may route outside the approved set (D096).
"""

from __future__ import annotations

import time
from typing import Any

import anthropic

from alloyiq.billing.rates import TokenUsage
from alloyiq.domain.llm import LLMError, LLMRequest, LLMResponse, ToolUse
from alloyiq.observability.logging import get_logger

log = get_logger(__name__)


def _usage(u: Any) -> TokenUsage | None:
    if u is None or u.input_tokens is None or u.output_tokens is None:
        return None
    return TokenUsage(
        input_tokens=int(u.input_tokens),
        output_tokens=int(u.output_tokens),  # includes thinking tokens (billed as output)
        cache_read_input_tokens=int(u.cache_read_input_tokens or 0),
        cache_creation_input_tokens=int(u.cache_creation_input_tokens or 0),
    )


def to_response(
    msg: Any, *, request_id: str | None, started: float, ttft_ms: int | None
) -> LLMResponse:
    texts = [b.text for b in msg.content if b.type == "text"]
    tool_uses = [
        ToolUse(id=b.id, name=b.name, input=dict(b.input or {}))
        for b in msg.content
        if b.type == "tool_use"
    ]
    category = None
    if msg.stop_reason == "refusal" and getattr(msg, "stop_details", None) is not None:
        category = getattr(msg.stop_details, "category", None)
    return LLMResponse(
        model_used=msg.model,
        stop_reason=msg.stop_reason,
        refusal_category=category,
        text="".join(texts) if texts else None,
        tool_uses=tool_uses,
        content=[b.model_dump(mode="json", exclude_none=True) for b in msg.content],
        usage=_usage(msg.usage),
        provider_request_id=request_id,
        message_id=msg.id,
        duration_ms=int((time.monotonic() - started) * 1000),
        ttft_ms=ttft_ms,
    )


def _retry_after(exc: anthropic.APIStatusError) -> float | None:
    raw = exc.response.headers.get("retry-after") if exc.response is not None else None
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


class AnthropicLLMGateway:
    def __init__(self, api_key: str, *, client: anthropic.AsyncAnthropic | None = None) -> None:
        self.client = client or anthropic.AsyncAnthropic(api_key=api_key, max_retries=0)

    def _params(self, req: LLMRequest) -> dict[str, Any]:
        output_config: dict[str, Any] = {"effort": req.effort}
        if req.output_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": req.output_schema}
        params: dict[str, Any] = {
            "model": req.model,
            "max_tokens": req.max_tokens,
            "system": req.system,
            "messages": req.messages,
            "thinking": {"type": "adaptive"},
            "output_config": output_config,
            # Correlation without personal data (section 9.1): an opaque run reference.
            "metadata": {"user_id": req.metadata.get("run_id", "")[:64]},
        }
        if req.tools:
            params["tools"] = req.tools
            params["tool_choice"] = {"type": "auto"}
        if req.cache:
            params["cache_control"] = {"type": "ephemeral"}
        return params

    async def complete(self, request: LLMRequest, *, timeout_s: float) -> LLMResponse:
        params = self._params(request)
        client = self.client.with_options(timeout=max(1.0, timeout_s))
        started = time.monotonic()
        try:
            if not request.stream:
                msg = await client.messages.create(**params)
                return to_response(
                    msg, request_id=getattr(msg, "_request_id", None), started=started, ttft_ms=None
                )
            ttft: int | None = None
            got_bytes = False
            try:
                async with client.messages.stream(**params) as stream:
                    async for event in stream:
                        got_bytes = True
                        if ttft is None and event.type == "content_block_delta":
                            ttft = int((time.monotonic() - started) * 1000)
                    msg = await stream.get_final_message()
            except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
                if got_bytes:
                    # The final usage never arrived: unknown, never estimated (section 9.1).
                    raise LLMError("interrupted", str(exc), response_started=True) from exc
                raise
            return to_response(
                msg, request_id=getattr(msg, "_request_id", None), started=started, ttft_ms=ttft
            )
        except LLMError:
            raise
        except anthropic.APITimeoutError as exc:
            raise LLMError("timeout", "model call timed out") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("unavailable", "could not reach the model provider") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("rate_limited", "rate limited", retry_after_s=_retry_after(exc)) from exc
        except anthropic.OverloadedError as exc:
            raise LLMError(
                "overloaded", "provider overloaded", retry_after_s=_retry_after(exc)
            ) from exc
        except anthropic.InternalServerError as exc:
            raise LLMError("unavailable", f"provider error {exc.status_code}") from exc
        except anthropic.APIStatusError as exc:
            # Provider error text names the invalid field; it never echoes our key.
            log.warning(
                "llm_request_rejected", status=exc.status_code, message=str(exc.message)[:300]
            )
            raise LLMError(
                "bad_request",
                f"request rejected ({exc.status_code})",
                provider_request_id=exc.response.headers.get("request-id")
                if exc.response is not None
                else None,
            ) from exc
