"""Dummy-payment acceptance tests through the HTTP API (R036-R038; D054, D064, D113-D115).

success -> exactly one credit; failure and expiry -> none; duplicate or reordered events
-> one credit; refunds touch only the demo ledger; the public webhook is always 404.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, text

from alloyiq.adapters.payments import DummyPaymentProvider
from alloyiq.application.billing import BillingService
from alloyiq.domain.payments import PaymentEventDraft
from alloyiq.repositories.billing import BillingRepository
from alloyiq.repositories.db import Database, DbContext
from alloyiq.worker.loops import build_billing

from .apiclient import ApiClient, login, register_verified
from .conftest import WORKER_URL, make_settings

pytestmark = pytest.mark.integration


def _key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _sim(body: dict[str, Any]) -> None:
    assert body["payment_mode"] == "simulated"
    assert body["money_collected"] == {"currency": "INR", "amount": "0.00"}


async def _member(app: FastAPI, migrator: Engine) -> ApiClient:
    c = ApiClient(app)
    email = await register_verified(c, migrator)
    await login(c, email)
    return c


def _processor(worker_db: Database) -> BillingService:
    return build_billing(make_settings(), worker_db)[1]  # type: ignore[arg-type]


async def _drain(worker_db: Database) -> None:
    p = _processor(worker_db)
    while await p.process_events():
        pass


async def _available(c: ApiClient) -> Decimal:
    r = await c.get("/account/balance")
    assert r.status_code == 200, r.text
    _sim(r.json()["data"])
    return Decimal(r.json()["data"]["available"]["amount"])


async def _checkout(c: ApiClient, amount: str, key: dict[str, str] | None = None) -> Any:
    return await c.post(
        "/billing/credit-checkouts",
        {"amount": {"currency": "INR", "amount": amount}, "return_path": "/account"},
        headers=key or _key(),
    )


async def _simulate(c: ApiClient, cid: str, outcome: str, key: dict[str, str] | None = None) -> Any:
    return await c.post(
        f"/billing/checkouts/{cid}/simulate", {"outcome": outcome}, headers=key or _key()
    )


# ------------------------------------------------------------------ opening credit
async def test_verified_member_gets_opening_credit(app: FastAPI, migrator: Engine) -> None:
    async with await _member(app, migrator) as c:
        assert await _available(c) == Decimal("500.00")
        r = await c.get("/account/ledger")
        items = r.json()["data"]["items"]
    assert [i["kind"] for i in items] == ["opening_grant"]
    assert items[0]["amount"] == {"currency": "INR", "amount": "500.00"}
    _sim(items[0])


# ------------------------------------------------------------------ checkout lifecycle
async def test_success_credits_exactly_once(
    app: FastAPI, migrator: Engine, worker_db: Database
) -> None:
    async with await _member(app, migrator) as c:
        key = _key()
        r1 = await _checkout(c, "200.00", key)
        r2 = await _checkout(c, "200.00", key)  # double-click: same key replays
        assert r1.status_code == r2.status_code == 201, r1.text
        co = r1.json()["data"]
        assert r2.json()["data"]["checkout_id"] == co["checkout_id"]
        _sim(co)
        assert co["status"] == "created" and co["hosted_url"] == (
            f"/account/checkout/{co['checkout_id']}"
        )
        # Two "success" clicks with different keys and one retry of the first.
        k = _key()
        for key_ in (k, k, _key()):
            r = await _simulate(c, co["checkout_id"], "success", key_)
            assert r.status_code == 202, r.text
            _sim(r.json()["data"])
        # Nothing changes until the asynchronous processor runs.
        assert await _available(c) == Decimal("500.00")
        await _drain(worker_db)
        assert await _available(c) == Decimal("700.00")
        got = (await c.get(f"/billing/checkouts/{co['checkout_id']}")).json()["data"]
        assert got["status"] == "paid" and got["credit_applied"] is True
        receipts = (await c.get("/billing/invoices")).json()["data"]["items"]
    assert len(receipts) == 1
    assert receipts[0]["sample"] is True and "not a tax invoice" in receipts[0]["label"]
    with migrator.connect() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM ledger_transactions WHERE checkout_id = :c"),
            {"c": co["checkout_id"]},
        ).scalar()
        events = conn.execute(
            text("SELECT count(*) FROM payment_events WHERE checkout_id = :c"),
            {"c": co["checkout_id"]},
        ).scalar()
    assert n == 1 and events == 2  # the retried key made no second event


async def test_failure_and_expiry_give_no_credit(
    app: FastAPI, migrator: Engine, worker_db: Database
) -> None:
    async with await _member(app, migrator) as c:
        failed = (await _checkout(c, "50.00")).json()["data"]["checkout_id"]
        assert (await _simulate(c, failed, "failure")).status_code == 202
        await _drain(worker_db)
        assert (await _simulate(c, failed, "success")).status_code == 202  # after final
        pending = (await _checkout(c, "60.00")).json()["data"]["checkout_id"]
        await _simulate(c, pending, "pending")
        await _drain(worker_db)
        assert (await c.get(f"/billing/checkouts/{pending}")).json()["data"]["status"] == "pending"
        with migrator.begin() as conn:  # 30 minutes later
            conn.execute(
                text("UPDATE checkouts SET expires_at = now() - interval '1 s' WHERE id = :i"),
                {"i": pending},
            )
        assert await _processor(worker_db).expire_checkouts() >= 1
        assert await _processor(worker_db).expire_checkouts() == 0  # deterministic event ID
        await _drain(worker_db)
        await _simulate(c, pending, "success")  # too late
        await _drain(worker_db)
        assert (await c.get(f"/billing/checkouts/{failed}")).json()["data"]["status"] == "failed"
        assert (await c.get(f"/billing/checkouts/{pending}")).json()["data"]["status"] == "expired"
        assert await _available(c) == Decimal("500.00")


EVENT_TYPES = ["checkout.pending", "checkout.paid", "checkout.failed"]


@settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(st.lists(st.sampled_from(EVENT_TYPES), min_size=1, max_size=6), st.randoms())
async def test_duplicate_and_reordered_events_credit_once(
    migrator: Engine, events: list[str], rnd: Any
) -> None:
    from .conftest import make_tenant

    assert WORKER_URL
    db = Database(WORKER_URL, pool_size=2, max_overflow=0)
    try:
        t = make_tenant(migrator)
        repo = BillingRepository()
        with migrator.begin() as conn:
            cid = conn.execute(
                text(
                    "INSERT INTO checkouts (account_id, user_id, amount, return_path, expires_at)"
                    " VALUES (:a, :u, 75, '/account', now() + interval '30 min') RETURNING id"
                ),
                {"a": t.account_id, "u": t.user_id},
            ).scalar_one()
        drafts = [
            PaymentEventDraft(f"test_{uuid.uuid4().hex}", e, "test:injection")  # type: ignore[arg-type]
            for e in events
        ]
        deliveries = drafts + [rnd.choice(drafts) for _ in range(len(drafts))]  # duplicates
        rnd.shuffle(deliveries)
        for d in deliveries:
            async with db.transaction(DbContext(account_id=t.account_id)) as conn:
                await repo.insert_event(conn, t.account_id, d, checkout_id=cid)
        await _drain(db)
        with migrator.connect() as conn:
            credits = conn.execute(
                text("SELECT count(*) FROM ledger_transactions WHERE checkout_id = :c"),
                {"c": cid},
            ).scalar()
            status = conn.execute(
                text("SELECT status FROM checkouts WHERE id = :c"), {"c": cid}
            ).scalar()
            stored = conn.execute(
                text("SELECT count(*) FROM payment_events WHERE checkout_id = :c"), {"c": cid}
            ).scalar()
        assert stored == len(drafts)  # duplicate event IDs are never stored twice
        assert credits == (1 if status == "paid" else 0)
        # Whatever the order, the first final event processed decides the outcome.
        assert status in ("paid", "failed", "pending")
    finally:
        await db.dispose()


# ------------------------------------------------------------------ refunds
async def test_refund_reverses_demo_credit_only(
    app: FastAPI, migrator: Engine, worker_db: Database
) -> None:
    async with await _member(app, migrator) as c:
        cid = (await _checkout(c, "200.00")).json()["data"]["checkout_id"]
        # Refunding an unpaid top-up is refused.
        r = await c.post(
            "/billing/refund-requests",
            {"payment_id": cid, "amount": {"currency": "INR", "amount": "10.00"}, "reason": "x"},
            headers=_key(),
        )
        assert r.status_code == 422, r.text
        await _simulate(c, cid, "success")
        await _drain(worker_db)
        key = _key()
        body = {
            "payment_id": cid,
            "amount": {"currency": "INR", "amount": "50.00"},
            "reason": "changed my mind",
        }
        r1 = await c.post("/billing/refund-requests", body, headers=key)
        r2 = await c.post("/billing/refund-requests", body, headers=key)
        assert r1.status_code == r2.status_code == 202, r1.text
        assert r1.json()["data"]["refund_id"] == r2.json()["data"]["refund_id"]
        _sim(r1.json()["data"])
        assert r1.json()["data"]["status"] == "requested"
        # A changed body under the same key is a conflict.
        changed = {**body, "amount": {"currency": "INR", "amount": "60.00"}}
        r = await c.post("/billing/refund-requests", changed, headers=key)
        assert r.status_code == 409 and r.json()["code"] == "IDEMPOTENCY_CONFLICT"
        too_much = {**body, "amount": {"currency": "INR", "amount": "150.01"}}
        assert (
            await c.post("/billing/refund-requests", too_much, headers=_key())
        ).status_code == 422
        await _drain(worker_db)
        assert await _available(c) == Decimal("650.00")
        ledger = (await c.get("/account/ledger?event_type=refund")).json()["data"]["items"]
        receipts = (await c.get("/billing/invoices")).json()["data"]["items"]
    assert len(ledger) == 1 and ledger[0]["amount"]["amount"] == "-50.00"
    assert ledger[0]["reverses_id"] is not None
    assert sorted(r["kind"] for r in receipts) == ["refund", "topup"]
    with migrator.connect() as conn:  # nothing outside the simulated demo ledger
        accounts = conn.execute(
            text(
                "SELECT DISTINCT ledger_account FROM ledger_postings p JOIN ledger_transactions t"
                " ON t.id = p.transaction_id WHERE t.refund_id IS NOT NULL AND t.checkout_id = :c"
            ),
            {"c": cid},
        ).scalars()
        names = set(accounts)
        assert {n for n in names if not n.startswith("wallet:")} == {"sim_refunds"}


# ------------------------------------------------------------------ limits and validation
async def test_checkout_limits(app: FastAPI, migrator: Engine) -> None:
    async with await _member(app, migrator) as c:
        assert (await _checkout(c, "500.01")).status_code == 422  # single checkout max
        assert (await _checkout(c, "10.005")).status_code == 422  # sub-paisa
        assert (await _checkout(c, "500.00")).status_code == 201
        assert (await _checkout(c, "500.00")).status_code == 201
        r = await _checkout(c, "0.01")  # daily limit 1000.00 reached
        assert r.status_code == 429 and r.json()["code"] == "PLAN_LIMIT_REACHED"
        assert int(r.headers["Retry-After"]) > 0
        r = await c.post(
            "/billing/credit-checkouts",
            {"amount": {"currency": "INR", "amount": "5.00"}, "return_path": "/account"},
        )
        assert r.status_code == 422  # Idempotency-Key header is required
        r = await c.post(
            "/billing/credit-checkouts",
            {
                "amount": {"currency": "INR", "amount": "5.00"},
                "return_path": "https://evil.example",
            },
            headers=_key(),
        )
        assert r.status_code == 422  # only approved same-origin return paths


async def test_other_accounts_checkouts_are_invisible(app: FastAPI, migrator: Engine) -> None:
    async with await _member(app, migrator) as a, await _member(app, migrator) as b:
        cid = (await _checkout(a, "20.00")).json()["data"]["checkout_id"]
        assert (await b.get(f"/billing/checkouts/{cid}")).status_code == 404
        assert (await _simulate(b, cid, "success")).status_code == 404
        r = await b.post(
            "/billing/refund-requests",
            {"payment_id": cid, "amount": {"currency": "INR", "amount": "1.00"}, "reason": "x"},
            headers=_key(),
        )
        assert r.status_code == 404
        assert (await b.get("/billing/invoices")).json()["data"]["items"] == []


async def test_simulation_requires_csrf_and_auth(app: FastAPI, migrator: Engine) -> None:
    async with await _member(app, migrator) as c:
        cid = (await _checkout(c, "20.00")).json()["data"]["checkout_id"]
        r = await c.http.post(
            f"/api/v1/billing/checkouts/{cid}/simulate",
            json={"outcome": "success"},
            headers=_key(),
        )
        assert r.status_code == 403 and r.json()["code"] == "CSRF_FAILED"
    async with ApiClient(app) as anon:
        assert (await anon.get("/account/balance")).status_code == 401


async def test_public_payment_webhook_is_always_404(app: FastAPI) -> None:
    async with ApiClient(app) as c:
        for provider in ("stripe", "razorpay", "dummy"):
            r = await c.http.post(
                f"/api/v1/webhooks/payments/{provider}", json={"type": "checkout.paid"}
            )
            assert r.status_code == 404 and r.json()["code"] == "RESOURCE_NOT_FOUND"


def test_dummy_provider_is_local_only() -> None:
    p = DummyPaymentProvider()
    assert p.hosted_url(uuid.uuid4()).startswith("/account/checkout/")
    assert p.expiry_event(uuid.UUID(int=1)).event_id == p.expiry_event(uuid.UUID(int=1)).event_id
