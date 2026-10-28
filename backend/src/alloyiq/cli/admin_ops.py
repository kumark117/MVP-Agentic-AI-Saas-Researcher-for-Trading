# ruff: noqa: E501 - SQL kept on readable single lines
"""alloyiq-admin operations commands: summary, runs, audit, suspend (section 17; DV-02, DV-05).

The platform summary is CLI-only in the MVP (no privileged HTTP surface). Output never
includes emails, prompts or report text: accounts are shown by ID and pseudonym.
"""

from __future__ import annotations

import argparse
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from alloyiq.cli.admin import _out, command
from alloyiq.repositories.audit import audit
from alloyiq.repositories.db import Database, DbContext


def _range_args(p: argparse.ArgumentParser) -> None:
    now = datetime.now(UTC)
    p.add_argument(
        "--from", dest="start", type=datetime.fromisoformat, default=now - timedelta(days=1)
    )
    p.add_argument("--to", dest="end", type=datetime.fromisoformat, default=now)


@command(
    "summary",
    "Platform summary: runs, outcomes, real spend vs caps, demo money (CLI only)",
    _range_args,
)
async def summary(db: Database, ns: argparse.Namespace) -> int:
    p = {"s": ns.start, "e": ns.end}
    async with db.transaction(DbContext.for_system()) as c:
        runs = (
            await c.execute(
                text(
                    "SELECT status, count(*) FROM research_runs WHERE submitted_at >= :s AND submitted_at < :e GROUP BY 1"
                ),
                p,
            )
        ).all()
        spend = (
            await c.execute(
                text(
                    "SELECT scope, period, limit_usd, spent_usd, reserved_usd FROM real_budget_accounts"
                    " WHERE scope = 'platform_day' AND period BETWEEN CAST(:s AS date) AND CAST(:e AS date) ORDER BY period"
                ),
                p,
            )
        ).all()
        cost = (
            await c.execute(
                text(
                    "SELECT coalesce(sum(provider_cost_usd), 0), count(*) FILTER (WHERE usage_status = 'unknown')"
                    " FROM usage_records WHERE created_at >= :s AND created_at < :e"
                ),
                p,
            )
        ).one()
        demo = (
            await c.execute(
                text(
                    "SELECT kind, coalesce(sum(p.amount), 0) FROM ledger_transactions t JOIN ledger_postings p"
                    " ON p.transaction_id = t.id AND p.ledger_account LIKE 'wallet:%'"
                    " WHERE t.posted_at >= :s AND t.posted_at < :e GROUP BY 1"
                ),
                p,
            )
        ).all()
        incidents = (
            await c.execute(
                text("SELECT kind, count(*) FROM incidents WHERE resolved_at IS NULL GROUP BY 1")
            )
        ).all()
        members = (
            await c.execute(text("SELECT count(*) FROM accounts WHERE status = 'active'"))
        ).scalar()
    _out(
        {
            "interval": {"from": ns.start, "to": ns.end},
            "active_accounts": members,
            "runs_by_status": {s: n for s, n in runs},
            "real_provider_cost_usd": {
                "calculated": str(cost[0]),
                "unknown_usage_calls": cost[1],
                "status": "calculated, not reconciled (DV-07)",
            },
            "platform_day_budgets": [
                {
                    "period": str(r.period),
                    "limit_usd": str(r.limit_usd),
                    "spent_usd": str(r.spent_usd),
                    "reserved_usd": str(r.reserved_usd),
                }
                for r in spend
            ],
            "demo_wallet_movements_inr_simulated": {k: str(v) for k, v in demo},
            "money_collected": "INR 0.00",
            "open_incidents": {k: n for k, n in incidents},
        }
    )
    return 0


def _runs_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--account", required=True, type=uuid.UUID)
    p.add_argument("--limit", type=int, default=20)


@command("runs", "Recent runs for one account (IDs, states, timings, charges)", _runs_args)
async def runs(db: Database, ns: argparse.Namespace) -> int:
    async with db.transaction(DbContext.for_system()) as c:
        rows = (
            await c.execute(
                text(
                    "SELECT id, status, billing_status, outcome_reason, error_code, submitted_at, terminal_at,"
                    " settled_charge, instrument_snapshot->>'canonical_id' AS instrument, horizon"
                    " FROM research_runs WHERE account_id = :a ORDER BY submitted_at DESC LIMIT :n"
                ),
                {"a": ns.account, "n": ns.limit},
            )
        ).all()
    _out([dict(r._mapping) for r in rows])
    return 0


def _audit_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--account", type=uuid.UUID)
    p.add_argument("--action", help="prefix filter, e.g. ledger. or auth.")
    p.add_argument("--limit", type=int, default=50)


@command("audit", "Recent audit events (metadata is already redacted)", _audit_args)
async def audit_list(db: Database, ns: argparse.Namespace) -> int:
    async with db.transaction(DbContext.for_system()) as c:
        rows = (
            await c.execute(
                text(
                    "SELECT created_at, account_id, actor_kind, action, outcome, target_type, target_id, run_id, metadata"
                    " FROM audit_events WHERE (CAST(:a AS uuid) IS NULL OR account_id = :a)"
                    " AND (CAST(:p AS text) IS NULL OR action LIKE :p || '%') ORDER BY created_at DESC LIMIT :n"
                ),
                {"a": ns.account, "p": ns.action, "n": ns.limit},
            )
        ).all()
    _out([dict(r._mapping) for r in rows])
    return 0


def _suspend_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("account", type=uuid.UUID)
    p.add_argument("--reason", required=True)
    p.add_argument("--lift", action="store_true", help="reactivate instead of suspending")


@command("suspend", "Suspend (or --lift) an account: sessions end, no new research", _suspend_args)
async def suspend(db: Database, ns: argparse.Namespace) -> int:
    status = "active" if ns.lift else "suspended"
    async with db.transaction(DbContext.for_system()) as c:
        n = (
            await c.execute(
                text("UPDATE accounts SET status = :s WHERE id = :a AND status <> 'deleted'"),
                {"s": status, "a": ns.account},
            )
        ).rowcount
        if n and not ns.lift:
            await c.execute(
                text(
                    "UPDATE sessions SET revoked_at = now(), revoked_reason = 'account_suspended'"
                    " WHERE account_id = :a AND revoked_at IS NULL"
                ),
                {"a": ns.account},
            )
        await audit(
            c,
            action="account.suspend" if not ns.lift else "account.reactivate",
            outcome="applied" if n else "not_found",
            account_id=ns.account if n else None,
            actor_kind="admin_cli",
            target_type="account",
            target_id=str(ns.account),
            metadata={"reason": ns.reason[:200]},
        )
    _out({"account": str(ns.account), "status": status if n else "not found"})
    return 0 if n else 2
