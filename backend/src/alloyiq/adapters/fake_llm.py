"""FakeLLMGateway: a scenario-driven stand-in for Claude in tests (build prompt section 18).

It answers like the real agents would, from what is actually in the request (evidence ids
and values from tool results), so the graph, validators and accounting run unchanged.
Every response and token count is SYNTHETIC and labelled so; it is never used in
production (build_workflow only wires the Anthropic gateway).
"""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from alloyiq.billing.rates import TokenUsage
from alloyiq.domain.llm import LLMError, LLMRequest, LLMResponse, ToolUse

SYNTHETIC_USAGE = TokenUsage(input_tokens=2_000, output_tokens=300)
_EVIDENCE = re.compile(r'<(untrusted_evidence|evidence) id="([0-9a-f-]{36})"[^>]*>(.*?)</\1>', re.S)


def _all_text(messages: list[dict[str, Any]]) -> str:
    out = []
    for m in messages:
        c = m["content"]
        if isinstance(c, str):
            out.append(c)
        else:
            for b in c:
                if b.get("type") == "tool_result":
                    out.append(
                        b["content"] if isinstance(b["content"], str) else json.dumps(b["content"])
                    )
                elif b.get("type") == "text":
                    out.append(b["text"])
    return "\n".join(out)


def _has_tool_results(messages: list[dict[str, Any]]) -> bool:
    return any(
        isinstance(m["content"], list) and any(b.get("type") == "tool_result" for b in m["content"])
        for m in messages
    )


@dataclass
class Scenario:
    """Switches for fault cases; all default to a clean run."""

    refuse_nodes: set[str] = field(default_factory=set)
    max_tokens_once: set[str] = field(default_factory=set)
    interrupt_nodes: set[str] = field(default_factory=set)
    overload_models: set[str] = field(default_factory=set)
    bad_numbers_first: bool = False  # first signal draft invents a number -> one repair
    always_bad_numbers: bool = False
    signal: str = "BUY"
    news_relevance: str = "high"
    request_unapproved_tool: bool = False


class FakeLLMGateway:
    def __init__(
        self,
        scenario: Scenario | None = None,
        handler: Callable[[LLMRequest], LLMResponse | LLMError] | None = None,
    ) -> None:
        self.scenario = scenario or Scenario()
        self.handler = handler
        self.requests: list[LLMRequest] = []
        self.calls: Counter[str] = Counter()

    def _resp(
        self,
        req: LLMRequest,
        *,
        text: str | None = None,
        tool_uses: list[ToolUse] | None = None,
        stop: str = "end_turn",
        refusal: str | None = None,
        usage: TokenUsage | None = SYNTHETIC_USAGE,
    ) -> LLMResponse:
        content: list[dict[str, Any]] = []
        if text is not None:
            content.append({"type": "text", "text": text})
        for t in tool_uses or []:
            content.append({"type": "tool_use", "id": t.id, "name": t.name, "input": t.input})
        return LLMResponse(
            model_used=req.model,
            stop_reason=stop,
            refusal_category=refusal,
            text=text,
            tool_uses=tool_uses or [],
            content=content,
            usage=usage,
            provider_request_id=f"synthetic-req-{uuid.uuid4().hex[:8]}",
            message_id=f"synthetic-msg-{uuid.uuid4().hex[:8]}",
            duration_ms=5,
            ttft_ms=3 if req.stream else None,
        )

    async def complete(self, request: LLMRequest, *, timeout_s: float) -> LLMResponse:
        self.requests.append(request)
        node = request.metadata.get("node_id", "")
        self.calls[node] += 1
        if self.handler is not None:
            out = self.handler(request)
            if isinstance(out, LLMError):
                raise out
            return out
        sc = self.scenario
        if request.model in sc.overload_models:
            raise LLMError("overloaded", "synthetic overload")
        if node in sc.refuse_nodes:
            return self._resp(request, stop="refusal", refusal="synthetic_policy", text=None)
        if node in sc.interrupt_nodes:
            raise LLMError("interrupted", "synthetic stream interruption", response_started=True)
        if node in sc.max_tokens_once and self.calls[node] == 1:
            return self._resp(request, text='{"summary": "cut', stop="max_tokens")
        if request.tools:
            return self._branch(request)
        return self._signal(request)

    def _branch(self, req: LLMRequest) -> LLMResponse:
        names = [t["name"] for t in req.tools]
        if not _has_tool_results(req.messages):
            first = names[0]
            uses = [
                ToolUse(
                    f"toolu_{uuid.uuid4().hex[:10]}",
                    first,
                    {"focus": ""} if first == "search_news" else {},
                )
            ]
            if self.scenario.request_unapproved_tool:
                uses.append(ToolUse(f"toolu_{uuid.uuid4().hex[:10]}", "get_account_balance", {}))
            return self._resp(req, tool_uses=uses, stop="tool_use")
        found = _EVIDENCE.findall(_all_text(req.messages))
        observations = [
            {"statement": "Synthetic observation of the cited evidence.", "evidence_ids": [eid]}
            for tag, eid, _ in found
            if tag == "evidence"
        ][:6]
        news = [
            {
                "evidence_id": eid,
                "relevance": self.scenario.news_relevance,
                "stance": "supporting",
                "excerpt": "synthetic excerpt",
            }
            for tag, eid, _ in found
            if tag == "untrusted_evidence"
        ]
        finding = {
            "summary": "Synthetic branch finding (test data).",
            "observations": observations
            or [
                {
                    "statement": "Synthetic observation.",
                    "evidence_ids": [e for _, e, _ in found][:1],
                }
            ],
            "stance": "bullish",
            "news_assessments": news if "search_news" in names else [],
        }
        return self._resp(req, text=json.dumps(finding))

    def _signal(self, req: LLMRequest) -> LLMResponse:
        text = _all_text(req.messages)
        found = _EVIDENCE.findall(text)
        quote = next(((eid, body) for tag, eid, body in found if '"price"' in body), None)
        ids = [eid for tag, eid, _ in found]
        price = json.loads(quote[1])["price"] if quote else None
        is_repair = "Validation failed" in text
        invent = self.scenario.always_bad_numbers or (
            self.scenario.bad_numbers_first and not is_repair
        )
        claim = f"The reference quote is {price}." if price else "The evidence is limited."
        if invent:
            claim = "The price will reach 98765.43 next week."
        out = {
            "signal": self.scenario.signal,
            "confidence_score": 0.4,
            "confidence_label": "low",
            "summary": "Synthetic report (test data). " + claim,
            "supporting_evidence": [
                {"claim": claim, "evidence_ids": [quote[0] if quote else ids[0]]}
            ]
            if ids
            else [],
            "opposing_evidence": [],
            "assumptions": ["Synthetic assumption."],
            "risks": ["Synthetic risk."],
            "limitations": ["Reference data, not a live exchange quote."],
        }
        return self._resp(req, text=json.dumps(out))
