"""A scriptable fake workflow for P5 tests (build prompt P5: "exercised with a fake graph").

Usage is SYNTHETIC test data written through the real call-intent protocol, so settlement,
recovery and cancellation are exercised exactly as the real graph will drive them.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from alloyiq.billing.rates import RateCard, TokenUsage, price_usage
from alloyiq.repositories.catalog import CatalogRepository
from alloyiq.repositories.usage import UsageRepository
from alloyiq.workflows.runner import RunHandle, RunResult

SYNTHETIC_USAGE = TokenUsage(input_tokens=10_000, output_tokens=1_000)


async def fake_call(
    handle: RunHandle,
    node: str,
    step: int,
    *,
    usage: TokenUsage | None = SYNTHETIC_USAGE,
    delay: float = 0.0,
    billable: bool = True,
    crash_after_intent: bool = False,
) -> bool:
    """One model call. Returns False if a completed call with this key was reused."""
    repo = UsageRepository()
    handle.check()  # no new call may start after cancel/deadline/lost authority
    key = f"{handle.ctx.run_id}:{node}:1:{step}"
    model = handle.ctx.approved_models[0] if handle.ctx.approved_models else "claude-opus-5-5"
    async with handle.tx() as conn:
        if await repo.completed_result(conn, key):
            return False
        rec = await repo.intent(
            conn,
            {
                "account_id": handle.ctx.account_id,
                "run_id": handle.ctx.run_id,
                "node_id": node,
                "attempt": 1,
                "generation": handle.ctx.generation,
                "call_key": key,
                "purpose": "branch",
                "request_fingerprint": hashlib.sha256(key.encode()).hexdigest(),
                "model_requested": model,
                "effort": "low",
                "max_tokens": 2000,
                "billable": billable,
                "nonbillable_reason": None if billable else "test",
            },
        )
        card_row = await CatalogRepository().current_rate_card(conn)
    if crash_after_intent:
        raise asyncio.CancelledError  # the worker "dies" mid-call
    await asyncio.sleep(delay)
    assert card_row is not None
    card = RateCard.from_row(card_row)
    async with handle.tx() as conn:
        await repo.complete(
            conn,
            rec.id,
            account_id=handle.ctx.account_id,
            run_id=handle.ctx.run_id,
            status="completed",
            model_used=model,
            provider_request_id=f"synthetic-{step}",
            provider_message_id=None,
            stop_reason="end_turn",
            refusal_category=None,
            error_code=None,
            duration_ms=int(delay * 1000),
            time_to_first_token_ms=None,
            usage=usage,
            cost=price_usage(card, model, usage, billable=billable),
            rate_card_version=card.version,
            billable=billable,
        )
    return True


Step = Callable[[RunHandle], Awaitable[None]]


@dataclass
class FakeWorkflow:
    """Runs `nodes` in order; each node may make one synthetic call."""

    nodes: list[str] = field(default_factory=lambda: ["validate_request", "price_branch"])
    outcome: str = "succeeded"
    calls_per_node: int = 1
    delay: float = 0.0
    before_finish: Step | None = None
    calls_started_after_cancel: int = 0
    executions: int = 0
    cancel_seen: list[datetime] = field(default_factory=list)

    async def execute(self, handle: RunHandle) -> RunResult:
        self.executions += 1
        for node in self.nodes:
            if await handle.completed_output(node):
                continue  # recovery: reuse completed node output
            await handle.node_started(node)
            for i in range(self.calls_per_node):
                if handle.cancelled:
                    self.calls_started_after_cancel += 1
                await fake_call(handle, node, i, delay=self.delay)
            await handle.node_finished(node, output_ref=f"synthetic:{node}")
            await handle.activity(node, "completed", f"{node} finished (synthetic test data)")
        if self.before_finish:
            await self.before_finish(handle)
        return RunResult(self.outcome)  # type: ignore[arg-type]


@dataclass
class ParallelBranches:
    """Three branches calling in parallel until cancelled; counts calls after the flag."""

    per_call_delay: float = 0.05
    calls_after_flag: int = 0
    total_calls: int = 0

    async def execute(self, handle: RunHandle) -> RunResult:
        async def branch(name: str) -> None:
            await handle.node_started(name)
            for step in range(1000):
                flagged_before_start = handle.cancelled
                await fake_call(handle, name, step, delay=self.per_call_delay)
                self.total_calls += 1
                if flagged_before_start:
                    self.calls_after_flag += 1  # must stay 0: check() refuses new calls

        await asyncio.gather(
            branch("price_branch"), branch("news_branch"), branch("technical_branch")
        )
        return RunResult("succeeded")
