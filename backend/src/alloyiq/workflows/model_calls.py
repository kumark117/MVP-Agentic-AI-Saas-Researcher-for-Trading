"""ModelCaller: every Claude call in a run goes through here (sections 8.5, 9.1; D038, D094-D096).

Before a call: cancel/deadline check, run and branch call limits, token budgets, the model
must be in the estimate's approved list, and the call's worst-case cost must fit the run's
remaining real-spend reservation. Then a call intent is written. After it: provider
request ID, model used, stop reason and measured usage are persisted, the calculated cost
is recorded against the real budget, and llm.completed is emitted.

Recovery: a completed call is never repeated; its stored response is reused. An intent
with no completion is ambiguous (unknown usage, platform-absorbed) and is retried only
under a new call key. Fallback: only on refusal or 529 overload, and only to the next
approved model, never with weakened instructions.
"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import replace
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from alloyiq.adapters.artifacts import ArtifactStore
from alloyiq.api.schemas.events import LlmCompleted, TokenCategoriesPayload
from alloyiq.application.budget import BudgetService
from alloyiq.billing.rates import RateCard, TokenUsage, price_usage
from alloyiq.domain.llm import LLMError, LLMGateway, LLMRequest, LLMResponse, ToolUse
from alloyiq.domain.money import per_mtok
from alloyiq.observability.tracing import TraceSink
from alloyiq.repositories.audit import security_event
from alloyiq.repositories.events import append_run_event
from alloyiq.repositories.usage import UsageRepository
from alloyiq.workflows.runner import RunHandle

CALL_TIMEOUT_S = 30.0
STREAM_TIMEOUT_S = 90.0
CHARS_PER_TOKEN_GUARD = Decimal("2.5")  # conservative pre-call guard only; never recorded


class ModelCallRefused(Exception):
    """A policy gate refused the call before it was made (budget, limits, model)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ModelCallFailed(Exception):
    def __init__(self, code: str, refusal_category: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.refusal_category = refusal_category


def _response_json(r: LLMResponse) -> bytes:
    return json.dumps(
        {
            "model_used": r.model_used,
            "stop_reason": r.stop_reason,
            "refusal_category": r.refusal_category,
            "text": r.text,
            "tool_uses": [{"id": t.id, "name": t.name, "input": t.input} for t in r.tool_uses],
            "content": r.content,
            "provider_request_id": r.provider_request_id,
            "message_id": r.message_id,
        },
        default=str,
    ).encode()


def _response_from(data: bytes) -> LLMResponse:
    d = json.loads(data)
    return LLMResponse(
        model_used=d["model_used"],
        stop_reason=d["stop_reason"],
        refusal_category=d["refusal_category"],
        text=d["text"],
        tool_uses=[ToolUse(**t) for t in d["tool_uses"]],
        content=d["content"],
        usage=None,  # already recorded on the original call; never counted twice
        provider_request_id=d["provider_request_id"],
        message_id=d["message_id"],
        duration_ms=0,
        ttft_ms=None,
    )


class ModelCaller:
    def __init__(
        self,
        handle: RunHandle,
        gateway: LLMGateway,
        card: RateCard,
        budget: BudgetService,
        artifacts: ArtifactStore,
        *,
        usage: UsageRepository | None = None,
        trace: TraceSink | None = None,
    ) -> None:
        self.handle = handle
        self.gateway = gateway
        self.card = card
        self.budget = budget
        self.artifacts = artifacts
        self.usage = usage or UsageRepository()
        self.trace = trace
        limits = handle.ctx.limits
        tb = limits.get("token_budget", {})
        self.max_run_calls = int(limits.get("run_model_calls", 24))
        self.max_branch_calls = int(limits.get("branch_model_calls", 6))
        self.input_budget = int(tb.get("run_input_tokens", 150_000))
        self.output_budget = int(tb.get("run_output_tokens", 20_000))
        self._loaded = False
        self.calls = 0
        self.node_calls: dict[str, int] = {}
        self.input_used = 0
        self.output_used = 0
        self._lock = asyncio.Lock()

    async def _load(self) -> None:
        if self._loaded:
            return
        async with self.handle.tx() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT c.node_id, count(*) AS n,"
                        " coalesce(sum(u.input_tokens + coalesce(u.cache_read_input_tokens, 0)"
                        "  + coalesce(u.cache_creation_input_tokens, 0)), 0) AS i,"
                        " coalesce(sum(u.output_tokens), 0) AS o"
                        " FROM llm_calls c LEFT JOIN usage_records u ON u.llm_call_id = c.id"
                        " WHERE c.run_id = :r GROUP BY c.node_id"
                    ),
                    {"r": self.handle.ctx.run_id},
                )
            ).all()
        for r in rows:
            self.node_calls[r.node_id] = int(r.n)
            self.calls += int(r.n)
            self.input_used += int(r.i)
            self.output_used += int(r.o)
        self._loaded = True

    def _estimate_input_tokens(self, req: LLMRequest) -> int:
        chars = len(json.dumps([req.system, req.messages, req.tools], default=str))
        return math.ceil(Decimal(chars) / CHARS_PER_TOKEN_GUARD)

    def _worst_usd(self, model: str, req: LLMRequest, est_input: int) -> Decimal:
        rin = self.card.rate(model, "input")
        rout = self.card.rate(model, "output")
        if rin is None or rout is None:
            raise ModelCallRefused("model_unpriced")
        return per_mtok(est_input, rin) + per_mtok(req.max_tokens, rout)

    async def _gate(self, node_id: str, req: LLMRequest) -> int:
        self.handle.check()
        if req.model not in self.handle.ctx.approved_models:
            raise ModelCallRefused("model_not_approved")
        if self.calls >= self.max_run_calls:
            raise ModelCallRefused("run_model_call_limit")
        branch = node_id.endswith("_branch")
        if branch and self.node_calls.get(node_id, 0) >= self.max_branch_calls:
            raise ModelCallRefused("branch_model_call_limit")
        est = self._estimate_input_tokens(req)
        if self.input_used + est > self.input_budget:
            raise ModelCallRefused("input_token_budget")
        if self.output_used + req.max_tokens > self.output_budget:
            raise ModelCallRefused("output_token_budget")
        worst = self._worst_usd(req.model, req, est)
        async with self.handle.tx() as conn:
            if not await self.budget.authorize_call(conn, self.handle.ctx.run_id, worst):
                raise ModelCallRefused("real_spend_reservation")
        return est

    async def call(
        self,
        *,
        node_id: str,
        purpose: str,
        step: int,
        request: LLMRequest,
        billable: bool = True,
        agent: str | None = None,
    ) -> LLMResponse:
        await self._load()
        ctx = self.handle.ctx
        models = [request.model] + [m for m in ctx.approved_models if m != request.model]
        last_refusal: LLMResponse | None = None
        for i, model in enumerate(models):
            if i > 0 and model not in ctx.approved_models:
                break
            req = replace(
                request,
                model=model,
                cache=request.cache and self.card.caching_priced(model),
                metadata={**request.metadata, "run_id": str(ctx.run_id), "node_id": node_id},
            )
            base_key = f"{ctx.run_id}:{node_id}:{purpose}:{step}:{model}"
            reused, key = await self._reuse_or_key(base_key)
            if reused is not None:
                resp = reused
            else:
                async with self._lock:
                    await self._gate(node_id, req)
                    self.calls += 1
                    self.node_calls[node_id] = self.node_calls.get(node_id, 0) + 1
                resp_or_err = await self._invoke(node_id, purpose, step, key, req, billable)
                if isinstance(resp_or_err, LLMError):
                    if resp_or_err.code == "overloaded" and i + 1 < len(models):
                        continue  # approved fallback on overload
                    raise ModelCallFailed(resp_or_err.code)
                resp = resp_or_err
                if self.trace is not None:
                    u = resp.usage
                    s = self.trace.span(f"claude:{purpose}", "llm", node_id=node_id, model=model)
                    self.trace.finish(
                        s,
                        model_used=resp.model_used,
                        stop_reason=resp.stop_reason,
                        usage_status="actual" if u else "unknown",
                        input_tokens=u.input_tokens if u else None,
                        output_tokens=u.output_tokens if u else None,
                        duration_ms=resp.duration_ms,
                        provider_request_id=resp.provider_request_id,
                    )
            if resp.stop_reason == "refusal":
                last_refusal = resp
                async with self.handle.tx() as conn:
                    await security_event(
                        conn,
                        kind="llm.refusal",
                        severity="low",
                        account_id=ctx.account_id,
                        run_id=ctx.run_id,
                        subject_id=node_id,
                        metadata={"model": resp.model_used, "category": resp.refusal_category},
                    )
                await self.handle.activity(
                    agent,
                    "model_refused",
                    f"The model declined ({resp.refusal_category or 'no category'}).",
                )
                continue  # the same request (never weakened) on the next approved model
            return resp
        if last_refusal is not None:
            raise ModelCallFailed("refusal", last_refusal.refusal_category)
        raise ModelCallFailed("no_model_available")

    async def _reuse_or_key(self, base_key: str) -> tuple[LLMResponse | None, str]:
        """A completed record is reused; a dangling intent becomes ambiguous (new key)."""
        async with self.handle.tx() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT call_key, status FROM llm_calls WHERE run_id = :r"
                        " AND (call_key = :k OR starts_with(call_key, :p)) ORDER BY started_at"
                    ),
                    {"r": self.handle.ctx.run_id, "k": base_key, "p": base_key + ":r"},
                )
            ).all()
            for row in rows:
                if row.status == "completed":
                    data = await self.artifacts.get(conn, f"llm:{row.call_key}")
                    if data is not None:
                        return _response_from(data), row.call_key
        if not rows:
            return None, base_key
        return None, f"{base_key}:r{self.handle.ctx.generation}.{len(rows)}"

    async def _invoke(
        self,
        node_id: str,
        purpose: str,
        step: int,
        key: str,
        req: LLMRequest,
        billable: bool,
    ) -> LLMResponse | LLMError:
        ctx = self.handle.ctx
        async with self.handle.tx() as conn:
            rec = await self.usage.intent(
                conn,
                {
                    "account_id": ctx.account_id,
                    "run_id": ctx.run_id,
                    "node_id": node_id,
                    "attempt": step + 1,
                    "generation": ctx.generation,
                    "call_key": key,
                    "purpose": purpose,
                    "request_fingerprint": _fingerprint(req),
                    "model_requested": req.model,
                    "effort": req.effort,
                    "max_tokens": req.max_tokens,
                    "billable": billable,
                    "nonbillable_reason": None if billable else purpose,
                },
            )
            await self.usage.mark_in_flight(conn, rec.id)
        timeout = min(STREAM_TIMEOUT_S if req.stream else CALL_TIMEOUT_S, self.handle.remaining_s())
        attempt = 0
        while True:
            try:
                resp = await self.gateway.complete(req, timeout_s=max(1.0, timeout))
                break
            except LLMError as err:
                retry = (
                    err.retryable_before_response
                    and err.code in ("rate_limited", "unavailable")
                    and attempt == 0
                    and self.handle.remaining_s() > 5
                )
                if retry:  # D095: before any bytes, once, honouring retry-after
                    attempt += 1
                    await asyncio.sleep(min(err.retry_after_s or 1.0, 5.0))
                    self.handle.check()
                    continue
                definitive = err.code in (
                    "bad_request",
                    "rate_limited",
                    "overloaded",
                    "not_configured",
                )
                async with self.handle.tx() as conn:
                    if definitive:
                        await conn.execute(
                            text(
                                "UPDATE llm_calls SET status = 'failed', error_code = :e,"
                                " ended_at = now(), billable = false,"
                                " nonbillable_reason = 'rejected_before_processing' WHERE id = :i"
                            ),
                            {"i": rec.id, "e": err.code},
                        )
                    else:  # the provider may have processed it: unknown, never zero
                        await self.usage.complete(
                            conn,
                            rec.id,
                            account_id=ctx.account_id,
                            run_id=ctx.run_id,
                            status="ambiguous",
                            model_used=None,
                            provider_request_id=err.provider_request_id,
                            provider_message_id=None,
                            stop_reason=None,
                            refusal_category=None,
                            error_code=err.code,
                            duration_ms=None,
                            time_to_first_token_ms=None,
                            usage=None,
                            cost=price_usage(self.card, req.model, None, billable=False),
                            rate_card_version=self.card.version,
                            billable=False,
                        )
                return err
        cost = price_usage(self.card, resp.model_used, resp.usage, billable=billable)
        async with self.handle.tx() as conn:
            await self.usage.complete(
                conn,
                rec.id,
                account_id=ctx.account_id,
                run_id=ctx.run_id,
                status="completed",
                model_used=resp.model_used,
                provider_request_id=resp.provider_request_id,
                provider_message_id=resp.message_id,
                stop_reason=resp.stop_reason,
                refusal_category=resp.refusal_category,
                error_code=None,
                duration_ms=resp.duration_ms,
                time_to_first_token_ms=resp.ttft_ms,
                usage=resp.usage,
                cost=cost,
                rate_card_version=self.card.version,
                billable=billable,
            )
            known = sum((ln.provider_usd or Decimal(0) for ln in cost.lines), Decimal(0))
            await self.budget.record_spend(conn, ctx.run_id, known)
            await self.artifacts.put(
                conn,
                account_id=ctx.account_id,
                key=f"llm:{key}",
                data=_response_json(resp),
                content_type="application/json",
            )
            if resp.ttft_ms is not None:
                await conn.execute(
                    text(
                        "UPDATE research_runs SET time_to_first_model_token_ms = coalesce("
                        "time_to_first_model_token_ms, (extract(epoch FROM (now() - submitted_at))"
                        " * 1000)::bigint) WHERE id = :r"
                    ),
                    {"r": ctx.run_id},
                )
            u = resp.usage
            await append_run_event(
                conn,
                ctx.account_id,
                ctx.run_id,
                "llm.completed",
                LlmCompleted(
                    node_id=node_id,
                    call_id=rec.id,
                    model_id=resp.model_used,
                    duration_ms=resp.duration_ms,
                    usage_status="actual" if u else "unknown",
                    token_categories=TokenCategoriesPayload(
                        input_tokens=u.input_tokens,
                        output_tokens=u.output_tokens,
                        cache_read_input_tokens=u.cache_read_input_tokens,
                        cache_creation_input_tokens=u.cache_creation_input_tokens,
                    )
                    if u
                    else None,
                    stop_reason=resp.stop_reason,
                ),
            )
        self._account(resp.usage)
        return resp

    def _account(self, u: TokenUsage | None) -> None:
        if u is None:
            return
        self.input_used += (
            (u.input_tokens or 0)
            + (u.cache_read_input_tokens or 0)
            + (u.cache_creation_input_tokens or 0)
        )
        self.output_used += u.output_tokens or 0


def _fingerprint(req: LLMRequest) -> str:
    import hashlib

    body: dict[str, Any] = {
        "model": req.model,
        "system": req.system,
        "messages": req.messages,
        "tools": req.tools,
        "schema": req.output_schema,
        "effort": req.effort,
        "max_tokens": req.max_tokens,
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
