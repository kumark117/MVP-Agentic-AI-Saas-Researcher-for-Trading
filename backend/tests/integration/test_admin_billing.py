"""alloyiq-admin billing commands against the test database (migrator role)."""

from __future__ import annotations

import argparse
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy import Engine, text

from alloyiq.application.wallet import WalletService
from alloyiq.cli import admin_billing
from alloyiq.repositories.db import Database

from .conftest import MIGRATOR_URL, make_tenant

pytestmark = pytest.mark.integration


@pytest.fixture
async def admin_db() -> AsyncIterator[Database]:
    if not MIGRATOR_URL:
        pytest.skip("TEST_MIGRATOR_DATABASE_URL not set")
    db = Database(MIGRATOR_URL.replace("+psycopg", "+asyncpg"), pool_size=1, max_overflow=0)
    yield db
    await db.dispose()


async def test_grant_credit_is_audited_and_idempotent(admin_db: Database, migrator: Engine) -> None:
    t = make_tenant(migrator)
    ns = argparse.Namespace(
        account=t.account_id,
        amount="125.50",
        reason="support goodwill",
        event_id="evt-1-" + t.email,
    )
    assert await admin_billing.grant_credit(admin_db, ns) == 0
    assert await admin_billing.grant_credit(admin_db, ns) == 0  # same event ID: no-op
    b = await WalletService(admin_db, opening_credit=Decimal(0)).balance(t.account_id)
    assert b.available == Decimal("125.50")
    with migrator.connect() as c:
        actors = (
            c.execute(
                text(
                    "SELECT actor_kind FROM audit_events WHERE account_id = :a"
                    " AND action = 'ledger.adjustment'"
                ),
                {"a": t.account_id},
            )
            .scalars()
            .all()
        )
    assert actors == ["admin_cli"]
    bad = argparse.Namespace(account=t.account_id, amount="1.001", reason="x", event_id=None)
    assert await admin_billing.grant_credit(admin_db, bad) == 2


async def test_reconcile_detects_and_repairs_projection_drift(
    admin_db: Database, migrator: Engine
) -> None:
    t = make_tenant(migrator)
    with migrator.begin() as c:  # simulate a corrupted projection
        c.execute(
            text("UPDATE wallets SET posted_credit = 999 WHERE account_id = :a"),
            {"a": t.account_id},
        )
    ns = argparse.Namespace(kind="payments", repair=True)
    assert await admin_billing.reconcile(admin_db, ns) == 1
    with migrator.connect() as c:
        credit = c.execute(
            text("SELECT posted_credit FROM wallets WHERE account_id = :a"), {"a": t.account_id}
        ).scalar()
    assert credit == 0


async def test_rate_card_activation_requires_confirmation(
    admin_db: Database, migrator: Engine
) -> None:
    version = f"v-test-{uuid.uuid4().hex[:8]}"
    with migrator.begin() as c:
        card = c.execute(
            text(
                "INSERT INTO rate_cards (version, demo_fx_usd_inr, demo_markup, fx_rule,"
                " source_note) VALUES (:v, 85, 1.25, 'demo', 'test') RETURNING id"
            ),
            {"v": version},
        ).scalar_one()
        c.execute(
            text(
                "INSERT INTO rate_card_items (rate_card_id, model_id, token_category,"
                " provider_usd_per_mtok, source_note) VALUES"
                " (:c, 'm', 'input', 1, 't'), (:c, 'm', 'cache_write', NULL, 't')"
            ),
            {"c": card},
        )
    no = argparse.Namespace(version=version, confirm_prices_verified=False)
    assert await admin_billing.rate_card_activate(admin_db, no) == 3
    yes = argparse.Namespace(version=version, confirm_prices_verified=True)
    try:
        assert await admin_billing.rate_card_activate(admin_db, yes) == 0
        with migrator.connect() as c:
            status = c.execute(
                text("SELECT status FROM rate_cards WHERE version = :v"), {"v": version}
            ).scalar()
            verified = c.execute(
                text(
                    "SELECT token_category, verified FROM rate_card_items WHERE rate_card_id = :c"
                    " ORDER BY token_category"
                ),
                {"c": card},
            ).all()
        assert status == "active"
        assert [tuple(v) for v in verified] == [("cache_write", False), ("input", True)]
    finally:  # leave the seeded draft card current for other tests
        with migrator.begin() as c:
            c.execute(
                text("UPDATE rate_cards SET status = 'retired' WHERE version = :v"), {"v": version}
            )
