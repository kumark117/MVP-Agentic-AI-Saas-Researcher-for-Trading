"""Conversations, follow-ups, feedback, reports with known percentiles, and exports
(R033-R035, R044-R048; P8 exit criteria)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, text

from alloyiq.application.exports import ExportService
from alloyiq.repositories.db import Database
from alloyiq.stats.core import percentile_cont

from .apiclient import ApiClient
from .test_runs import _admitted, _estimate, _member, _start

pytestmark = pytest.mark.integration


def _account_of(migrator: Engine) -> uuid.UUID:
    with migrator.connect() as c:
        return c.execute(
            text("SELECT account_id FROM sessions ORDER BY created_at DESC LIMIT 1")
        ).scalar_one()


async def test_conversation_messages_are_deduplicated_and_isolated(
    app: FastAPI, migrator: Engine
) -> None:
    async with await _member(app, migrator) as a, await _member(app, migrator) as b:
        conv = (await a.post("/conversations", {"title": "Gold"})).json()["data"]
        cid = conv["conversation_id"]
        mid = str(uuid.uuid4())
        r1 = await a.post(
            f"/conversations/{cid}/messages", {"content": "First", "client_message_id": mid}
        )
        r2 = await a.post(
            f"/conversations/{cid}/messages", {"content": "First", "client_message_id": mid}
        )
        assert (r1.status_code, r2.status_code) == (201, 200)
        assert r1.json()["data"]["message_id"] == r2.json()["data"]["message_id"]
        clash = await a.post(
            f"/conversations/{cid}/messages", {"content": "Other", "client_message_id": mid}
        )
        assert clash.status_code == 409
        too_long = await a.post(
            f"/conversations/{cid}/messages",
            {"content": "x" * 4001, "client_message_id": str(uuid.uuid4())},
        )
        assert too_long.status_code == 422
        msgs = (await a.get(f"/conversations/{cid}/messages")).json()["data"]["items"]
        assert [m["content"] for m in msgs] == ["First"]
        assert (await b.get(f"/conversations/{cid}/messages")).status_code == 404
        assert (await b.get("/conversations")).json()["data"]["items"] == []


async def test_follow_up_only_after_previous_run_is_terminal_and_feedback(
    app: FastAPI, migrator: Engine, enabled_gold: uuid.UUID
) -> None:
    async with await _member(app, migrator) as c:
        run_id = await _admitted(c, enabled_gold, migrator, question="First question")
        conv = (await c.get(f"/research/runs/{run_id}")).json()["data"]["conversation_id"]
        assert conv
        r = await c.post(
            "/research/estimates",
            {
                "instrument_id": str(enabled_gold),
                "horizon": "swing",
                "model_profile_id": (await _estimate(c, enabled_gold, migrator))[
                    "model_profile_id"
                ],
                "conversation_id": conv,
                "question": "Follow-up",
            },
        )
        assert r.status_code == 409 and r.json()["code"] == "INVALID_RUN_STATE"
        fb = await c.post(f"/research/runs/{run_id}/feedback", {"rating": "positive"})
        assert fb.status_code == 409  # feedback opens when the run has finished
        await c.post(f"/research/runs/{run_id}/cancel", {})
        follow = await _estimate(
            c, enabled_gold, migrator, conversation_id=conv, question="Follow-up"
        )
        assert follow["normalized_request"]["conversation_id"] == conv
        assert (await _start(c, follow)).status_code == 202
        first = await c.post(f"/research/runs/{run_id}/feedback", {"rating": "positive"})
        again = await c.put(
            f"/research/runs/{run_id}/feedback", {"rating": "negative", "comment": "meh"}
        )
        assert first.status_code == 201 and again.status_code == 200
        assert again.json()["data"]["rating"] == "negative"
        assert again.json()["data"]["feedback_id"] == first.json()["data"]["feedback_id"]
        msgs = (await c.get(f"/conversations/{conv}/messages")).json()["data"]["items"]
        assert [m["content"] for m in msgs] == ["First question", "Follow-up"]


def _insert_runs(
    migrator: Engine, account: uuid.UUID, durations: list[tuple[str, int]], base: datetime
) -> None:
    """Terminal runs with exact durations (ms) for percentile checks."""
    with migrator.begin() as c:
        user, ins, prof = c.execute(
            text(
                "SELECT (SELECT owner_user_id FROM accounts WHERE id = :a),"
                " (SELECT id FROM instruments LIMIT 1), (SELECT id FROM model_profiles LIMIT 1)"
            ),
            {"a": account},
        ).one()
        for i, (status, ms) in enumerate(durations):
            eid, rid = uuid.uuid4(), uuid.uuid4()
            sub = base + timedelta(minutes=i)
            c.execute(
                text(
                    "INSERT INTO estimates (id, account_id, user_id, normalized_request, request_fingerprint,"
                    " instrument_id, horizon, model_profile_id, approved_models, limits, rate_card_version,"
                    " min_charge, estimated_charge, max_charge, max_real_usd, expires_at)"
                    " VALUES (:e, :a, :u, '{}', 'fp', :i, 'swing', :p, ARRAY['m'], '{}', 'v', 0, 0, 1, 1, now())"
                ),
                {"e": eid, "a": account, "u": user, "i": ins, "p": prof},
            )
            c.execute(
                text(
                    "INSERT INTO research_runs (id, account_id, user_id, estimate_id, instrument_id,"
                    " instrument_snapshot, horizon, model_profile_id, versions, limits, authorized_max_charge,"
                    " reserved_amount, status, billing_status, submitted_at, started_at, terminal_at,"
                    " outcome_reason) VALUES (:r, :a, :u, :e, :i, '{}', 'swing', :p, '{}', '{}', 1, 1, :s,"
                    " 'released', :sub, :sub + interval '1 second', :sub + make_interval(secs => :ms / 1000.0),"
                    " :reason)"
                ),
                {
                    "r": rid,
                    "a": account,
                    "u": user,
                    "e": eid,
                    "i": ins,
                    "p": prof,
                    "s": status,
                    "sub": sub,
                    "ms": ms,
                    "reason": "queue_expired" if status == "timed_out" and ms < 0 else None,
                },
            )


async def test_summary_reports_known_percentiles_and_populations(
    app: FastAPI, migrator: Engine
) -> None:
    async with await _member(app, migrator) as c:
        account = _account_of(migrator)
        base = datetime(2026, 3, 1, tzinfo=UTC)
        ok_ms = [10_000, 20_000, 30_000, 40_000, 50_000]
        _insert_runs(
            migrator,
            account,
            [("succeeded", m) for m in ok_ms] + [("incomplete", 60_000), ("cancelled", 5_000)],
            base,
        )
        q = {"from": base.isoformat(), "to": (base + timedelta(days=1)).isoformat()}
        r = await c.get("/reports/summary", params=q)
        assert r.status_code == 200, r.text
        s = r.json()["data"]
        assert s["counts"]["submitted"] == 7 and s["counts"]["succeeded"] == 5
        assert s["counts"]["cancelled"] == 1
        assert s["success_rate"] == {
            "numerator": 5,
            "denominator": 6,
            "value": pytest.approx(5 / 6),
            "definition": s["success_rate"]["definition"],
        }
        lat = s["latency"]["success_end_to_end"]
        assert lat["sample_count"] == 5 and lat["low_sample"] is True
        assert lat["p50_ms"] == pytest.approx(percentile_cont(ok_ms, 0.5))
        assert lat["p95_ms"] == pytest.approx(percentile_cont(ok_ms, 0.95))
        assert lat["method"] == "exact" and lat["approximate"] is False
        assert s["latency"]["all_terminal_end_to_end"]["sample_count"] == 7
        assert s["window_basis"] == {
            "counts": "submitted_at",
            "outcomes": "terminal_at",
            "financial": "posted_at",
        }
        assert s["money_collected"]["amount"] == "0.00"
        assert any("samples" in w for w in s["warnings"])
        assert s["daily"][0]["day"] == "2026-03-01"
        empty = (
            await c.get(
                "/reports/summary",
                params={"from": "2025-01-01T00:00:00Z", "to": "2025-01-02T00:00:00Z"},
            )
        ).json()["data"]
        assert (
            empty["latency"]["queue"]["p50_ms"] is None and empty["success_rate"]["value"] is None
        )
        long = await c.get(
            "/reports/summary",
            params={"from": "2026-01-01T00:00:00Z", "to": "2026-06-01T00:00:00Z"},
        )
        assert long.status_code == 422
        models = await c.get("/reports/models", params=q)
        assert models.status_code == 200 and models.json()["data"]["models"] == []


async def test_exports_are_signed_owned_neutralised_and_expire(
    app: FastAPI, migrator: Engine, worker_db: Database
) -> None:
    async with await _member(app, migrator) as c, await _member(app, migrator) as other:
        conv = (await c.post("/conversations", {"title": "x"})).json()["data"]["conversation_id"]
        await c.post(
            f"/conversations/{conv}/messages",
            {
                "content": '=HYPERLINK("http://evil.example","click")',
                "client_message_id": str(uuid.uuid4()),
            },
        )
        body: dict[str, Any] = {
            "kind": "account_data",
            "format": "csv",
            "from": "2026-01-01T00:00:00Z",
            "to": "2026-12-31T00:00:00Z",
        }
        key = {"Idempotency-Key": str(uuid.uuid4())}
        r1, r2 = (
            await c.post("/exports", body, headers=key),
            await c.post("/exports", body, headers=key),
        )
        assert r1.status_code == r2.status_code == 202, r1.text
        export_id = r1.json()["data"]["export_id"]
        assert r2.json()["data"]["export_id"] == export_id
        api_exports: ExportService = app.state.services.extra["exports"]
        worker = ExportService(worker_db, api_exports.artifacts, secret=api_exports.secret.decode())
        while await worker.process_pending():
            pass
        job = (await c.get(f"/exports/{export_id}")).json()["data"]
        assert job["status"] == "ready" and job["download_url"].startswith(
            f"/api/v1/exports/{export_id}"
        )
        dl = await c.http.get(job["download_url"])
        assert dl.status_code == 200 and "attachment" in dl.headers["content-disposition"]
        text_body = dl.content.decode("utf-8-sig")
        assert "'=HYPERLINK" in text_body and ",=HYPERLINK" not in text_body
        # Another account cannot use the link, nor see the job.
        assert (await other.http.get(job["download_url"])).status_code == 404
        assert (await other.get(f"/exports/{export_id}")).status_code == 404
        # Expired link.
        expired = (
            job["download_url"].replace("expires=", "expires=1").split("&sig")[0]
            + "&sig="
            + "0" * 64
        )
        assert (await c.http.get(expired)).status_code in (404, 410)
        # A pending deletion invalidates links.
        with migrator.begin() as conn:
            acc = conn.execute(
                text("SELECT account_id FROM export_jobs WHERE id = :i"), {"i": export_id}
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO deletion_requests (account_id, user_id, scheduled_at)"
                    " SELECT :a, owner_user_id, now() + interval '7 days' FROM accounts WHERE id = :a"
                ),
                {"a": acc},
            )
        fresh = (await c.get(f"/exports/{export_id}")).json()["data"]["download_url"]
        assert (await c.http.get(fresh)).status_code == 410


async def test_export_validation(app: FastAPI, migrator: Engine) -> None:
    async with await _member(app, migrator) as c:
        bad = await c.post(
            "/exports",
            {
                "kind": "runs",
                "format": "xlsx",
                "from": "2026-01-01T00:00:00Z",
                "to": "2026-02-01T00:00:00Z",
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert bad.status_code == 422
        missing_key = await c.post(
            "/exports",
            {
                "kind": "runs",
                "format": "csv",
                "from": "2026-01-01T00:00:00Z",
                "to": "2026-02-01T00:00:00Z",
            },
        )
        assert missing_key.status_code == 422
    async with ApiClient(app) as anon:
        assert (await anon.get(f"/exports/{uuid.uuid4()}")).status_code == 401
