"""SettlementService: resolve a terminal run's demo reservation and real-spend hold once
(build prompt section 8.7; R038, R041, R043; D058, D062).

Runs inside the caller's transaction, after the run row is locked by the terminal CAS.
Idempotent: only an active reservation settles, so a replay or a second worker is a no-op.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from alloyiq.api.schemas.common import BillingStatus, Money
from alloyiq.api.schemas.events import BillingUpdated
from alloyiq.application.budget import BudgetService
from alloyiq.application.wallet import WalletService
from alloyiq.repositories.audit import audit
from alloyiq.repositories.events import append_run_event
from alloyiq.repositories.runs import RunRepository
from alloyiq.repositories.usage import UsageRepository


class SettlementService:
    def __init__(
        self, wallet: WalletService, budget: BudgetService, runs: RunRepository | None = None
    ) -> None:
        self.wallet = wallet
        self.budget = budget
        self.runs = runs or RunRepository()

    async def settle(
        self, conn: AsyncConnection, run: dict[str, Any], *, request_id: str | None = None
    ) -> bool:
        run_id: UUID = run["id"]
        account_id: UUID = run["account_id"]
        rate_card = (run.get("versions") or {}).get("rate_card", "")
        # Calls interrupted mid-flight (cancel, crash) never get blind zero usage: D038.
        await UsageRepository().mark_ambiguous(conn, run_id, rate_card)
        charge, unknown = await self.runs.usage_totals(conn, run_id)
        split = await self.wallet.settle(
            conn,
            account_id,
            run_id,
            charge,
            unknown_usage=unknown,
            rate_card_version=(run.get("versions") or {}).get("rate_card"),
            request_id=request_id,
        )
        await self.budget.settle(conn, account_id, run_id, unknown_usage=unknown)
        if split is None:
            return False
        status: BillingStatus
        if split.pending > 0:
            status = "pending_settlement"
        elif split.debit > 0:
            status = "settled"
        else:
            status = "released"
        await self.runs.update_run(
            conn,
            run_id,
            {
                "billing_status": status,
                "settled_charge": split.debit,
                "pending_amount": split.pending,
                "released_amount": split.released,
            },
        )
        await append_run_event(
            conn,
            account_id,
            run_id,
            "billing.updated",
            BillingUpdated(
                billing_status=status,
                settled_charge=Money.inr(split.debit),
                released_amount=Money.inr(split.released),
                pending_amount=Money.inr(split.pending),
            ),
            request_id=request_id,
        )
        await audit(
            conn,
            action="run.settled",
            outcome=status,
            account_id=account_id,
            actor_kind="system",
            run_id=run_id,
            metadata={
                "exact_charge": str(split.exact_charge),
                "debit": str(split.debit),
                "rounding_adjustment": str(split.rounding_adjustment),
                "pending": str(split.pending),
                "released": str(split.released),
                "capped": split.capped,
                "payment_mode": "simulated",
            },
        )
        return True
