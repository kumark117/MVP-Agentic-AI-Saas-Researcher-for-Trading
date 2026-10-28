"""Database-enforced invariants: balanced immutable ledger, immutable messages, frozen rate cards."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from alloyiq.repositories.db import Database, DbContext

from .conftest import Tenant

pytestmark = pytest.mark.integration

TXN = text(
    "INSERT INTO ledger_transactions (id, account_id, business_event_id, kind, description)"
    " VALUES (:id, :a, :b, 'adjustment', 'test')"
)
POST = text(
    "INSERT INTO ledger_postings (transaction_id, account_id, ledger_account, amount, currency)"
    " VALUES (:t, :a, :acct, :amt, :cur)"
)


def _ctx(t: Tenant) -> DbContext:
    return DbContext(account_id=t.account_id, user_id=t.user_id)


async def _journal(db: Database, t: Tenant, legs: list[tuple[str, str, str]]) -> uuid.UUID:
    tid = uuid.uuid4()
    async with db.transaction(_ctx(t)) as c:
        await c.execute(TXN, {"id": tid, "a": t.account_id, "b": f"test:{tid}"})
        for acct, amt, cur in legs:
            await c.execute(
                POST,
                {
                    "t": tid,
                    "a": t.account_id,
                    "acct": acct.format(a=t.account_id),
                    "amt": Decimal(amt),
                    "cur": cur,
                },
            )
    return tid


async def test_balanced_journal_commits(api_db: Database, tenant_a: Tenant) -> None:
    tid = await _journal(
        api_db, tenant_a, [("wallet:{a}", "10.50", "INR"), ("sim_adjustments", "-10.50", "INR")]
    )
    async with api_db.transaction(_ctx(tenant_a)) as c:
        total = (
            await c.execute(
                text("SELECT sum(amount) FROM ledger_postings WHERE transaction_id = :t"),
                {"t": tid},
            )
        ).scalar()
    assert total == 0


@pytest.mark.parametrize(
    "legs",
    [
        [("wallet:{a}", "10.00", "INR"), ("sim_adjustments", "-9.99", "INR")],  # unbalanced
        [("wallet:{a}", "10.00", "INR")],  # single posting
        [],  # header without postings
        [("wallet:{a}", "10.00", "USD"), ("sim_adjustments", "-10.00", "USD")],  # wrong currency
    ],
)
async def test_invalid_journals_are_rejected_at_commit(
    api_db: Database, tenant_a: Tenant, legs: list[tuple[str, str, str]]
) -> None:
    with pytest.raises(DBAPIError):
        await _journal(api_db, tenant_a, legs)


async def test_unknown_ledger_account_rejected(api_db: Database, tenant_a: Tenant) -> None:
    with pytest.raises(IntegrityError):
        await _journal(
            api_db, tenant_a, [("cash", "1.00", "INR"), ("sim_adjustments", "-1.00", "INR")]
        )


async def test_postings_and_journals_are_immutable(
    migrator: Engine, api_db: Database, tenant_a: Tenant
) -> None:
    tid = await _journal(
        api_db, tenant_a, [("wallet:{a}", "1.00", "INR"), ("sim_adjustments", "-1.00", "INR")]
    )
    # Even the table owner cannot edit or delete (trigger), and the API role lacks the grant.
    for stmt in (
        "UPDATE ledger_postings SET amount = 2 WHERE transaction_id = :t",
        "DELETE FROM ledger_postings WHERE transaction_id = :t",
        "UPDATE ledger_transactions SET description = 'x' WHERE id = :t",
    ):
        with pytest.raises(DBAPIError), migrator.begin() as c:
            c.execute(text(stmt), {"t": tid})
        with pytest.raises(DBAPIError):
            async with api_db.transaction(_ctx(tenant_a)) as c:
                await c.execute(text(stmt), {"t": tid})


async def test_duplicate_business_event_rejected(api_db: Database, tenant_a: Tenant) -> None:
    tid = uuid.uuid4()
    async with api_db.transaction(_ctx(tenant_a)) as c:
        await c.execute(TXN, {"id": tid, "a": tenant_a.account_id, "b": "dup:event"})
        await c.execute(
            POST,
            {
                "t": tid,
                "a": tenant_a.account_id,
                "acct": f"wallet:{tenant_a.account_id}",
                "amt": 1,
                "cur": "INR",
            },
        )
        await c.execute(
            POST,
            {
                "t": tid,
                "a": tenant_a.account_id,
                "acct": "sim_adjustments",
                "amt": -1,
                "cur": "INR",
            },
        )
    with pytest.raises(IntegrityError):
        async with api_db.transaction(_ctx(tenant_a)) as c:
            await c.execute(TXN, {"id": uuid.uuid4(), "a": tenant_a.account_id, "b": "dup:event"})


async def test_wallet_cannot_go_negative(api_db: Database, tenant_a: Tenant) -> None:
    with pytest.raises(IntegrityError):
        async with api_db.transaction(_ctx(tenant_a)) as c:
            await c.execute(
                text("UPDATE wallets SET reserved = 5 WHERE account_id = :a"),
                {"a": tenant_a.account_id},
            )


async def test_messages_are_immutable(api_db: Database, tenant_a: Tenant) -> None:
    async with api_db.transaction(_ctx(tenant_a)) as c:
        conv = (
            await c.execute(
                text(
                    "INSERT INTO conversations (account_id, user_id) VALUES (:a, :u) RETURNING id"
                ),
                {"a": tenant_a.account_id, "u": tenant_a.user_id},
            )
        ).scalar()
        mid = (
            await c.execute(
                text(
                    "INSERT INTO messages (account_id, conversation_id, user_id, role, content)"
                    " VALUES (:a, :c, :u, 'user', 'hello') RETURNING id"
                ),
                {"a": tenant_a.account_id, "c": conv, "u": tenant_a.user_id},
            )
        ).scalar()
    with pytest.raises(DBAPIError):
        async with api_db.transaction(_ctx(tenant_a)) as c:
            await c.execute(
                text("UPDATE messages SET content = 'edited' WHERE id = :m"), {"m": mid}
            )


def test_active_rate_card_is_frozen(migrator: Engine) -> None:
    version = f"test-{uuid.uuid4().hex[:8]}"
    with migrator.begin() as c:
        cid = c.execute(
            text(
                "INSERT INTO rate_cards (version, demo_fx_usd_inr, demo_markup, fx_rule, source_note)"
                " VALUES (:v, 85, 1.25, 'test', 'test') RETURNING id"
            ),
            {"v": version},
        ).scalar()
        c.execute(
            text(
                "INSERT INTO rate_card_items (rate_card_id, model_id, token_category,"
                " provider_usd_per_mtok, verified, source_note)"
                " VALUES (:c, 'm', 'input', 1, true, 'test')"
            ),
            {"c": cid},
        )
        c.execute(
            text("UPDATE rate_cards SET status = 'active', activated_at = now() WHERE id = :c"),
            {"c": cid},
        )
    for stmt in (
        "UPDATE rate_cards SET demo_markup = 2 WHERE id = :c",
        "UPDATE rate_card_items SET provider_usd_per_mtok = 9 WHERE rate_card_id = :c",
        "INSERT INTO rate_card_items (rate_card_id, model_id, token_category, source_note)"
        " VALUES (:c, 'm', 'output', 'x')",
        "DELETE FROM rate_cards WHERE id = :c",
    ):
        with pytest.raises(DBAPIError), migrator.begin() as c:
            c.execute(text(stmt), {"c": cid})
    with migrator.begin() as c:  # retirement is the one allowed change
        c.execute(
            text("UPDATE rate_cards SET status = 'retired', effective_to = now() WHERE id = :c"),
            {"c": cid},
        )
