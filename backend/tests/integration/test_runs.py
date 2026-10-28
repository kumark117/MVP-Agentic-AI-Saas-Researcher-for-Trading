"""Estimate, admission, worker, settlement, cancellation and recovery (P5 exit criteria).

Runs through the real API and a real JobRunner on the worker role, with a fake graph
that writes SYNTHETIC usage through the real call-intent protocol.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, text

from alloyiq.application.recovery import RecoverySweeper
from alloyiq.repositories.db import Database
from alloyiq.worker.jobs import JobRunner
from alloyiq.worker.loops import build_billing, build_lifecycle
from alloyiq.workflows.runner import LostAuthority, RunHandle

from .apiclient import ApiClient, login, register_verified
from .conftest import make_settings, profile_id
from .fake_graph import FakeWorkflow, ParallelBranches

pytestmark = pytest.mark.integration

DEEP_MAX = Decimal("106.25")  # worst case for the deep profile on rate card v1 (unit-tested)


def _key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


async def _member(app: FastAPI, migrator: Engine) -> ApiClient:
    c = ApiClient(app)
    await login(c, await register_verified(c, migrator))
    return c


async def _estimate(
    c: ApiClient, instrument: uuid.UUID, migrator: Engine, **extra: Any
) -> dict[str, Any]:
    body = {
        "instrument_id": str(instrument),
        "horizon": "swing",
        "model_profile_id": str(profile_id(migrator)),
        **extra,
    }
    r = await c.post("/research/estimates", body)
    assert r.status_code == 201, r.text
    data: dict[str, Any] = r.json()["data"]
    return data


async def _start(c: ApiClient, est: dict[str, Any], key: dict[str, str] | None = None) -> Any:
    return await c.post(
        "/research/runs",
        {"estimate_id": est["estimate_id"], "max_charge": est["max_charge"]},
        headers=key or _key(),
    )


async def _admitted(c: ApiClient, gold: uuid.UUID, migrator: Engine, **extra: Any) -> str:
    r = await _start(c, await _estimate(c, gold, migrator, **extra))
    assert r.status_code == 202, r.text
    run_id: str = r.json()["data"]["run_id"]
    return run_id


def _runner(worker_db: Database, executor: Any, **kw: Any) -> JobRunner:
    settings = make_settings()
    wallet, _ = build_billing(settings, worker_db)  # type: ignore[arg-type]
    return JobRunner(
        worker_db,
        executor,
        build_lifecycle(settings, wallet),  # type: ignore[arg-type]
        worker_id=f"test-{uuid.uuid4().hex[:6]}",
        **kw,
    )


async def _process(worker_db: Database, run_id: str, executor: Any, **kw: Any) -> JobRunner:
    """Claim jobs until this run's job is processed (other tests may leave jobs queued)."""
    runner = _runner(worker_db, executor, **kw)
    for _ in range(200):
        if not await runner.run_one():
            break
        if await _status_db(worker_db, run_id) not in ("queued",):
            break
    return runner


async def _status_db(db: Database, run_id: str) -> str:
    from alloyiq.repositories.db import DbContext

    async with db.transaction(DbContext.for_system()) as c:
        return str(
            (
                await c.execute(
                    text("SELECT status FROM research_runs WHERE id = :r"), {"r": run_id}
                )
            ).scalar()
        )


async def _snap(c: ApiClient, run_id: str) -> dict[str, Any]:
    r = await c.get(f"/research/runs/{run_id}")
    assert r.status_code == 200, r.text
    data: dict[str, Any] = r.json()["data"]
    return data


async def _available(c: ApiClient) -> Decimal:
    return Decimal((await c.get("/account/balance")).json()["data"]["available"]["amount"])


def _events(engine: Engine, run_id: str) -> list[str]:
    with engine.connect() as c:
        return list(
            c.execute(
                text("SELECT event_type FROM run_events WHERE scope_id = :r ORDER BY seq"),
                {"r": run_id},
            ).scalars()
        )


@pytest.fixture(autouse=True)
def _drain_foreign_jobs(migrator: Engine) -> None:
    """Jobs left queued by other tests must not be claimed instead of this test's run."""
    with migrator.begin() as c:
        c.execute(
            text(
                "UPDATE jobs SET available_at = now() + interval '1 day'"
                " WHERE state = 'runnable' AND available_at <= now()"
            )
        )


# ------------------------------------------------------------------ estimate
async def test_estimate_contract_and_validation(
    app: FastAPI, migrator: Engine, enabled_gold: uuid.UUID
) -> None:
    async with await _member(app, migrator) as c:
        est = await _estimate(c, enabled_gold, migrator, question="Is gold overbought?")
        assert est["max_charge"] == {"currency": "INR", "amount": str(DEEP_MAX)}
        assert est["estimated_charge"]["amount"] == "42.50"
        assert est["max_real_cost"] == {"currency": "USD", "amount": "1.00"}
        assert est["approved_models"] == ["claude-opus-5-5", "claude-sonnet-5-5"]
        assert est["limits"]["run_model_calls"] == 24 and est["payment_mode"] == "simulated"
        assert est["price_version"].endswith("(draft)")
        assert len(est["request_fingerprint"]) == 64
        mcx = uuid.uuid4()  # unknown or disabled instruments are both refused
        r = await c.post(
            "/research/estimates",
            {
                "instrument_id": str(mcx),
                "horizon": "swing",
                "model_profile_id": str(profile_id(migrator)),
            },
        )
        assert r.status_code == 422 and r.json()["code"] == "UNSUPPORTED_INSTRUMENT"
        r = await c.post(
            "/research/estimates",
            {
                "instrument_id": str(enabled_gold),
                "horizon": "swing",
                "model_profile_id": str(profile_id(migrator)),
                "question": "x" * 4001,
            },
        )
        assert r.status_code == 422


# ------------------------------------------------------------------ admission
async def test_admission_is_atomic_and_idempotent(
    app: FastAPI, migrator: Engine, enabled_gold: uuid.UUID
) -> None:
    async with await _member(app, migrator) as c:
        est = await _estimate(c, enabled_gold, migrator, question="Outlook?")
        low = await c.post(
            "/research/runs",
            {
                "estimate_id": est["estimate_id"],
                "max_charge": {"currency": "INR", "amount": "1.00"},
            },
            headers=_key(),
        )
        assert low.status_code == 422 and low.json()["code"] == "ESTIMATE_MISMATCH"
        key = _key()
        a, b = await _start(c, est, key), await _start(c, est, key)
        assert a.status_code == b.status_code == 202, a.text
        assert a.json()["data"] == b.json()["data"]
        run = a.json()["data"]
        assert run["status"] == "queued" and run["billing_status"] == "reserved"
        assert run["reserved_amount"]["amount"] == str(DEEP_MAX)
        assert run["money_collected"] == {"currency": "INR", "amount": "0.00"}
        changed = await c.post(
            "/research/runs",
            {
                "estimate_id": est["estimate_id"],
                "max_charge": {"currency": "INR", "amount": "200.00"},
            },
            headers=key,
        )
        assert changed.status_code == 409 and changed.json()["code"] == "IDEMPOTENCY_CONFLICT"
        reused = await _start(c, est)  # a new key cannot reuse a consumed estimate
        assert reused.status_code == 409
        assert await _available(c) == Decimal("500.00") - DEEP_MAX
        snap = await _snap(c, run["run_id"])
        assert snap["status"] == "queued" and snap["latest_event_id"] == f"{run['run_id']}:1"
        assert snap["versions"]["graph"] and snap["versions"]["rate_card"].startswith("v1")
        assert snap["queue_ms"] is None and snap["end_to_end_ms"] is None  # never invented
        expired = await _estimate(c, enabled_gold, migrator)
        with migrator.begin() as conn:
            conn.execute(
                text("UPDATE estimates SET expires_at = now() - interval '1 s' WHERE id = :e"),
                {"e": expired["estimate_id"]},
            )
        r = await _start(c, expired)
        assert r.status_code == 410 and r.json()["code"] == "ESTIMATE_EXPIRED"
    assert _events(migrator, run["run_id"]) == ["run.queued"]
    with migrator.connect() as conn:
        msg = conn.execute(
            text("SELECT content FROM messages WHERE run_id = :r"), {"r": run["run_id"]}
        ).scalar()
    assert msg == "Outlook?"


async def test_two_devices_racing_for_the_last_credit(
    app: FastAPI, migrator: Engine, enabled_gold: uuid.UUID
) -> None:
    async with ApiClient(app) as phone:
        email = await register_verified(phone, migrator)
        await login(phone, email)
        async with ApiClient(app) as laptop:
            await login(laptop, email)
            # Leave credit for exactly one run.
            with migrator.connect() as conn:
                account = conn.execute(
                    text(
                        "SELECT a.id FROM accounts a JOIN users u ON u.id = a.owner_user_id"
                        " WHERE u.email = :e"
                    ),
                    {"e": email},
                ).scalar_one()
            from .conftest import Tenant, grant

            grant(migrator, Tenant(uuid.uuid4(), account, email), "-350.00")
            e1 = await _estimate(phone, enabled_gold, migrator)
            e2 = await _estimate(laptop, enabled_gold, migrator)
            r1, r2 = await asyncio.gather(_start(phone, e1), _start(laptop, e2))
            codes = sorted([r1.status_code, r2.status_code])
            assert codes == [202, 409], (r1.text, r2.text)
            loser = r1 if r1.status_code == 409 else r2
            assert loser.json()["code"] == "INSUFFICIENT_CREDIT"
            assert await _available(phone) == Decimal("150.00") - DEEP_MAX
    with migrator.connect() as conn:
        runs = conn.execute(
            text("SELECT count(*) FROM research_runs WHERE account_id = :a"), {"a": account}
        ).scalar()
        holds = conn.execute(
            text("SELECT count(*) FROM reservations WHERE account_id = :a"), {"a": account}
        ).scalar()
    assert runs == holds == 1  # the rolled-back admission left no orphans


async def test_plan_limit_on_queued_runs(
    app: FastAPI, migrator: Engine, enabled_gold: uuid.UUID
) -> None:
    async with await _member(app, migrator) as c:
        with migrator.connect() as conn:
            account = conn.execute(
                text("SELECT account_id FROM sessions ORDER BY created_at DESC LIMIT 1")
            ).scalar_one()
        from .conftest import Tenant, grant

        grant(migrator, Tenant(uuid.uuid4(), account, ""), "1000.00")
        for _ in range(5):
            assert (await _start(c, await _estimate(c, enabled_gold, migrator))).status_code == 202
        r = await _start(c, await _estimate(c, enabled_gold, migrator))
        assert r.status_code == 429 and r.json()["code"] == "PLAN_LIMIT_REACHED"
        assert r.headers["Retry-After"]


# ------------------------------------------------------------------ worker and settlement
async def test_worker_runs_and_settles_once(
    app: FastAPI, migrator: Engine, worker_db: Database, enabled_gold: uuid.UUID
) -> None:
    async with await _member(app, migrator) as c:
        run_id = await _admitted(c, enabled_gold, migrator)
        flow = FakeWorkflow(nodes=["validate_request", "price_branch", "news_branch"])
        await _process(worker_db, run_id, flow)
        snap = await _snap(c, run_id)
        assert snap["status"] == "succeeded" and snap["billing_status"] == "settled"
        # 3 synthetic calls x (10k input x 4 + 1k output x 20 USD/MTok) x 1.25 x 85 = 19.125
        assert snap["settled_charge"]["amount"] == "19.13"
        assert snap["queue_ms"] is not None and snap["execution_ms"] is not None
        assert snap["progress"]["completed_nodes"] == 3
        assert await _available(c) == Decimal("480.87")
        usage = (await c.get(f"/research/runs/{run_id}/usage")).json()["data"]
        assert len(usage["calls"]) == 3 and usage["provider_cost_status"] == "calculated"
        assert usage["provider_cost_total"] == {"currency": "USD", "amount": "0.18"}
        assert usage["money_collected"]["amount"] == "0.00"
        activity = (await c.get(f"/research/runs/{run_id}/activity")).json()["data"]["items"]
        assert [a["agent"] for a in activity] == ["validate_request", "price_branch", "news_branch"]
        r = await c.get(f"/research/runs/{run_id}/report")
        assert r.status_code == 409 and r.json()["code"] == "REPORT_NOT_READY"
        listed = (await c.get("/research/runs")).json()["data"]["items"]
        assert listed[0]["run_id"] == run_id and listed[0]["status"] == "succeeded"
    events = _events(migrator, run_id)
    assert events[:2] == ["run.queued", "run.started"]
    assert events[-2:] == ["billing.updated", "run.finished"]
    assert events.count("billing.updated") == 1
    with migrator.connect() as conn:
        debits = conn.execute(
            text("SELECT count(*) FROM ledger_transactions WHERE run_id = :r"), {"r": run_id}
        ).scalar()
    assert debits == 1


async def test_other_accounts_cannot_see_runs(
    app: FastAPI, migrator: Engine, enabled_gold: uuid.UUID
) -> None:
    async with await _member(app, migrator) as a, await _member(app, migrator) as b:
        run_id = await _admitted(a, enabled_gold, migrator)
        for path in ("", "/usage", "/activity", "/report", "/events"):
            assert (await b.get(f"/research/runs/{run_id}{path}")).status_code == 404, path
        assert (await b.post(f"/research/runs/{run_id}/cancel", {})).status_code == 404
        assert (await b.get("/research/runs")).json()["data"]["items"] == []


# ------------------------------------------------------------------ cancellation
async def test_cancel_queued_run_releases_everything(
    app: FastAPI, migrator: Engine, enabled_gold: uuid.UUID
) -> None:
    async with await _member(app, migrator) as c:
        run_id = await _admitted(c, enabled_gold, migrator)
        r = await c.post(f"/research/runs/{run_id}/cancel", {"reason": "changed my mind"})
        assert r.status_code == 200, r.text
        snap = r.json()["data"]
        assert snap["status"] == "cancelled" and snap["billing_status"] == "released"
        again = await c.post(f"/research/runs/{run_id}/cancel", {})
        assert again.status_code == 200 and again.json()["data"]["status"] == "cancelled"
        assert await _available(c) == Decimal("500.00")
    assert _events(migrator, run_id)[-3:] == [
        "run.cancel_requested",
        "billing.updated",
        "run.finished",
    ]


async def test_cancel_during_parallel_branches(
    app: FastAPI, migrator: Engine, worker_db: Database, enabled_gold: uuid.UUID
) -> None:
    async with await _member(app, migrator) as c:
        run_id = await _admitted(c, enabled_gold, migrator)
        flow = ParallelBranches(per_call_delay=0.05)
        task = asyncio.create_task(_process(worker_db, run_id, flow, cancel_poll_s=0.1))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if flow.total_calls >= 3:
                break
        r = await c.post(f"/research/runs/{run_id}/cancel", {})
        assert r.status_code == 202 and r.json()["data"]["status"] == "cancel_requested"
        await asyncio.wait_for(task, 10)
        snap = await _snap(c, run_id)
    assert snap["status"] == "cancelled"
    assert flow.calls_after_flag == 0
    with migrator.connect() as conn:
        stop_calls, stop_branches = conn.execute(
            text(
                "SELECT cancel_stop_calls_ms, cancel_stop_branches_ms FROM research_runs"
                " WHERE id = :r"
            ),
            {"r": run_id},
        ).one()
    assert stop_calls is not None and stop_calls < 2000  # target: 2 s p95
    assert stop_branches is not None and stop_branches < 5000  # target: 5 s p95
    # Confirmed usage is billed. Calls cut off mid-flight are unknown (the provider may
    # still bill them), so a bounded part of the reserve stays pending; the rest is released.
    assert Decimal(snap["settled_charge"]["amount"]) > 0
    assert snap["billing_status"] in ("settled", "pending_settlement")
    total = sum(
        Decimal(snap[k]["amount"]) for k in ("settled_charge", "pending_amount", "released_amount")
    )
    assert total == DEEP_MAX


async def test_cancel_versus_success_race_has_one_outcome(
    app: FastAPI, migrator: Engine, worker_db: Database, enabled_gold: uuid.UUID
) -> None:
    async with await _member(app, migrator) as c:
        run_id = await _admitted(c, enabled_gold, migrator)
        gate = asyncio.Event()

        async def hold(_: RunHandle) -> None:
            await gate.wait()

        flow = FakeWorkflow(nodes=["validate_request"], before_finish=hold)
        # A slow watcher, so the graph "finishes" before the worker sees the flag.
        task = asyncio.create_task(_process(worker_db, run_id, flow, cancel_poll_s=30))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if await _status_db(worker_db, run_id) == "running" and flow.executions:
                break
        assert (await c.post(f"/research/runs/{run_id}/cancel", {})).status_code == 202
        gate.set()
        await asyncio.wait_for(task, 10)
        snap = await _snap(c, run_id)
    assert snap["status"] == "cancelled"  # the committed cancel wins over a later success
    assert _events(migrator, run_id).count("run.finished") == 1


# ------------------------------------------------------------------ recovery and fencing
async def test_killed_worker_is_recovered_without_double_charge(
    app: FastAPI, migrator: Engine, worker_db: Database, enabled_gold: uuid.UUID
) -> None:
    async with await _member(app, migrator) as c:
        run_id = await _admitted(c, enabled_gold, migrator)
        gate = asyncio.Event()

        async def die(_: RunHandle) -> None:
            await gate.wait()

        first = FakeWorkflow(nodes=["validate_request", "price_branch"], before_finish=die)
        runner = _runner(worker_db, first, heartbeat_s=3600)
        task = asyncio.create_task(runner.run_one())
        for _ in range(100):
            await asyncio.sleep(0.05)
            with migrator.connect() as conn:
                n = conn.execute(
                    text(
                        "SELECT count(*) FROM llm_calls WHERE run_id = :r AND status = 'completed'"
                    ),
                    {"r": run_id},
                ).scalar()
            if n == 2:
                break
        task.cancel()  # the process dies after its provider calls completed
        with pytest.raises(asyncio.CancelledError):
            await task
        with migrator.begin() as conn:
            gen1 = conn.execute(
                text("SELECT generation FROM research_runs WHERE id = :r"), {"r": run_id}
            ).scalar_one()
            conn.execute(
                text("UPDATE jobs SET lease_expires_at = now() - interval '1 s' WHERE run_id = :r"),
                {"r": run_id},
            )
        sweep = await RecoverySweeper(worker_db, runner.lifecycle).sweep()
        assert sweep.requeued >= 1
        second = FakeWorkflow(nodes=["validate_request", "price_branch"])
        await _process(worker_db, run_id, second)
        snap = await _snap(c, run_id)
        assert snap["status"] == "succeeded"
        # Completed nodes were reused: no second provider call, one charge.
        with migrator.connect() as conn:
            calls = conn.execute(
                text("SELECT count(*) FROM llm_calls WHERE run_id = :r"), {"r": run_id}
            ).scalar()
            gen2 = conn.execute(
                text("SELECT generation FROM research_runs WHERE id = :r"), {"r": run_id}
            ).scalar_one()
        assert calls == 2 and gen2 == gen1 + 1
        assert snap["settled_charge"]["amount"] == "12.75"
        # The stale worker's write is fenced out.
        from alloyiq.workflows.runner import RunContext

        with migrator.connect() as conn:
            row = dict(
                conn.execute(text("SELECT * FROM research_runs WHERE id = :r"), {"r": run_id})
                .one()
                ._mapping
            )
        stale = RunHandle(worker_db, RunContext.from_run({**row, "generation": gen1}, None, False))
        with pytest.raises(LostAuthority):
            await stale.activity(None, "late", "stale write")
    assert "activity.appended" in _events(migrator, run_id)


async def test_ambiguous_call_is_unknown_and_platform_absorbed(
    app: FastAPI, migrator: Engine, worker_db: Database, enabled_gold: uuid.UUID
) -> None:
    from .fake_graph import fake_call

    async with await _member(app, migrator) as c:
        run_id = await _admitted(c, enabled_gold, migrator)

        class CrashMidCall:
            async def execute(self, handle: RunHandle) -> Any:
                await fake_call(handle, "price_branch", 0, crash_after_intent=True)

        runner = _runner(worker_db, CrashMidCall(), heartbeat_s=3600)
        await runner.run_one()  # dies mid-call: the job stays leased, nothing finishes
        with migrator.begin() as conn:
            conn.execute(
                text("UPDATE jobs SET lease_expires_at = now() - interval '1 s' WHERE run_id = :r"),
                {"r": run_id},
            )
        await RecoverySweeper(worker_db, runner.lifecycle).sweep()
        await _process(worker_db, run_id, FakeWorkflow(nodes=["validate_request"]))
        usage = (await c.get(f"/research/runs/{run_id}/usage")).json()["data"]
        snap = await _snap(c, run_id)
    statuses = {call["node_id"]: call["usage_status"] for call in usage["calls"]}
    assert statuses["price_branch"] == "unknown" and usage["unknown_call_count"] == 1
    amb = next(call for call in usage["calls"] if call["node_id"] == "price_branch")
    assert amb["billable"] is False and amb["provider_cost"] is None
    assert usage["provider_cost_status"] == "unknown"
    # Unknown usage keeps a bounded pending hold (released by the 24 h scheduler).
    assert snap["billing_status"] == "pending_settlement"
    assert Decimal(snap["pending_amount"]["amount"]) > 0


async def test_queue_expiry_and_dead_jobs(
    app: FastAPI, migrator: Engine, worker_db: Database, enabled_gold: uuid.UUID
) -> None:
    async with await _member(app, migrator) as c:
        old = await _admitted(c, enabled_gold, migrator)
        dead = await _admitted(c, enabled_gold, migrator)
        with migrator.begin() as conn:
            conn.execute(
                text(
                    "UPDATE research_runs SET submitted_at = now() - interval '6 minutes'"
                    " WHERE id = :r"
                ),
                {"r": old},
            )
            conn.execute(
                text(
                    "UPDATE research_runs SET status = 'running', started_at = now() WHERE id = :r"
                ),
                {"r": dead},
            )
            conn.execute(
                text(
                    "UPDATE jobs SET state = 'leased', attempts = 3, generation = 3,"
                    " lease_expires_at = now() - interval '1 s' WHERE run_id = :r"
                ),
                {"r": dead},
            )
        settings = make_settings()
        wallet, _ = build_billing(settings, worker_db)  # type: ignore[arg-type]
        res = await RecoverySweeper(worker_db, build_lifecycle(settings, wallet)).sweep()  # type: ignore[arg-type]
        assert res.queue_expired >= 1 and res.dead >= 1
        s_old, s_dead = await _snap(c, old), await _snap(c, dead)
        assert (s_old["status"], s_old["outcome_reason"]) == ("timed_out", "queue_expired")
        assert s_old["billing_status"] == "released"
        assert (s_dead["status"], s_dead["outcome_reason"]) == (
            "failed",
            "worker_retries_exhausted",
        )
        assert await _available(c) == Decimal("500.00")
    with migrator.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM incidents WHERE run_id = :r AND kind = 'job_dead'"),
                {"r": dead},
            ).scalar()
            == 1
        )
