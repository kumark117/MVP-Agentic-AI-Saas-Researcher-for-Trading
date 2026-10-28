"""The research graph (D032; build prompt section 9.5), as a LangGraph StateGraph with the
Postgres checkpointer (one thread per run: thread_id = run_id).

validate_request -> policy_check -> {price_branch, news_branch, technical_branch}
  -> merge_evidence -> handoff_validation -> evidence_minimum_gate
  -> (sufficient)   signal_synthesis -> final_validation -> persist_report
  -> (insufficient / blocked / invalid) persist_incomplete_report
Settlement and the terminal event follow in RunLifecycle once the executor returns.

Each branch writes only its own state key (no reducer can overwrite another branch).
Checkpoints hold only normalised evidence and findings; raw provider payloads live in the
ArtifactStore by key. Recovery resumes from the last checkpoint, and completed model and
tool calls inside a node are reused from their stored outputs, never repeated.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from sqlalchemy import text

from alloyiq.adapters.artifacts import ArtifactStore
from alloyiq.agents.branch import BRANCHES, AgentSpec, BranchAgent
from alloyiq.agents.schemas import SignalOutput
from alloyiq.agents.signal import REPORT_POLICY, ReportCheck, SignalAgent, assemble_report
from alloyiq.api.schemas.events import ReportReady, ValidationCompleted
from alloyiq.application.budget import BudgetService
from alloyiq.billing.rates import RateCard
from alloyiq.domain.llm import LLMGateway
from alloyiq.observability.tracing import TraceSink, make_sink
from alloyiq.repositories.catalog import CatalogRepository
from alloyiq.repositories.events import append_run_event
from alloyiq.repositories.evidence import EvidenceRepository
from alloyiq.tools.gateway import ToolSession
from alloyiq.workflows.evidence import EvidenceItem, evidence_minimum, handoff_checks
from alloyiq.workflows.model_calls import ModelCaller, ModelCallFailed, ModelCallRefused
from alloyiq.workflows.runner import RunHandle, RunResult
from alloyiq.workflows.tool_calls import PolicyEngine, ToolCaller

NODE_LABELS = {
    "validate_request": "Checking the request",
    "policy_check": "Applying the tool and budget policy",
    "price_branch": "Price agent",
    "news_branch": "News agent",
    "technical_branch": "Technical agent",
    "merge_evidence": "Merging evidence",
    "handoff_validation": "Validating evidence handoff",
    "evidence_minimum_gate": "Checking the evidence minimum",
    "signal_synthesis": "Signal agent writing the report",
    "final_validation": "Validating the report",
    "persist_report": "Saving the report",
    "persist_incomplete_report": "Saving an honest NO_SIGNAL report",
}


class GraphState(TypedDict, total=False):
    run_id: str
    price: dict[str, Any]
    news: dict[str, Any]
    technical: dict[str, Any]
    evidence: list[dict[str, Any]]
    handoff: dict[str, Any]
    gate: dict[str, Any]
    signal: dict[str, Any]
    outcome: dict[str, Any]


Node = Callable[[GraphState], Awaitable[dict[str, Any]]]
CheckpointerFactory = Callable[[], AbstractAsyncContextManager[Any]]


class ResearchWorkflow:
    """RunExecutor backed by LangGraph."""

    def __init__(
        self,
        *,
        gateway: LLMGateway,
        tool_sessions: Callable[[], AbstractAsyncContextManager[ToolSession]],
        artifacts: ArtifactStore,
        budget: BudgetService,
        checkpointer: CheckpointerFactory,
        langsmith_api_key: str | None = None,
        langsmith_project: str = "alloyiq-dev",
    ) -> None:
        self.gateway = gateway
        self.tool_sessions = tool_sessions
        self.artifacts = artifacts
        self.budget = budget
        self.checkpointer = checkpointer
        self.langsmith_api_key = langsmith_api_key
        self.langsmith_project = langsmith_project

    async def _config(self, handle: RunHandle) -> tuple[dict[str, Any], RateCard]:
        async with handle.tx() as conn:
            profile = await CatalogRepository().get_model_profile(conn, handle.ctx.model_profile_id)
            card_id = (
                await conn.execute(
                    text("SELECT id FROM rate_cards WHERE version = :v"),
                    {"v": handle.ctx.versions.get("rate_card")},
                )
            ).scalar_one()
            card_row = dict(
                (await conn.execute(text("SELECT * FROM rate_cards WHERE id = :i"), {"i": card_id}))
                .one()
                ._mapping
            )
            items = await conn.execute(
                text(
                    "SELECT model_id, token_category, provider_usd_per_mtok, verified"
                    " FROM rate_card_items WHERE rate_card_id = :c"
                ),
                {"c": card_id},
            )
            card_row["items"] = [dict(i._mapping) for i in items]
        assert profile is not None  # noqa: S101 - FK from the run
        return profile, RateCard.from_row(card_row)  # the version pinned on the run (D056)

    async def execute(self, handle: RunHandle) -> RunResult:
        profile, card = await self._config(handle)
        sink = make_sink(
            handle.db,
            handle.ctx.account_id,
            handle.ctx.run_id,
            api_key=self.langsmith_api_key,
            project=self.langsmith_project,
        )
        models = ModelCaller(handle, self.gateway, card, self.budget, self.artifacts, trace=sink)
        final: dict[str, Any] = {}
        try:
            final = await self._invoke(handle, models, profile, sink)
        finally:
            outcome_name = (final.get("outcome") or {}).get("status", "interrupted")
            with contextlib.suppress(Exception):  # tracing never blocks or fails research
                await asyncio.shield(sink.flush(outcome_name))
        outcome = final.get("outcome") or {"status": "failed", "reason": "graph_incomplete"}
        return RunResult(outcome["status"], outcome.get("reason"), outcome.get("error_code"))

    async def _invoke(
        self, handle: RunHandle, models: ModelCaller, profile: dict[str, Any], sink: TraceSink
    ) -> dict[str, Any]:
        async with self.tool_sessions() as session, self.checkpointer() as saver:
            tools = ToolCaller(
                handle,
                session,
                PolicyEngine({b.node_id: frozenset(b.tools) for b in BRANCHES}),
                self.artifacts,
            )
            graph = build_graph(handle, models, tools, profile, sink).compile(checkpointer=saver)
            config: Any = {"configurable": {"thread_id": str(handle.ctx.run_id)}}
            resume = await saver.aget_tuple(config) is not None
            final = await graph.ainvoke(
                None if resume else {"run_id": str(handle.ctx.run_id)}, config
            )
        result: dict[str, Any] = final
        return result


def _items(state: GraphState) -> list[EvidenceItem]:
    return [EvidenceItem.from_dict(e) for e in state.get("evidence", [])]


def build_graph(
    handle: RunHandle,
    models: ModelCaller,
    tools: ToolCaller,
    profile: dict[str, Any],
    sink: TraceSink | None = None,
) -> StateGraph[Any]:
    repo = EvidenceRepository()
    ctx = handle.ctx
    primary = profile["primary_model_id"]

    def node(name: str, fn: Node) -> Node:
        async def run(state: GraphState) -> dict[str, Any]:
            await handle.node_started(name)
            span = sink.span(name, "chain", node_id=name) if sink else None
            await handle.activity(None, "node_started", NODE_LABELS.get(name, name))
            try:
                out = await fn(state)
            except BaseException:
                await _finish_quietly(handle, name)
                if sink and span:
                    sink.finish(span, status="failed")
                raise
            await handle.node_finished(name, output_ref=f"checkpoint:{ctx.run_id}:{name}")
            if sink and span:
                sink.finish(span, status="completed")
            return out

        return run

    async def validate_request(_: GraphState) -> dict[str, Any]:
        ins = ctx.instrument_snapshot
        required = ("instrument_id", "unit", "quote_currency", "instrument_type", "data_mode")
        if any(not ins.get(k) for k in required):
            return {"outcome": {"status": "failed", "reason": "invalid_instrument_snapshot"}}
        return {}

    async def policy_check(_: GraphState) -> dict[str, Any]:
        if primary not in ctx.approved_models:
            return {"outcome": {"status": "failed", "reason": "model_not_approved"}}
        return {}

    def branch(spec: AgentSpec) -> Node:
        async def run(_: GraphState) -> dict[str, Any]:
            agent = BranchAgent(
                spec,
                handle,
                models,
                tools,
                model=primary,
                effort=profile["branch_effort"],
                max_tokens=int(profile["max_tokens_branch"]),
            )
            res = await agent.run()
            stance = res.finding["stance"] if res.finding else "no finding"
            await handle.activity(
                spec.name,
                "branch_finished",
                f"{spec.name.title()} agent: {stance}" + (f" ({res.error})" if res.error else ""),
            )
            return {
                spec.name: {"finding": res.finding, "evidence": res.evidence, "error": res.error}
            }

        return run

    async def merge_evidence(state: GraphState) -> dict[str, Any]:
        merged: dict[str, dict[str, Any]] = {}
        for b in ("price", "news", "technical"):
            for e in (state.get(b) or {}).get("evidence", []):  # type: ignore[attr-defined]
                merged[e["evidence_id"]] = e
        return {"evidence": list(merged.values())}

    async def handoff_validation(state: GraphState) -> dict[str, Any]:
        snap = {**ctx.instrument_snapshot}
        verdict = handoff_checks(_items(state), snap)
        async with handle.tx() as conn:
            await repo.validation(
                conn,
                account_id=ctx.account_id,
                run_id=ctx.run_id,
                boundary="handoff",
                subject_id=str(ctx.run_id),
                verdict=verdict.verdict,
                reasons=list(verdict.reasons),
                policy_version=REPORT_POLICY,
            )
            await append_run_event(
                conn,
                ctx.account_id,
                ctx.run_id,
                "validation.completed",
                ValidationCompleted(
                    boundary="handoff",
                    subject_id=str(ctx.run_id),
                    verdict=verdict.verdict,
                    reasons=list(verdict.reasons),
                ),
            )
        return {"handoff": {"passed": verdict.passed, "reasons": list(verdict.reasons)}}

    async def evidence_minimum_gate(state: GraphState) -> dict[str, Any]:
        news = (state.get("news") or {}).get("finding") or {}
        relevant = {
            a["evidence_id"]
            for a in news.get("news_assessments", [])
            if a["relevance"] in ("high", "medium")
        }
        res = evidence_minimum(
            _items(state),
            horizon=ctx.horizon,
            instrument_type=str(ctx.instrument_snapshot.get("instrument_type")),
            relevant_news_ids=relevant,
        )
        missing = list(res.missing)
        handoff = state.get("handoff") or {}
        if not handoff.get("passed", False):
            missing.append(
                "Evidence failed handoff validation: " + ", ".join(handoff.get("reasons", []))
            )
        for b in ("price", "news", "technical"):
            err = (state.get(b) or {}).get("error")  # type: ignore[attr-defined]
            if err and (err.startswith("refused:real_spend") or err.endswith("token_budget")):
                missing.append("The run's real-spend or token budget was reached.")
        return {"gate": {"sufficient": not missing, "missing": list(dict.fromkeys(missing))}}

    async def signal_synthesis(state: GraphState) -> dict[str, Any]:
        agent = SignalAgent(
            models,
            model=primary,
            effort=profile["signal_effort"],
            max_tokens=int(profile["max_tokens_signal"]),
            repair_tokens=int(profile["max_tokens_repair"]),
        )
        findings = {
            b: (state.get(b) or {}).get("finding")  # type: ignore[attr-defined]
            for b in ("price", "news", "technical")
        }
        try:
            out, checks, repaired = await agent.write(_items(state), findings, gate_sufficient=True)
        except ModelCallRefused as exc:
            return {
                "signal": {
                    "output": None,
                    "checks": [],
                    "repaired": False,
                    "error": f"refused:{exc.reason}",
                }
            }
        except ModelCallFailed as exc:
            return {
                "signal": {
                    "output": None,
                    "checks": [],
                    "repaired": False,
                    "error": f"failed:{exc.code}",
                }
            }
        return {
            "signal": {
                "output": out.model_dump() if out else None,
                "checks": [
                    {"name": c.name, "passed": c.passed, "detail": c.detail} for c in checks
                ],
                "repaired": repaired,
                "error": None if out else "report_validation_failed",
            }
        }

    async def final_validation(state: GraphState) -> dict[str, Any]:
        sig = state.get("signal") or {}
        verdict: Literal["pass", "fail"] = "pass" if sig.get("output") else "fail"
        reasons = [c["name"] for c in sig.get("checks", []) if not c["passed"]] or (
            [sig["error"]] if sig.get("error") else []
        )
        async with handle.tx() as conn:
            await repo.validation(
                conn,
                account_id=ctx.account_id,
                run_id=ctx.run_id,
                boundary="final_report",
                subject_id=str(ctx.run_id),
                verdict=verdict,
                reasons=reasons,
                policy_version=REPORT_POLICY,
            )
            await append_run_event(
                conn,
                ctx.account_id,
                ctx.run_id,
                "validation.completed",
                ValidationCompleted(
                    boundary="final_report",
                    subject_id=str(ctx.run_id),
                    verdict=verdict,
                    reasons=reasons,
                ),
            )
        return {}

    async def _persist(state: GraphState, complete: bool) -> dict[str, Any]:
        sig = state.get("signal") or {}
        gate = state.get("gate") or {"missing": []}
        out = SignalOutput.model_validate(sig["output"]) if complete and sig.get("output") else None
        missing = list(gate.get("missing", []))
        if not complete and not missing:
            missing = [_reason_text(sig.get("error"))]
        report = assemble_report(
            run_id=ctx.run_id,
            instrument=ctx.instrument_snapshot,
            horizon=ctx.horizon,
            out=out,
            evidence=_items(state),
            checks=[ReportCheck(**c) for c in sig.get("checks", [])],
            repaired=bool(sig.get("repaired")),
            missing=[] if complete else missing,
        )
        async with handle.tx() as conn:
            created = await repo.insert_report(
                conn, account_id=ctx.account_id, run_id=ctx.run_id, report=report
            )
            if created:
                await conn.execute(
                    text("UPDATE research_runs SET report_ready_at = now() WHERE id = :r"),
                    {"r": ctx.run_id},
                )
                await append_run_event(
                    conn,
                    ctx.account_id,
                    ctx.run_id,
                    "report.ready",
                    ReportReady(
                        report_id=uuid.UUID(report["report_id"]),
                        signal=report["signal"],
                        complete=report["complete"],
                    ),
                )
        if complete:
            return {"outcome": {"status": "succeeded", "reason": None}}
        error = sig.get("error") or ""
        budget = (
            "real_spend" in error
            or "token_budget" in error
            or any("budget was reached" in m for m in missing)
        )
        reason = (
            "budget_exceeded"
            if budget
            else "evidence_minimum_not_met"
            if gate.get("missing")
            else _reason_code(error)
        )
        return {
            "outcome": {
                "status": "incomplete",
                "reason": reason,
                "error_code": "BUDGET_EXCEEDED" if budget else None,
            }
        }

    async def persist_report(state: GraphState) -> dict[str, Any]:
        return await _persist(state, True)

    async def persist_incomplete_report(state: GraphState) -> dict[str, Any]:
        return await _persist(state, False)

    g: StateGraph[Any] = StateGraph(GraphState)
    for name, fn in (
        ("validate_request", validate_request),
        ("policy_check", policy_check),
        ("merge_evidence", merge_evidence),
        ("handoff_validation", handoff_validation),
        ("evidence_minimum_gate", evidence_minimum_gate),
        ("signal_synthesis", signal_synthesis),
        ("final_validation", final_validation),
        ("persist_report", persist_report),
        ("persist_incomplete_report", persist_incomplete_report),
    ):
        g.add_node(name, cast(Any, node(name, fn)))
    for spec in BRANCHES:
        g.add_node(spec.node_id, cast(Any, node(spec.node_id, branch(spec))))

    def stop_or(next_node: str) -> Callable[[GraphState], str]:
        return lambda s: END if s.get("outcome") else next_node

    g.add_edge(START, "validate_request")
    g.add_conditional_edges("validate_request", stop_or("policy_check"))
    g.add_conditional_edges(
        "policy_check",
        lambda s: [END] if s.get("outcome") else [b.node_id for b in BRANCHES],
    )
    g.add_edge([b.node_id for b in BRANCHES], "merge_evidence")
    g.add_edge("merge_evidence", "handoff_validation")
    g.add_edge("handoff_validation", "evidence_minimum_gate")
    g.add_conditional_edges(
        "evidence_minimum_gate",
        lambda s: "signal_synthesis" if s["gate"]["sufficient"] else "persist_incomplete_report",
    )
    g.add_edge("signal_synthesis", "final_validation")
    g.add_conditional_edges(
        "final_validation",
        lambda s: (
            "persist_report"
            if (s.get("signal") or {}).get("output")
            else "persist_incomplete_report"
        ),
    )
    g.add_edge("persist_report", END)
    g.add_edge("persist_incomplete_report", END)
    return g


def _reason_code(error: str | None) -> str:
    if not error:
        return "report_validation_failed"
    if "refusal" in error:
        return "model_refused"
    if error.startswith("failed:"):
        return "model_unavailable"
    return error.replace(":", "_")[:60]


def _reason_text(error: str | None) -> str:
    code = _reason_code(error)
    return {
        "model_refused": "The model declined to write this report.",
        "model_unavailable": "The model was unavailable.",
        "report_validation_failed": "The report failed validation after one repair attempt.",
    }.get(code, f"The report could not be completed ({code}).")


async def _finish_quietly(handle: RunHandle, name: str) -> None:
    with contextlib.suppress(BaseException):
        await handle.node_finished(name, "cancelled" if handle.cancelled else "failed")
