"""Wallet, reservations, settlement and real-spend budgets against real PostgreSQL (R038, R042).

Includes the Hypothesis properties from build prompt section 18: the ledger always
balances, available credit never goes negative under concurrent admissions, and the
wallet projection always equals what postings and reservations say.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, text

from alloyiq.application.budget import BudgetService, SpendCaps
from alloyiq.application.wallet import WalletService
from alloyiq.domain.errors import DomainError, ErrorCode
from alloyiq.repositories.db import Database, DbContext

from .conftest import API_URL, Tenant, grant, make_run, make_tenant

pytestmark = pytest.mark.integration


def _ctx(t: Tenant) -> DbContext:
    return DbContext(account_id=t.account_id, user_id=t.user_id)


def _unbalanced_journals(engine: Engine) -> int:
    with engine.connect() as c:
        return int(
            c.execute(
                text(
                    "SELECT count(*) FROM (SELECT transaction_id FROM ledger_postings"
                    " GROUP BY transaction_id, currency HAVING sum(amount) <> 0) x"
                )
            ).scalar_one()
        )


async def _projection_ok(db: Database, w: WalletService, t: Tenant) -> bool:
    async with db.transaction(_ctx(t)) as c:
        return await w.verify_projection(c, t.account_id)


async def test_opening_grant_is_posted_once(api_db: Database, migrator: Engine) -> None:
    t = make_tenant(migrator)
    w = WalletService(api_db, opening_credit=Decimal("500.00"))
    for _ in range(2):  # a repeated hook is a no-op: one business event, one journal
        async with api_db.transaction(_ctx(t)) as c:
            await w.grant_opening_credit(c, t.account_id, t.user_id)
    b = await w.balance(t.account_id)
    assert b.available == Decimal("500.00") and b.posted_credit == Decimal("500.00")
    page = await w.ledger(t.account_id)
    assert [e["kind"] for e in page.items] == ["opening_grant"]
    assert await _projection_ok(api_db, w, t)
    with migrator.connect() as c:
        n = c.execute(
            text("SELECT count(*) FROM account_events WHERE account_id = :a"),
            {"a": t.account_id},
        ).scalar()
    assert n == 1  # balance.updated for the account stream


async def test_reservation_settlement_is_idempotent(api_db: Database, migrator: Engine) -> None:
    t = make_tenant(migrator)
    grant(migrator, t, "100.00")
    w = WalletService(api_db, opening_credit=Decimal("0"))
    run = make_run(migrator, t, "25.00")
    async with api_db.transaction(_ctx(t)) as c:
        await w.reserve(c, t.account_id, run, Decimal("25.00"))
    b = await w.balance(t.account_id)
    assert (b.reserved, b.available) == (Decimal("25.00"), Decimal("75.00"))
    for attempt in range(2):
        async with api_db.transaction(_ctx(t)) as c:
            split = await w.settle(
                c,
                t.account_id,
                run,
                Decimal("3.455"),
                unknown_usage=False,
                rate_card_version="v-test",
            )
        assert (split is None) == (attempt == 1)
    b = await w.balance(t.account_id)
    assert (b.reserved, b.posted_debit, b.available) == (0, Decimal("3.46"), Decimal("96.54"))
    debits = [e for e in (await w.ledger(t.account_id)).items if e["kind"] == "run_debit"]
    assert len(debits) == 1 and debits[0]["amount"] == Decimal("-3.460000")
    with migrator.connect() as c:
        adj = c.execute(
            text("SELECT rounding_adjustment FROM ledger_transactions WHERE run_id = :r"),
            {"r": run},
        ).scalar()
    assert adj == Decimal("0.005000")
    assert await _projection_ok(api_db, w, t)


async def test_unknown_usage_keeps_a_bounded_pending_hold(
    api_db: Database, worker_db: Database, migrator: Engine
) -> None:
    t = make_tenant(migrator)
    grant(migrator, t, "50.00")
    w = WalletService(api_db, opening_credit=Decimal("0"))
    run = make_run(migrator, t, "20.00")
    async with api_db.transaction(_ctx(t)) as c:
        await w.reserve(c, t.account_id, run, Decimal("20.00"))
        await w.settle(
            c, t.account_id, run, Decimal("2.00"), unknown_usage=True, rate_card_version=None
        )
    b = await w.balance(t.account_id)
    assert (b.posted_debit, b.pending_settlement, b.available) == (
        Decimal("2.00"),
        Decimal("18.00"),
        Decimal("30.00"),
    )
    with migrator.begin() as c:  # 24 hours later
        c.execute(
            text("UPDATE reservations SET expires_at = now() - interval '1 s' WHERE run_id = :r"),
            {"r": run},
        )
    released = await WalletService(worker_db, opening_credit=Decimal("0")).release_due_pending()
    assert released >= 1
    b = await w.balance(t.account_id)
    assert (b.pending_settlement, b.available) == (0, Decimal("48.00"))
    assert await _projection_ok(api_db, w, t)


async def test_insufficient_credit(api_db: Database, migrator: Engine) -> None:
    t = make_tenant(migrator)
    grant(migrator, t, "10.00")
    w = WalletService(api_db, opening_credit=Decimal("0"))
    run = make_run(migrator, t, "25.00")
    with pytest.raises(DomainError) as e:
        async with api_db.transaction(_ctx(t)) as c:
            await w.reserve(c, t.account_id, run, Decimal("25.00"))
    assert e.value.code == ErrorCode.INSUFFICIENT_CREDIT and e.value.status == 409


@settings(
    max_examples=12,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    balance=st.integers(min_value=1, max_value=60),
    amounts=st.lists(st.integers(min_value=1, max_value=30), min_size=2, max_size=6),
)
async def test_concurrent_admissions_never_overspend(
    migrator: Engine, balance: int, amounts: list[int]
) -> None:
    """Two (or six) devices racing for the last credit: no overspend, no orphan holds."""
    assert API_URL
    db = Database(API_URL, pool_size=len(amounts), max_overflow=0)
    try:
        t = make_tenant(migrator)
        grant(migrator, t, f"{balance}.00")
        w = WalletService(db, opening_credit=Decimal("0"))
        runs = [make_run(migrator, t, f"{a}.00") for a in amounts]

        async def admit(run: uuid.UUID, amount: int) -> bool:
            try:
                async with db.transaction(_ctx(t)) as c:
                    await w.reserve(c, t.account_id, run, Decimal(amount))
                return True
            except DomainError:
                return False

        won = await asyncio.gather(*(admit(r, a) for r, a in zip(runs, amounts, strict=True)))
        reserved = sum(a for a, ok in zip(amounts, won, strict=True) if ok)
        b = await w.balance(t.account_id)
        assert reserved <= balance
        assert b.available >= 0 and b.reserved == reserved
        assert b.available == balance - reserved
        # Every refused admission left nothing behind; any affordable remainder was taken.
        losers = [a for a, ok in zip(amounts, won, strict=True) if not ok]
        assert all(a > b.available for a in losers)
        assert await _projection_ok(db, w, t)
        assert _unbalanced_journals(migrator) == 0
    finally:
        await db.dispose()


# ------------------------------------------------------------------ real-spend budgets
def _budget(day: date) -> BudgetService:
    caps = SpendCaps(Decimal("1.50"), Decimal("5.00"), Decimal("20.00"))
    return BudgetService(caps, clock=lambda: datetime.combine(day, datetime.min.time(), UTC))


def _isolated_day() -> date:
    # Each test uses its own UTC "day" so the shared platform row is not polluted.
    return date(2200, 1, 1) + timedelta(days=random.randrange(1, 300_000))  # noqa: S311


async def test_budget_caps_and_settlement(api_db: Database, migrator: Engine) -> None:
    day = _isolated_day()
    bs = _budget(day)
    t = make_tenant(migrator)
    with pytest.raises(DomainError) as e:
        bs.check_per_run(Decimal("1.51"))
    assert e.value.code == ErrorCode.BUDGET_EXCEEDED and e.value.status == 422
    runs = [make_run(migrator, t) for _ in range(4)]
    async with api_db.transaction(_ctx(t)) as c:
        for r in runs[:3]:
            await bs.reserve(c, t.account_id, r, Decimal("1.50"))
    with pytest.raises(DomainError) as e:  # 4.50 held; 1.50 more exceeds the 5.00 account cap
        async with api_db.transaction(_ctx(t)) as c:
            await bs.reserve(c, t.account_id, runs[3], Decimal("1.50"))
    assert e.value.status == 503 and e.value.code == ErrorCode.BUDGET_EXCEEDED
    assert e.value.headers and int(e.value.headers["Retry-After"]) > 0
    async with api_db.transaction(_ctx(t)) as c:
        assert await bs.authorize_call(c, runs[0], Decimal("1.00"))
        await bs.record_spend(c, runs[0], Decimal("0.40"))
        assert not await bs.authorize_call(c, runs[0], Decimal("1.20"))
        await bs.settle(c, t.account_id, runs[0], unknown_usage=False)
        await bs.settle(c, t.account_id, runs[0], unknown_usage=False)  # idempotent
        res = await bs.settle(c, t.account_id, runs[1], unknown_usage=True)
    assert res is not None and res.status == "pending" and res.spent_usd == Decimal("1.50")
    async with api_db.transaction(_ctx(t)) as c:
        acct = await bs.repo.get(c, "account_day", str(t.account_id), day)
        plat = await bs.repo.get(c, "platform_day", "platform", day)
    assert acct is not None and plat is not None
    # run0 spent 0.40, run1 conservatively 1.50; run2 still holds 1.50.
    assert (acct.spent_usd, acct.reserved_usd) == (Decimal("1.90"), Decimal("1.50"))
    assert (plat.spent_usd, plat.reserved_usd) == (Decimal("1.90"), Decimal("1.50"))


async def test_demo_credit_cannot_raise_real_caps(api_db: Database, migrator: Engine) -> None:
    day = _isolated_day()
    bs = _budget(day)
    t = make_tenant(migrator)
    grant(migrator, t, "100000.00")  # a huge demo balance changes nothing about real spend
    runs = [make_run(migrator, t) for _ in range(4)]
    async with api_db.transaction(_ctx(t)) as c:
        for r in runs[:3]:
            await bs.reserve(c, t.account_id, r, Decimal("1.50"))
    with pytest.raises(DomainError):
        async with api_db.transaction(_ctx(t)) as c:
            await bs.reserve(c, t.account_id, runs[3], Decimal("1.50"))
