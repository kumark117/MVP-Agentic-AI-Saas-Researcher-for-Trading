"""ToolCaller and PolicyEngine for MCP tool use inside a run (D035-D044).

The model chooses only *which* allowlisted tool to call (and an optional news focus);
the server builds every argument from the run's immutable request (instrument, horizon),
so neither the model nor the browser can pick a provider symbol or URL. Each result is
validated, normalised into evidence with server-assigned IDs, and persisted; injection
hits are quarantined with a security event.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import ValidationError

from alloyiq.adapters.artifacts import ArtifactStore
from alloyiq.api.schemas.events import ToolCompleted, ToolFailed, ToolStarted
from alloyiq.repositories.audit import security_event
from alloyiq.repositories.events import append_run_event
from alloyiq.repositories.evidence import EvidenceRepository
from alloyiq.security import injection
from alloyiq.tools.gateway import ToolSession
from alloyiq.tools.schemas import TOOL_DEADLINE_S, TOOL_VERSIONS, FxArgs, InstrumentArgs, NewsArgs
from alloyiq.workflows.evidence import (
    VALIDATOR_VERSION,
    EvidenceItem,
    model_view,
    normalise,
    validate_envelope,
)
from alloyiq.workflows.runner import RunHandle

POLICY_VERSION = "tool-policy-1"


class PolicyEngine:
    """Tool authorisation independent of model text (D035, D093)."""

    def __init__(self, allowed: dict[str, frozenset[str]]) -> None:
        self.allowed = allowed

    def authorise(self, node_id: str, tool: str) -> bool:
        return tool in self.allowed.get(node_id, frozenset())


@dataclass
class ToolOutcome:
    tool: str
    ok: bool
    evidence: list[EvidenceItem] = field(default_factory=list)
    model_text: str = ""
    error: str | None = None


class ToolCallRefused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ToolCaller:
    def __init__(
        self,
        handle: RunHandle,
        session: ToolSession,
        policy: PolicyEngine,
        artifacts: ArtifactStore,
        *,
        repo: EvidenceRepository | None = None,
    ) -> None:
        self.handle = handle
        self.session = session
        self.policy = policy
        self.artifacts = artifacts
        self.repo = repo or EvidenceRepository()
        limits = handle.ctx.limits
        self.max_run = int(limits.get("run_tool_calls", 30))
        self.max_branch = int(limits.get("branch_tool_calls", 10))
        self._counts: dict[str, int] | None = None
        self._lock = asyncio.Lock()

    def _args(self, tool: str, call_id: uuid.UUID, model_args: dict[str, Any]) -> dict[str, Any]:
        ctx = self.handle.ctx
        if tool == "get_fx_rate":
            return FxArgs(tool_call_id=call_id).model_dump(mode="json")
        ins, horizon = ctx.instrument_id, ctx.horizon
        if tool == "search_news":
            focus = model_args.get("focus")
            try:
                return NewsArgs(
                    tool_call_id=call_id,
                    instrument_id=ins,
                    horizon=horizon,  # type: ignore[arg-type]
                    focus=str(focus) if focus else None,
                ).model_dump(mode="json")
            except ValidationError:  # an unsafe focus is dropped, never forwarded
                return NewsArgs(
                    tool_call_id=call_id,
                    instrument_id=ins,
                    horizon=horizon,  # type: ignore[arg-type]
                ).model_dump(mode="json")
        return InstrumentArgs(
            tool_call_id=call_id,
            instrument_id=ins,
            horizon=horizon,  # type: ignore[arg-type]
        ).model_dump(mode="json")

    async def call(
        self, node_id: str, tool: str, model_args: dict[str, Any], *, step: int, index: int
    ) -> ToolOutcome:
        ctx = self.handle.ctx
        if not self.policy.authorise(node_id, tool):
            async with self.handle.tx() as conn:
                await self.repo.validation(
                    conn,
                    account_id=ctx.account_id,
                    run_id=ctx.run_id,
                    boundary="tool_policy",
                    subject_id=f"{node_id}:{tool}",
                    verdict="fail",
                    reasons=["tool_not_allowlisted"],
                    policy_version=POLICY_VERSION,
                )
                await security_event(
                    conn,
                    kind="tool.not_allowed",
                    severity="medium",
                    account_id=ctx.account_id,
                    run_id=ctx.run_id,
                    subject_id=node_id,
                    metadata={"tool": tool[:60]},
                )
            return ToolOutcome(
                tool,
                False,
                error="TOOL_NOT_ALLOWED",
                model_text=f"Tool {tool!r} is not available to this agent.",
            )
        key = f"{ctx.run_id}:{node_id}:tool:{step}:{index}:{tool}"
        reused = await self._reuse(key)
        if reused is not None:
            return reused
        async with self._lock:
            if self._counts is None:
                async with self.handle.tx() as conn:
                    self._counts = await self.repo.count_tool_calls(conn, ctx.run_id)
            if sum(self._counts.values()) >= self.max_run:
                raise ToolCallRefused("run_tool_call_limit")
            if self._counts.get(node_id, 0) >= self.max_branch:
                raise ToolCallRefused("branch_tool_call_limit")
            self.handle.check()
            self._counts[node_id] = self._counts.get(node_id, 0) + 1
        call_id = uuid.uuid4()
        args = self._args(tool, call_id, model_args)
        args_hash = hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()
        async with self.handle.tx() as conn:
            await self.repo.tool_intent(
                conn,
                {
                    "id": call_id,
                    "account_id": ctx.account_id,
                    "run_id": ctx.run_id,
                    "node_id": node_id,
                    "call_key": key,
                    "generation": ctx.generation,
                    "tool_name": tool,
                    "tool_version": TOOL_VERSIONS.get(tool, "unknown"),
                    "args_hash": args_hash,
                },
            )
            await append_run_event(
                conn,
                ctx.account_id,
                ctx.run_id,
                "tool.started",
                ToolStarted(
                    node_id=node_id,
                    tool_call_id=call_id,
                    tool_name=tool,
                    argument_summary=_arg_summary(tool, args),
                ),
            )
        started = time.monotonic()
        try:
            env = await asyncio.wait_for(self.session.call(tool, args), TOOL_DEADLINE_S + 2)
        except TimeoutError:
            env = None
        duration = int((time.monotonic() - started) * 1000)
        now = datetime.now(UTC)
        if env is None or env.status != "ok":
            code = env.error.code if env and env.error else "PROVIDER_UNAVAILABLE"
            async with self.handle.tx() as conn:
                await self.repo.tool_finish(
                    conn,
                    call_id,
                    status="failed",
                    duration_ms=duration,
                    result_ref=None,
                    error_code=code,
                )
                await append_run_event(
                    conn,
                    ctx.account_id,
                    ctx.run_id,
                    "tool.failed",
                    ToolFailed(
                        node_id=node_id,
                        tool_call_id=call_id,
                        tool_name=tool,
                        duration_ms=duration,
                        error_code=code,
                        retry=0,
                    ),
                )
            outcome = ToolOutcome(
                tool,
                False,
                error=code,
                model_text=f"Tool {tool} failed: {code}. No data was returned.",
            )
            await self._store(key, outcome)
            return outcome
        verdict = validate_envelope(
            env,
            tool=tool,
            instrument=ctx.instrument_snapshot | {"instrument_id": str(ctx.instrument_id)},
            now=now,
        )
        items = normalise(env, tool, now) if verdict.passed else []
        async with self.handle.tx() as conn:
            await self.repo.validation(
                conn,
                account_id=ctx.account_id,
                run_id=ctx.run_id,
                boundary="tool_envelope",
                subject_id=str(call_id),
                verdict=verdict.verdict,
                reasons=list(verdict.reasons),
                policy_version=VALIDATOR_VERSION,
            )
            for item in items:
                await self.repo.insert_evidence(
                    conn,
                    account_id=ctx.account_id,
                    run_id=ctx.run_id,
                    instrument_id=ctx.instrument_id if item.kind != "fx" else None,
                    item=item,
                )
                if item.quarantined:
                    await self.repo.validation(
                        conn,
                        account_id=ctx.account_id,
                        run_id=ctx.run_id,
                        boundary="untrusted_text",
                        subject_id=item.evidence_id,
                        verdict="quarantine",
                        reasons=item.taint_flags,
                        policy_version=injection.POLICY_VERSION,
                    )
                    await security_event(
                        conn,
                        kind="injection.quarantined",
                        severity="medium",
                        account_id=ctx.account_id,
                        run_id=ctx.run_id,
                        subject_id=item.evidence_id,
                        metadata={
                            "flags": item.taint_flags,
                            "source_domain": item.payload.get("source_domain"),
                        },
                    )
            status = "completed" if verdict.passed else "failed"
            await self.repo.tool_finish(
                conn,
                call_id,
                status=status,
                duration_ms=duration,
                result_ref=f"tool:{key}",
                error_code=None if verdict.passed else "VALIDATION_FAILED",
            )
            validation_status: Literal["passed", "failed", "quarantined"] = (
                "failed"
                if not verdict.passed
                else "quarantined"
                if any(i.quarantined for i in items)
                else "passed"
            )
            await append_run_event(
                conn,
                ctx.account_id,
                ctx.run_id,
                "tool.completed",
                ToolCompleted(
                    node_id=node_id,
                    tool_call_id=call_id,
                    tool_name=tool,
                    duration_ms=duration,
                    observation_summary=_observation(tool, items, verdict.reasons),
                    evidence_ids=[uuid.UUID(i.evidence_id) for i in items],
                    validation_status=validation_status,
                ),
            )
        if verdict.passed:
            text = "\n".join(model_view(i) for i in items) or "No items returned."
            if env.warnings:
                text += "\nWarnings: " + "; ".join(env.warnings[:5])
            outcome = ToolOutcome(tool, True, items, text)
        else:
            outcome = ToolOutcome(
                tool,
                False,
                error="VALIDATION_FAILED",
                model_text=f"Tool {tool} returned data that failed validation "
                f"({', '.join(verdict.reasons)}); it must not be used.",
            )
        await self._store(key, outcome)
        return outcome

    async def _store(self, key: str, outcome: ToolOutcome) -> None:
        data = json.dumps(
            {
                "tool": outcome.tool,
                "ok": outcome.ok,
                "error": outcome.error,
                "model_text": outcome.model_text,
                "evidence": [i.to_dict() for i in outcome.evidence],
            },
            default=str,
        ).encode()
        async with self.handle.tx() as conn:
            await self.artifacts.put(
                conn,
                account_id=self.handle.ctx.account_id,
                key=f"tool:{key}",
                data=data,
                content_type="application/json",
            )

    async def _reuse(self, key: str) -> ToolOutcome | None:
        async with self.handle.tx() as conn:
            data = await self.artifacts.get(conn, f"tool:{key}")
        if data is None:
            return None
        d = json.loads(data)
        return ToolOutcome(
            d["tool"],
            d["ok"],
            [EvidenceItem.from_dict(e) for e in d["evidence"]],
            d["model_text"],
            d["error"],
        )


def _arg_summary(tool: str, args: dict[str, Any]) -> str:
    if tool == "search_news":
        focus = args.get("focus") or "none"
        return f"news for the run's instrument ({args.get('horizon')}), focus: {focus}"
    if tool == "get_fx_rate":
        return "USD to INR reference rate"
    return f"{tool.replace('_', ' ')} for the run's instrument ({args.get('horizon')})"


def _observation(tool: str, items: list[EvidenceItem], reasons: tuple[str, ...]) -> str:
    if reasons:
        return f"Rejected by validation: {', '.join(reasons)}"[:500]
    if tool == "get_quote" and items:
        p = items[0].payload
        return (
            f"Quote {p['price']} {items[0].currency}/{items[0].unit} ({items[0].freshness_state})"
        )
    if tool == "search_news":
        q = sum(1 for i in items if i.quarantined)
        return f"{len(items)} articles" + (f", {q} quarantined by the security policy" if q else "")
    if tool == "get_technical_indicators":
        return f"{len(items)} deterministic indicator values"
    if tool == "get_fx_rate" and items:
        return f"USD/INR {items[0].payload['rate']} (for converted display only)"
    if tool == "get_history" and items:
        return f"{items[0].payload['count']} bars ({items[0].payload['interval']})"
    return f"{len(items)} items"
