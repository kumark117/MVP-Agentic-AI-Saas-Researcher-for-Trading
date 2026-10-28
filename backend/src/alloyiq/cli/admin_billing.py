"""alloyiq-admin billing commands: grant-credit, rate-card-activate, reconcile-payments.

All are owner-only (host CLI, migrator role) and audited with actor_kind=admin_cli.
"""

from __future__ import annotations

import argparse
import json
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import text

from alloyiq.billing import posting
from alloyiq.cli.admin import _out, command
from alloyiq.repositories.audit import audit
from alloyiq.repositories.db import Database, DbContext


def _grant_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--account", required=True, type=uuid.UUID, help="account UUID")
    p.add_argument("--amount", required=True, help="INR, e.g. 250.00 (negative to deduct)")
    p.add_argument("--reason", required=True, help="recorded on the journal and audit event")
    p.add_argument("--event-id", help="idempotency: re-running with the same ID is a no-op")


@command("grant-credit", "Post an audited demo-credit adjustment journal", _grant_args)
async def grant_credit(db: Database, ns: argparse.Namespace) -> int:
    from alloyiq.application.wallet import WalletService

    try:
        amount = Decimal(ns.amount)
    except InvalidOperation:
        print("amount must be a decimal such as 250.00")
        return 2
    if amount == 0 or amount != amount.quantize(Decimal("0.01")):
        print("amount must be non-zero with at most 2 decimal places")
        return 2
    event_id = ns.event_id or str(uuid.uuid4())
    wallet = WalletService(db, opening_credit=Decimal(0))
    async with db.transaction(DbContext(account_id=ns.account, system=True)) as conn:
        journal = posting.adjustment(ns.account, event_id, amount, ns.reason)
        posted = await wallet.post(
            conn, ns.account, journal, actor_kind="admin_cli", operator_reason=ns.reason
        )
        balance = await wallet.repo.computed_balance(conn, ns.account)
    _out(
        {
            "transaction_id": str(posted.transaction_id),
            "created": posted.created,
            "business_event_id": journal.business_event_id,
            "available": str(balance.available),
            "payment_mode": "simulated",
        }
    )
    return 0


def _activate_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("version", help="rate card version, e.g. v1-2026-10-06")
    p.add_argument(
        "--confirm-prices-verified",
        action="store_true",
        help="you checked every listed price against Anthropic's live pricing page (OD-3)",
    )


@command(
    "rate-card-activate",
    "Activate a draft rate card (retires the current one); prices become immutable",
    _activate_args,
)
async def rate_card_activate(db: Database, ns: argparse.Namespace) -> int:
    async with db.transaction(DbContext.for_system()) as conn:
        card = (
            await conn.execute(
                text("SELECT id, status FROM rate_cards WHERE version = :v FOR UPDATE"),
                {"v": ns.version},
            )
        ).first()
        if card is None:
            print(f"no rate card {ns.version}")
            return 2
        if card.status != "draft":
            print(f"rate card {ns.version} is {card.status}; only drafts can be activated")
            return 2
        items = [
            dict(r._mapping)
            for r in await conn.execute(
                text(
                    "SELECT model_id, token_category, provider_usd_per_mtok FROM rate_card_items"
                    " WHERE rate_card_id = :c ORDER BY model_id, token_category"
                ),
                {"c": card.id},
            )
        ]
        priced = [i for i in items if i["provider_usd_per_mtok"] is not None]
        unpriced = [f"{i['model_id']}:{i['token_category']}" for i in items if i not in priced]
        if not ns.confirm_prices_verified:
            _out(
                {
                    "refused": "re-run with --confirm-prices-verified after checking each price",
                    "prices_to_verify": [
                        f"{i['model_id']}:{i['token_category']} = "
                        f"{i['provider_usd_per_mtok']} USD/MTok"
                        for i in priced
                    ],
                    "unpriced_will_be_cost_status_unknown": unpriced,
                }
            )
            return 3
        await conn.execute(
            text(
                "UPDATE rate_card_items SET verified = true"
                " WHERE rate_card_id = :c AND provider_usd_per_mtok IS NOT NULL"
            ),
            {"c": card.id},
        )
        await conn.execute(
            text(
                "UPDATE rate_cards SET status = 'retired', effective_to = now()"
                " WHERE status = 'active'"
            )
        )
        await conn.execute(
            text(
                "UPDATE rate_cards SET status = 'active', effective_from = now(),"
                " activated_at = now() WHERE id = :c"
            ),
            {"c": card.id},
        )
        await audit(
            conn,
            action="rate_card.activate",
            outcome="activated",
            account_id=None,
            actor_kind="admin_cli",
            target_type="rate_card",
            target_id=ns.version,
            metadata={"verified_items": len(priced), "unpriced": unpriced},
        )
    _out({"activated": ns.version, "verified_items": len(priced), "unpriced": unpriced})
    return 0


def _reconcile_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "kind", choices=["payments", "usage"], help="demo ledger (payments) or provider usage"
    )
    p.add_argument("--repair", action="store_true", help="rebuild drifted wallet projections")


@command(
    "reconcile",
    "Check demo wallets against postings, and paid checkouts against top-up journals",
    _reconcile_args,
)
async def reconcile(db: Database, ns: argparse.Namespace) -> int:
    if ns.kind == "usage":
        # Provider-invoice reconciliation is deferred (DV-07): cost_status stays calculated.
        _out({"kind": "usage", "status": "deferred (DV-07)", "reconciled": 0})
        return 0
    from alloyiq.repositories.ledger import WalletRepository

    repo = WalletRepository()
    problems: list[dict[str, Any]] = []
    async with db.transaction(DbContext.for_system()) as conn:
        accounts = (await conn.execute(text("SELECT account_id FROM wallets"))).scalars().all()
        for account_id in accounts:
            projection = await repo.projection(conn, account_id)
            computed = await repo.computed_balance(conn, account_id)
            if projection != computed:
                problems.append(
                    {"account_id": str(account_id), "kind": "projection_drift", "fixed": ns.repair}
                )
                if ns.repair:
                    await repo.rebuild_projection(conn, account_id)
        mismatched = await conn.execute(
            text(
                "SELECT c.id, c.account_id, count(t.id) AS journals FROM checkouts c"
                " LEFT JOIN ledger_transactions t ON t.business_event_id = 'topup:' || c.id"
                " GROUP BY c.id, c.account_id, c.status"
                " HAVING (c.status = 'paid') <> (count(t.id) = 1) OR count(t.id) > 1"
            )
        )
        for row in mismatched:
            problems.append(
                {
                    "account_id": str(row.account_id),
                    "kind": "checkout_journal_mismatch",
                    "checkout_id": str(row.id),
                    "journals": row.journals,
                }
            )
        for p in problems:
            await conn.execute(
                text(
                    "INSERT INTO incidents (account_id, kind, details)"
                    " VALUES (:a, 'ledger_mismatch', CAST(:d AS jsonb))"
                ),
                {"a": p["account_id"], "d": json.dumps(p)},
            )
        await audit(
            conn,
            action="reconcile.payments",
            outcome="clean" if not problems else "mismatches",
            account_id=None,
            actor_kind="admin_cli",
            metadata={"wallets": len(accounts), "problems": len(problems)},
        )
    _out({"wallets_checked": len(accounts), "problems": problems})
    return 0 if not problems else 1


# ------------------------------------------------------------------ evaluation (R069-R070)
def _eval_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dataset", default="v1")
    p.add_argument("--case", action="append", help="run only these case ids")
    p.add_argument(
        "--live",
        action="store_true",
        help="real providers and Claude for live_ok cases (needs LIVE_PROVIDER_TESTS=1)",
    )


@command("eval", "Run the evaluation set and record scores (offline = synthetic)", _eval_args)
async def eval_cmd(db: Database, ns: argparse.Namespace) -> int:
    import os
    import tempfile

    from alloyiq.adapters.artifacts import LocalArtifactStore
    from alloyiq.config import get_settings, secret_or_none
    from alloyiq.evaluations.harness import EvalHarness, load_dataset
    from alloyiq.workflows.factory import PostgresCheckpointer, checkpoint_dsn

    settings = get_settings()
    url = os.environ.get("DATABASE_MIGRATOR_URL") or os.environ.get("ALEMBIC_DATABASE_URL") or ""
    live_gateway = live_tools = None
    if ns.live:
        if os.environ.get("LIVE_PROVIDER_TESTS") != "1":
            print(
                "Refusing: --live spends real money; set LIVE_PROVIDER_TESTS=1 with owner approval."
            )
            return 3
        from alloyiq.adapters.anthropic_gateway import AnthropicLLMGateway
        from alloyiq.tools.gateway import MCPToolGateway

        key = secret_or_none(settings.anthropic_api_key)
        if not key:
            print("ANTHROPIC_API_KEY is not configured")
            return 2
        live_gateway = AnthropicLLMGateway(key)
        live_tools = MCPToolGateway(settings.mcp_url, settings.mcp_service_token.get_secret_value())
    harness = EvalHarness(
        db,
        artifacts=LocalArtifactStore(tempfile.mkdtemp(prefix="alloyiq-eval-")),
        checkpointer=PostgresCheckpointer(checkpoint_dsn(url)),
        live_gateway=live_gateway,
        live_tools=live_tools,
    )
    s = await harness.run(load_dataset(ns.dataset), case_ids=ns.case)
    _out(
        {
            "evaluation_run_id": str(s.run_id),
            "cases": s.cases,
            "checks": {k: f"{p}/{n}" for k, (p, n) in s.by_check.items()},
            "citation_support": round(s.citation_support, 4),
            "release_gates_passed": s.gates_passed,
            "failures": s.failures,
        }
    )
    return 0 if s.gates_passed else 1
