"""The research graph end to end with fake providers (P6 exit criteria).

Claude is replaced by FakeLLMGateway (SYNTHETIC responses and token counts) and market
data, FX and news by the P3 scenario fakes; everything else is real: LangGraph with the
Postgres checkpointer, the call-intent protocol, validators, settlement and events.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, text

from alloyiq.adapters.artifacts import LocalArtifactStore
from alloyiq.adapters.fake_llm import FakeLLMGateway, Scenario
from alloyiq.adapters.fakes import FakeFx, FakeMarketData, FakeNews
from alloyiq.application.budget import BudgetService, SpendCaps
from alloyiq.repositories.db import Database
from alloyiq.tools.cache import NullCache
from alloyiq.tools.gateway import InProcessToolGateway
from alloyiq.tools.service import Providers, ToolService
from alloyiq.workflows.factory import PostgresCheckpointer, UnavailableWorkflow, checkpoint_dsn
from alloyiq.workflows.graph import ResearchWorkflow

from .conftest import WORKER_URL
from .test_runs import _admitted, _member, _process, _snap

pytestmark = pytest.mark.integration

from .test_runs import _drain_foreign_jobs  # noqa: E402,F401 - re-exported autouse fixture


def workflow(
    db: Database, tmp: Path, llm: FakeLLMGateway, *, market: str = "normal", news: str = "normal"
) -> ResearchWorkflow:
    m = FakeMarketData(market)  # type: ignore[arg-type]
    svc = ToolService(
        db,
        Providers(market={"yahoo": m, "alphavantage": m}, fx=FakeFx(), news=FakeNews(news)),  # type: ignore[arg-type]
        cache=NullCache(),
    )
    assert WORKER_URL
    caps = SpendCaps(Decimal("1.50"), Decimal("1000000"), Decimal("1000000"))
    return ResearchWorkflow(
        gateway=llm,
        tool_sessions=InProcessToolGateway(svc).session,
        artifacts=LocalArtifactStore(tmp),
        budget=BudgetService(caps),
        checkpointer=PostgresCheckpointer(checkpoint_dsn(WORKER_URL)),
    )


async def _run(
    app: FastAPI,
    migrator: Engine,
    worker_db: Database,
    gold: uuid.UUID,
    tmp: Path,
    llm: FakeLLMGateway,
    **kw: Any,
) -> tuple[Any, str, dict[str, Any]]:
    c = await _member(app, migrator)
    run_id = await _admitted(c, gold, migrator, question="What is the outlook for gold?")
    await _process(worker_db, run_id, workflow(worker_db, tmp, llm, **kw))
    return c, run_id, await _snap(c, run_id)


def _q(engine: Engine, sql: str, run_id: str) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(text(sql), {"r": run_id}).all())


async def test_happy_path_produces_a_cited_report(
    app: FastAPI, migrator: Engine, worker_db: Database, enabled_gold: uuid.UUID, tmp_path: Path
) -> None:
    llm = FakeLLMGateway()
    c, run_id, snap = await _run(app, migrator, worker_db, enabled_gold, tmp_path, llm)
    async with c:
        assert snap["status"] == "succeeded", snap
        assert snap["report_available"] and snap["billing_status"] == "settled"
        report = (await c.get(f"/research/runs/{run_id}/report")).json()["data"]
        usage = (await c.get(f"/research/runs/{run_id}/usage")).json()["data"]
        activity = (await c.get(f"/research/runs/{run_id}/activity")).json()["data"]["items"]
    assert report["signal"] == "BUY" and report["complete"] is True
    assert report["citations"] and report["validation_summary"]["passed"] is True
    assert report["confidence"]["calibration_status"] == "uncalibrated"
    assert report["data_as_of"] is not None and report["missing_evidence"] == []
    # 3 branches x (tool call + finding) + 1 signal call, all on the approved primary model.
    assert len(usage["calls"]) == 7 and all(u["usage_status"] == "actual" for u in usage["calls"])
    assert {u["model_id"] for u in usage["calls"]} == {"claude-opus-5-5"}
    assert usage["provider_cost_status"] == "calculated"
    assert any(a["action"] == "branch_finished" for a in activity)
    assert snap["time_to_first_model_token_ms"] is not None
    assert snap["observability_gap"] is True  # no LangSmith key: a visible gap, never silent
    # The member's question was wrapped as untrusted data, never as an instruction.
    assert any(
        'untrusted_evidence id="question"' in str(r.messages[0]["content"]) for r in llm.requests
    )
    events = [
        r[0]
        for r in _q(
            migrator, "SELECT event_type FROM run_events WHERE scope_id = :r ORDER BY seq", run_id
        )
    ]
    for e in (
        "tool.started",
        "tool.completed",
        "llm.completed",
        "validation.completed",
        "report.ready",
        "billing.updated",
        "run.finished",
    ):
        assert e in events, e
    nodes = {
        r[0]
        for r in _q(
            migrator,
            "SELECT node_id FROM run_nodes WHERE run_id = :r AND status = 'completed'",
            run_id,
        )
    }
    assert {"price_branch", "news_branch", "technical_branch", "persist_report"} <= nodes
    # One graph thread per run, in the graph schema (owned by the worker role only).
    from alloyiq.repositories.db import DbContext

    async with worker_db.transaction(DbContext.for_system()) as conn:
        n = (
            await conn.execute(
                text("SELECT count(*) FROM graph.checkpoints WHERE thread_id = :t"), {"t": run_id}
            )
        ).scalar()
    assert n and n > 0


async def test_injection_article_is_quarantined(
    app: FastAPI, migrator: Engine, worker_db: Database, enabled_gold: uuid.UUID, tmp_path: Path
) -> None:
    from alloyiq.adapters.fakes import INJECTION_TEXT

    llm = FakeLLMGateway()
    _, run_id, snap = await _run(
        app, migrator, worker_db, enabled_gold, tmp_path, llm, news="injection"
    )
    assert snap["status"] == "succeeded"
    q = _q(
        migrator, "SELECT taint_flags FROM evidence_items WHERE run_id = :r AND quarantined", run_id
    )
    assert len(q) == 1 and q[0][0]
    sec = _q(migrator, "SELECT kind FROM security_events WHERE run_id = :r", run_id)
    assert ("injection.quarantined",) in [tuple(s) for s in sec]
    # The malicious text never reached any model call, and caused no extra tool call.
    sent = " ".join(str(r.messages) for r in llm.requests)
    assert INJECTION_TEXT[:40] not in sent
    tools = _q(migrator, "SELECT tool_name FROM tool_calls WHERE run_id = :r", run_id)
    assert len(tools) == 3


async def test_missing_bars_end_as_honest_no_signal(
    app: FastAPI, migrator: Engine, worker_db: Database, enabled_gold: uuid.UUID, tmp_path: Path
) -> None:
    c, run_id, snap = await _run(
        app, migrator, worker_db, enabled_gold, tmp_path, FakeLLMGateway(), market="missing_bars"
    )
    async with c:
        report = (await c.get(f"/research/runs/{run_id}/report")).json()["data"]
    assert snap["status"] == "incomplete" and snap["outcome_reason"] == "evidence_minimum_not_met"
    assert report["signal"] == "NO_SIGNAL" and report["complete"] is False
    assert any("indicator set" in m for m in report["missing_evidence"])
    # No Signal call was made: the gate stopped synthesis.
    calls = _q(migrator, "SELECT purpose FROM llm_calls WHERE run_id = :r", run_id)
    assert ("signal",) not in [tuple(x) for x in calls]


async def test_one_repair_fixes_an_ungrounded_number(
    app: FastAPI, migrator: Engine, worker_db: Database, enabled_gold: uuid.UUID, tmp_path: Path
) -> None:
    c, run_id, snap = await _run(
        app,
        migrator,
        worker_db,
        enabled_gold,
        tmp_path,
        FakeLLMGateway(Scenario(bad_numbers_first=True)),
    )
    async with c:
        report = (await c.get(f"/research/runs/{run_id}/report")).json()["data"]
        usage = (await c.get(f"/research/runs/{run_id}/usage")).json()["data"]
    assert snap["status"] == "succeeded"
    assert report["validation_summary"]["repaired"] is True
    repair = [u for u in usage["calls"] if u["purpose"] == "repair"]
    assert len(repair) == 1 and repair[0]["billable"] is False  # platform cost only
    assert "98765.43" not in report["summary"]


async def test_report_that_never_validates_is_incomplete(
    app: FastAPI, migrator: Engine, worker_db: Database, enabled_gold: uuid.UUID, tmp_path: Path
) -> None:
    c, run_id, snap = await _run(
        app,
        migrator,
        worker_db,
        enabled_gold,
        tmp_path,
        FakeLLMGateway(Scenario(always_bad_numbers=True)),
    )
    async with c:
        report = (await c.get(f"/research/runs/{run_id}/report")).json()["data"]
    assert snap["status"] == "incomplete"
    assert report["signal"] == "NO_SIGNAL" and report["complete"] is False
    calls = _q(
        migrator,
        "SELECT purpose FROM llm_calls WHERE run_id = :r AND purpose IN ('signal','repair')",
        run_id,
    )
    assert len(calls) == 2  # exactly one repair; rules are never loosened


async def test_refusal_uses_only_approved_fallback_then_stops(
    app: FastAPI, migrator: Engine, worker_db: Database, enabled_gold: uuid.UUID, tmp_path: Path
) -> None:
    llm = FakeLLMGateway(Scenario(refuse_nodes={"signal_synthesis"}))
    _, run_id, snap = await _run(app, migrator, worker_db, enabled_gold, tmp_path, llm)
    assert snap["status"] == "incomplete" and snap["outcome_reason"] == "model_refused"
    tried = [r for r in llm.requests if r.metadata.get("node_id") == "signal_synthesis"]
    assert [r.model for r in tried] == ["claude-opus-5-5", "claude-sonnet-5-5"]
    assert tried[0].system == tried[1].system and tried[0].messages == tried[1].messages
    sec = _q(
        migrator,
        "SELECT count(*) FROM security_events WHERE run_id = :r AND kind = 'llm.refusal'",
        run_id,
    )
    assert sec[0][0] == 2


async def test_interrupted_stream_is_unknown_never_fabricated(
    app: FastAPI, migrator: Engine, worker_db: Database, enabled_gold: uuid.UUID, tmp_path: Path
) -> None:
    c, run_id, snap = await _run(
        app,
        migrator,
        worker_db,
        enabled_gold,
        tmp_path,
        FakeLLMGateway(Scenario(interrupt_nodes={"signal_synthesis"})),
    )
    async with c:
        usage = (await c.get(f"/research/runs/{run_id}/usage")).json()["data"]
    sig = [u for u in usage["calls"] if u["node_id"] == "signal_synthesis"]
    assert len(sig) == 1 and sig[0]["usage_status"] == "unknown"
    assert sig[0]["token_categories"]["input_tokens"] is None  # never a tokenizer estimate
    assert usage["provider_cost_status"] == "unknown" and usage["provider_cost_total"] is None
    assert snap["status"] == "incomplete" and snap["billing_status"] == "pending_settlement"


async def test_unapproved_tool_request_is_refused_by_policy(
    app: FastAPI, migrator: Engine, worker_db: Database, enabled_gold: uuid.UUID, tmp_path: Path
) -> None:
    _, run_id, snap = await _run(
        app,
        migrator,
        worker_db,
        enabled_gold,
        tmp_path,
        FakeLLMGateway(Scenario(request_unapproved_tool=True)),
    )
    assert snap["status"] == "succeeded"
    sec = _q(migrator, "SELECT kind FROM security_events WHERE run_id = :r", run_id)
    assert ("tool.not_allowed",) in [tuple(s) for s in sec]
    names = {
        r[0] for r in _q(migrator, "SELECT tool_name FROM tool_calls WHERE run_id = :r", run_id)
    }
    assert "get_account_balance" not in names


async def test_max_tokens_on_a_branch_gets_one_repair(
    app: FastAPI, migrator: Engine, worker_db: Database, enabled_gold: uuid.UUID, tmp_path: Path
) -> None:
    llm = FakeLLMGateway(Scenario(max_tokens_once={"price_branch"}))
    _, _, snap = await _run(app, migrator, worker_db, enabled_gold, tmp_path, llm)
    assert snap["status"] == "succeeded"
    assert llm.calls["price_branch"] == 3  # cut off, then tool use, then the finding


async def test_no_api_key_fails_explicitly() -> None:
    from alloyiq.workflows.factory import build_workflow

    from .conftest import make_settings

    wf = build_workflow(make_settings(anthropic_api_key=None), None)  # type: ignore[arg-type]
    assert isinstance(wf, UnavailableWorkflow) and "ANTHROPIC_API_KEY" in wf.reason


async def test_failed_trace_export_is_buffered_and_flagged(
    worker_db: Database, migrator: Engine
) -> None:
    from alloyiq.observability.tracing import LangSmithTraceSink

    from .conftest import Tenant, make_run, make_tenant

    t: Tenant = make_tenant(migrator)
    run_id = make_run(migrator, t)
    sink = LangSmithTraceSink(worker_db, t.account_id, run_id, api_key="x", project="p")

    def boom(_: str) -> None:
        raise ConnectionError("LangSmith down")

    sink._export = boom  # type: ignore[method-assign]
    span = sink.span("price_branch", "chain", node_id="price_branch", prompt="SECRET PROMPT")
    sink.finish(span, status="completed")
    await sink.flush("succeeded")
    with migrator.connect() as conn:
        gap = conn.execute(
            text("SELECT observability_gap FROM research_runs WHERE id = :r"), {"r": run_id}
        ).scalar()
        payload = conn.execute(
            text("SELECT payload FROM trace_outbox WHERE run_id = :r"), {"r": run_id}
        ).scalar_one()
    assert gap is True
    assert payload["spans"][0]["meta"] == {"node_id": "price_branch", "status": "completed"}
    assert "SECRET" not in str(payload)  # redaction: prompts never leave the platform
