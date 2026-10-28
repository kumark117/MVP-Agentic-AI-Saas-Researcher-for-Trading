"""P9 hardening: retention purge, account deletion, Redis and LangSmith outages, and the
evaluation harness (R054-R055, R058, R069-R070; build prompt section 18)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, text

from alloyiq.adapters.artifacts import LocalArtifactStore
from alloyiq.application.purge import PurgeService
from alloyiq.evaluations.harness import EvalHarness, load_dataset
from alloyiq.repositories.db import Database, DbContext
from alloyiq.security.ratelimit import LIMITS, RedisRateLimiter
from alloyiq.tools.cache import RedisCache
from alloyiq.workflows.factory import PostgresCheckpointer, checkpoint_dsn

from .conftest import MIGRATOR_URL, WORKER_URL, make_settings
from .test_runs import (
    _admitted,
    _drain_foreign_jobs,  # noqa: F401 - autouse fixture
    _member,
)

pytestmark = pytest.mark.integration


def _purge(worker_db: Database, tmp: Path) -> PurgeService:
    from alloyiq.worker.loops import build_billing, build_lifecycle

    settings = make_settings()
    wallet, _ = build_billing(settings, worker_db)  # type: ignore[arg-type]
    return PurgeService(worker_db, build_lifecycle(settings, wallet), LocalArtifactStore(tmp))  # type: ignore[arg-type]


async def test_retention_purge_removes_only_expired_rows(
    app: FastAPI, migrator: Engine, worker_db: Database, enabled_gold: uuid.UUID, tmp_path: Path
) -> None:
    async with await _member(app, migrator) as c:
        run_id = await _admitted(c, enabled_gold, migrator)
    with migrator.begin() as conn:
        old, kept = conn.execute(
            text("SELECT min(seq), max(seq) FROM run_events WHERE scope_id = :r"), {"r": run_id}
        ).one()
        # Events are append-only, so add one already past retention next to the live ones.
        conn.execute(
            text(
                "INSERT INTO run_events (account_id, scope_id, seq, event_type, payload, expires_at)"
                " SELECT account_id, id, 99999, 'activity.appended',"
                ' \'{"agent": null, "action": "x", "summary": "old"}\', now() - interval \'1 day\''
                " FROM research_runs WHERE id = :r"
            ),
            {"r": run_id},
        )
        conn.execute(
            text(
                "UPDATE idempotency_records SET expires_at = now() - interval '1 s'"
                " WHERE resource_id = :r"
            ),
            {"r": run_id},
        )
    report = await _purge(worker_db, tmp_path).run_once()
    assert report.deleted["run_events"] >= 1 and report.deleted["idempotency_records"] >= 1
    with migrator.connect() as conn:
        seqs = (
            conn.execute(
                text("SELECT seq FROM run_events WHERE scope_id = :r ORDER BY seq"), {"r": run_id}
            )
            .scalars()
            .all()
        )
    assert 99999 not in seqs and old in seqs and kept in seqs


async def test_account_deletion_purges_content_and_keeps_financial_records(
    app: FastAPI, migrator: Engine, worker_db: Database, enabled_gold: uuid.UUID, tmp_path: Path
) -> None:
    async with await _member(app, migrator) as c:
        queued = await _admitted(c, enabled_gold, migrator, question="Private question")
        conv = (await c.post("/conversations", {"title": "Mine"})).json()["data"]["conversation_id"]
        await c.post(
            f"/conversations/{conv}/messages",
            {"content": "secret note", "client_message_id": str(uuid.uuid4())},
        )
        with migrator.connect() as conn:
            account, user = conn.execute(
                text("SELECT account_id, user_id FROM research_runs WHERE id = :r"), {"r": queued}
            ).one()
        with migrator.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO deletion_requests (account_id, user_id, scheduled_at)"
                    " VALUES (:a, :u, now() - interval '1 minute')"
                ),
                {"a": account, "u": user},
            )
        report = await _purge(worker_db, tmp_path).run_once()
        assert report.accounts_deleted >= 1
        assert (await c.get("/me")).status_code == 401  # sessions revoked
    with migrator.connect() as conn:
        q = lambda sql: conn.execute(text(sql), {"a": account}).scalar()  # noqa: E731
        assert q("SELECT count(*) FROM messages WHERE account_id = :a") == 0
        assert q("SELECT count(*) FROM conversations WHERE account_id = :a") == 0
        assert q("SELECT status FROM accounts WHERE id = :a") == "deleted"
        assert q(
            "SELECT email FROM users WHERE id = (SELECT owner_user_id FROM accounts WHERE id = :a)"
        ).endswith("@deleted.invalid")
        assert q("SELECT status FROM research_runs WHERE account_id = :a") == "cancelled"
        assert q("SELECT count(*) FROM ledger_transactions WHERE account_id = :a") >= 1  # kept
        assert q("SELECT count(*) FROM tombstones WHERE account_id = :a") >= 2
        assert q("SELECT status FROM deletion_requests WHERE account_id = :a") == "completed"


async def test_messages_stay_immutable_outside_system_scope(
    api_db: Database, migrator: Engine
) -> None:
    from sqlalchemy.exc import DBAPIError

    from .conftest import make_tenant

    t = make_tenant(migrator)
    with migrator.begin() as conn:
        cid = conn.execute(
            text("INSERT INTO conversations (account_id, user_id) VALUES (:a, :u) RETURNING id"),
            {"a": t.account_id, "u": t.user_id},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO messages (account_id, conversation_id, user_id, role, content)"
                " VALUES (:a, :c, :u, 'user', 'x')"
            ),
            {"a": t.account_id, "c": cid, "u": t.user_id},
        )
    for sql in ("UPDATE messages SET content = 'y'", "DELETE FROM messages"):
        with pytest.raises(DBAPIError):
            async with api_db.transaction(
                DbContext(account_id=t.account_id, user_id=t.user_id)
            ) as c:
                await c.execute(text(sql + " WHERE conversation_id = :c"), {"c": cid})


# ------------------------------------------------------------------ outages
async def test_redis_outage_falls_back_and_never_fails_open() -> None:
    limiter = RedisRateLimiter("redis://127.0.0.1:1/0")  # nothing listens there
    limit = LIMITS["login:email"]
    decisions = [
        await limiter.hit("login:email:x@example.com", limit) for _ in range(limit.capacity + 2)
    ]
    assert decisions[0].allowed
    assert not all(d.allowed for d in decisions)  # the in-process fallback still limits
    cache = RedisCache("redis://127.0.0.1:1/0")
    assert await cache.get("k") is None  # a Redis failure is a cache miss, not an error
    await cache.set("k", {"v": 1}, 10)


async def test_research_works_with_redis_down(
    migrator: Engine, worker_db: Database, enabled_gold: uuid.UUID
) -> None:
    """Admission, queueing, events and replay all come from PostgreSQL (ADR-005)."""
    from alloyiq.api.app import create_app
    from alloyiq.api.deps import build_services

    from .conftest import API_URL

    settings = make_settings(redis_url="redis://127.0.0.1:1/0")
    db = Database(API_URL or "", pool_size=2, max_overflow=0)
    try:
        services = build_services(settings, db=db)  # type: ignore[arg-type]

        async def ok() -> None:
            return None

        app = create_app(settings, services=services, readiness_probe=ok)  # type: ignore[arg-type]
        async with await _member(app, migrator) as c:
            run_id = await _admitted(c, enabled_gold, migrator)
            cancel = await c.post(f"/research/runs/{run_id}/cancel", {})
            assert cancel.status_code == 200  # durable state changes need no Redis
            r = await c.get(f"/research/runs/{run_id}/events")  # terminal: replay, then close
            assert r.status_code == 200
            assert "event: run.queued" in r.text and "event: run.finished" in r.text
    finally:
        await db.dispose()


# ------------------------------------------------------------------ evaluation harness
@pytest.fixture
async def admin_db() -> AsyncIterator[Database]:
    assert MIGRATOR_URL
    db = Database(MIGRATOR_URL.replace("+psycopg", "+asyncpg"), pool_size=2, max_overflow=0)
    yield db
    await db.dispose()


async def test_evaluation_harness_scores_and_gates(admin_db: Database, tmp_path: Path) -> None:
    ds = load_dataset("v1")
    assert len(ds.cases) >= 30
    instruments = {c.instrument for c in ds.cases}
    horizons = {(c.instrument, c.horizon) for c in ds.cases}
    assert len(instruments) == 4 and len(horizons) >= 10  # every instrument and horizon
    assert WORKER_URL and MIGRATOR_URL
    harness = EvalHarness(
        admin_db,
        artifacts=LocalArtifactStore(tmp_path),
        checkpointer=PostgresCheckpointer(checkpoint_dsn(MIGRATOR_URL)),
    )
    subset = [
        "gold-swing",
        "gold-stale-quote",
        "gold-injection-unapproved-tool",
        "gold-ungrounded-number-repaired",
        "gold-signal-refusal",
        "copper-hold-is-supported",
    ]
    s = await harness.run(ds, case_ids=subset)
    assert s.cases == len(subset)
    assert s.gates_passed, s.failures
    assert s.failures == [], s.failures
    assert s.citation_support >= 0.95
    async with admin_db.transaction(DbContext.for_system()) as conn:
        n = (
            await conn.execute(
                text("SELECT count(*) FROM evaluation_scores WHERE evaluation_run_id = :r"),
                {"r": s.run_id},
            )
        ).scalar()
    assert n and n >= len(subset) * 4
