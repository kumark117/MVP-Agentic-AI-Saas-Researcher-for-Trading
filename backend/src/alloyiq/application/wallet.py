"""WalletService: demo credit balance, ledger view, reservations and settlement (R038, D060-D063).

available = posted_credit - posted_debit - active reservations - pending holds, computed
from postings and reservations. Demo credit is simulated and has no cash value.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from alloyiq.api.schemas.common import Money
from alloyiq.api.schemas.events import BalanceUpdated
from alloyiq.billing import posting
from alloyiq.billing.posting import SettlementSplit, split_settlement
from alloyiq.domain.errors import DomainError, ErrorCode
from alloyiq.repositories.audit import ActorKind, audit
from alloyiq.repositories.db import Database, DbContext
from alloyiq.repositories.events import append_account_event
from alloyiq.repositories.ledger import Posted, WalletBalance, WalletRepository
from alloyiq.repositories.paging import decode_cursor, encode_cursor

PENDING_HOLD_HOURS = 24  # D061: bounded pending reserve for unknown usage


def insufficient_credit() -> DomainError:
    return DomainError(
        409,
        ErrorCode.INSUFFICIENT_CREDIT,
        "Not enough demo credit",
        "Your available demo credit is lower than this run's maximum charge. Top up "
        "(simulated) or choose a cheaper profile.",
    )


@dataclass(frozen=True, slots=True)
class LedgerPage:
    items: list[dict[str, Any]]
    next_cursor: str | None


class WalletService:
    def __init__(
        self,
        db: Database,
        *,
        opening_credit: Decimal,
        repo: WalletRepository | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.db = db
        self.opening_credit = opening_credit
        self.repo = repo or WalletRepository()
        self.now = clock

    # ------------------------------------------------------------------ reads
    async def balance(self, account_id: UUID, user_id: UUID | None = None) -> WalletBalance:
        async with self.db.transaction(DbContext(account_id=account_id, user_id=user_id)) as conn:
            return await self.repo.computed_balance(conn, account_id)

    async def ledger(
        self,
        account_id: UUID,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        kind: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> LedgerPage:
        after = decode_cursor(cursor)
        async with self.db.transaction(DbContext(account_id=account_id)) as conn:
            rows = await self.repo.list_entries(
                conn, account_id, start=start, end=end, kind=kind, after=after, limit=limit + 1
            )
        more = len(rows) > limit
        rows = rows[:limit]
        nxt = encode_cursor(rows[-1]["posted_at"], rows[-1]["id"]) if more and rows else None
        return LedgerPage(rows, nxt)

    # ------------------------------------------------------------------ journals
    async def post(
        self,
        conn: AsyncConnection,
        account_id: UUID,
        journal: posting.Journal,
        *,
        actor_kind: ActorKind = "system",
        actor_user_id: UUID | None = None,
        request_id: str | None = None,
        notify: bool = True,
        **refs: Any,
    ) -> Posted:
        """Post once per business event, audit it and emit balance.updated (same txn)."""
        posted = await self.repo.post(conn, account_id, journal, **refs)
        if posted.created:
            await audit(
                conn,
                action=f"ledger.{journal.kind}",
                outcome="posted",
                account_id=account_id,
                actor_user_id=actor_user_id,
                actor_kind=actor_kind,
                target_type="ledger_transaction",
                target_id=str(posted.transaction_id),
                request_id=request_id,
                run_id=refs.get("run_id"),
                metadata={
                    "business_event_id": journal.business_event_id,
                    "wallet_delta": str(journal.wallet_delta(account_id)),
                    "payment_mode": "simulated",
                },
            )
            if notify:
                await self.emit_balance(conn, account_id, request_id=request_id)
        return posted

    async def emit_balance(
        self, conn: AsyncConnection, account_id: UUID, *, request_id: str | None = None
    ) -> None:
        b = await self.repo.computed_balance(conn, account_id)
        await append_account_event(
            conn,
            account_id,
            "balance.updated",
            BalanceUpdated(
                available=Money.inr(b.available),
                reserved=Money.inr(b.reserved),
                pending_settlement=Money.inr(b.pending_settlement),
            ),
            request_id=request_id,
        )

    async def grant_opening_credit(
        self, conn: AsyncConnection, account_id: UUID, user_id: UUID
    ) -> None:
        """AuthService.on_verified hook: one opening grant per account, ever."""
        if self.opening_credit <= 0:
            return
        await self.post(
            conn,
            account_id,
            posting.opening_grant(account_id, self.opening_credit),
            actor_user_id=user_id,
        )

    # ------------------------------------------------------------------ reservations
    async def reserve(
        self, conn: AsyncConnection, account_id: UUID, run_id: UUID, amount: Decimal
    ) -> UUID:
        """Admission step: hold the run's maximum charge or raise INSUFFICIENT_CREDIT."""
        rid = await self.repo.reserve(conn, account_id, run_id, amount)
        if rid is None:
            raise insufficient_credit()
        return rid

    async def settle(
        self,
        conn: AsyncConnection,
        account_id: UUID,
        run_id: UUID,
        exact_charge: Decimal,
        *,
        unknown_usage: bool,
        rate_card_version: str | None,
        request_id: str | None = None,
    ) -> SettlementSplit | None:
        """Settle the run's reservation once (build prompt section 8.7). Idempotent."""
        await self.repo.lock_wallet(conn, account_id)  # wallet first: the global lock order
        res = await self.repo.get_reservation(conn, run_id, lock=True)
        if res is None or res.status != "active":
            return None
        split = split_settlement(res.amount, exact_charge, unknown_usage=unknown_usage)
        await self.repo.apply_settlement(conn, res, split, PENDING_HOLD_HOURS)
        if split.debit > 0:
            await self.post(
                conn,
                account_id,
                posting.run_debit(account_id, run_id, split.debit),
                run_id=run_id,
                rate_card_version=rate_card_version,
                rounding_adjustment=split.rounding_adjustment,
                request_id=request_id,
                notify=False,
            )
        await self.emit_balance(conn, account_id, request_id=request_id)
        return split

    async def release_due_pending(self) -> int:
        """Scheduler: release pending holds older than 24 h (worker, system scope)."""
        async with self.db.transaction(DbContext.for_system()) as conn:
            due = await self.repo.due_pending(conn)
        released = 0
        for res in due:
            async with self.db.transaction(DbContext(account_id=res.account_id)) as conn:
                await self.repo.lock_wallet(conn, res.account_id)
                current = await self.repo.get_reservation(conn, res.run_id, lock=True)
                if current is None or current.status != "pending":
                    continue
                amount = await self.repo.release_pending(conn, current)
                await audit(
                    conn,
                    action="reservation.pending_released",
                    outcome="released",
                    account_id=res.account_id,
                    actor_kind="system",
                    run_id=res.run_id,
                    metadata={"amount": str(amount), "policy": "24h unknown-usage hold"},
                )
                await self.emit_balance(conn, res.account_id)
                released += 1
        return released

    async def verify_projection(self, conn: AsyncConnection, account_id: UUID) -> bool:
        return await self.repo.projection(conn, account_id) == await self.repo.computed_balance(
            conn, account_id
        )
