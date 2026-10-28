"""WalletRepository: demo journals, wallet projection and credit reservations (D060, D063).

Every function runs inside the caller's transaction and locks the wallet row first, which
is the first lock in the fixed admission order (wallet -> account_day -> platform_day).
The wallet columns are a projection kept in step with postings and reservations in the
same transaction; computed_balance() derives the authoritative values from source rows,
and the projection can always be rebuilt from them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from alloyiq.billing.posting import Journal, SettlementSplit, wallet_account
from alloyiq.domain.money import ZERO


@dataclass(frozen=True, slots=True)
class WalletBalance:
    posted_credit: Decimal
    posted_debit: Decimal
    reserved: Decimal
    pending_settlement: Decimal

    @property
    def available(self) -> Decimal:
        return self.posted_credit - self.posted_debit - self.reserved - self.pending_settlement


@dataclass(frozen=True, slots=True)
class Posted:
    transaction_id: UUID
    created: bool  # False: the business event was already posted (idempotent no-op)


@dataclass(frozen=True, slots=True)
class ReservationRecord:
    id: UUID
    account_id: UUID
    run_id: UUID
    amount: Decimal
    settled_amount: Decimal
    released_amount: Decimal
    pending_amount: Decimal
    status: str
    expires_at: datetime | None


def _balance(row: Any) -> WalletBalance:
    return WalletBalance(
        Decimal(row.posted_credit),
        Decimal(row.posted_debit),
        Decimal(row.reserved),
        Decimal(row.pending_settlement),
    )


class WalletRepository:
    async def lock_wallet(
        self, conn: AsyncConnection, account_id: UUID
    ) -> tuple[UUID, WalletBalance]:
        row = (
            await conn.execute(
                text(
                    "SELECT id, posted_credit, posted_debit, reserved, pending_settlement"
                    " FROM wallets WHERE account_id = :a FOR UPDATE"
                ),
                {"a": account_id},
            )
        ).one()
        return row.id, _balance(row)

    async def projection(self, conn: AsyncConnection, account_id: UUID) -> WalletBalance:
        row = (
            await conn.execute(
                text(
                    "SELECT posted_credit, posted_debit, reserved, pending_settlement"
                    " FROM wallets WHERE account_id = :a"
                ),
                {"a": account_id},
            )
        ).one()
        return _balance(row)

    async def computed_balance(self, conn: AsyncConnection, account_id: UUID) -> WalletBalance:
        """Authoritative balance from postings and reservations (not the projection)."""
        row = (
            await conn.execute(
                text(
                    "SELECT"
                    " coalesce((SELECT sum(amount) FILTER (WHERE amount > 0) FROM ledger_postings"
                    "   WHERE account_id = :a AND ledger_account = :w), 0) AS posted_credit,"
                    " coalesce((SELECT -sum(amount) FILTER (WHERE amount < 0) FROM ledger_postings"
                    "   WHERE account_id = :a AND ledger_account = :w), 0) AS posted_debit,"
                    " coalesce((SELECT sum(amount) FROM reservations"
                    "   WHERE account_id = :a AND status = 'active'), 0) AS reserved,"
                    " coalesce((SELECT sum(pending_amount) FROM reservations"
                    "   WHERE account_id = :a AND status = 'pending'), 0) AS pending_settlement"
                ),
                {"a": account_id, "w": wallet_account(account_id)},
            )
        ).one()
        return _balance(row)

    async def rebuild_projection(self, conn: AsyncConnection, account_id: UUID) -> WalletBalance:
        await self.lock_wallet(conn, account_id)
        b = await self.computed_balance(conn, account_id)
        await conn.execute(
            text(
                "UPDATE wallets SET posted_credit = :c, posted_debit = :d, reserved = :r,"
                " pending_settlement = :p, updated_at = now() WHERE account_id = :a"
            ),
            {
                "a": account_id,
                "c": b.posted_credit,
                "d": b.posted_debit,
                "r": b.reserved,
                "p": b.pending_settlement,
            },
        )
        return b

    # ------------------------------------------------------------------ journals
    async def post(
        self,
        conn: AsyncConnection,
        account_id: UUID,
        journal: Journal,
        *,
        run_id: UUID | None = None,
        checkout_id: UUID | None = None,
        refund_id: UUID | None = None,
        reverses_id: UUID | None = None,
        rate_card_version: str | None = None,
        rounding_adjustment: Decimal = ZERO,
        operator_reason: str | None = None,
    ) -> Posted:
        """Post a balanced journal once per business event; repeats are no-ops (D063)."""
        await self.lock_wallet(conn, account_id)
        new_id = (
            await conn.execute(
                text(
                    "INSERT INTO ledger_transactions (account_id, business_event_id, kind,"
                    " currency, reverses_id, run_id, checkout_id, refund_id, rate_card_version,"
                    " rounding_adjustment, description, operator_reason)"
                    " VALUES (:a, :b, :k, :c, :rev, :run, :co, :rf, :rcv, :adj, :d, :op)"
                    " ON CONFLICT (business_event_id) DO NOTHING RETURNING id"
                ),
                {
                    "a": account_id,
                    "b": journal.business_event_id,
                    "k": journal.kind,
                    "c": journal.currency,
                    "rev": reverses_id,
                    "run": run_id,
                    "co": checkout_id,
                    "rf": refund_id,
                    "rcv": rate_card_version,
                    "adj": rounding_adjustment,
                    "d": journal.description,
                    "op": operator_reason,
                },
            )
        ).scalar()
        if new_id is None:
            existing = (
                await conn.execute(
                    text("SELECT id FROM ledger_transactions WHERE business_event_id = :b"),
                    {"b": journal.business_event_id},
                )
            ).scalar_one()
            return Posted(existing, False)
        for p in journal.postings:
            await conn.execute(
                text(
                    "INSERT INTO ledger_postings (transaction_id, account_id, ledger_account,"
                    " amount, currency) VALUES (:t, :a, :l, :m, :c)"
                ),
                {
                    "t": new_id,
                    "a": account_id,
                    "l": p.ledger_account,
                    "m": p.amount,
                    "c": p.currency,
                },
            )
        delta = journal.wallet_delta(account_id)
        await conn.execute(
            text(
                "UPDATE wallets SET posted_credit = posted_credit + :cr,"
                " posted_debit = posted_debit + :db, updated_at = now() WHERE account_id = :a"
            ),
            {"a": account_id, "cr": max(delta, ZERO), "db": max(-delta, ZERO)},
        )
        return Posted(new_id, True)

    async def find_transaction(self, conn: AsyncConnection, business_event_id: str) -> UUID | None:
        tid: UUID | None = (
            await conn.execute(
                text("SELECT id FROM ledger_transactions WHERE business_event_id = :b"),
                {"b": business_event_id},
            )
        ).scalar()
        return tid

    async def list_entries(
        self,
        conn: AsyncConnection,
        account_id: UUID,
        *,
        start: datetime | None,
        end: datetime | None,
        kind: str | None,
        after: tuple[datetime, UUID] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows = await conn.execute(
            text(
                "SELECT t.id, t.business_event_id, t.kind, t.description, t.run_id,"
                " t.checkout_id, t.refund_id, t.reverses_id, t.posted_at, p.amount, p.currency"
                " FROM ledger_transactions t JOIN ledger_postings p ON p.transaction_id = t.id"
                "  AND p.ledger_account = :w"
                " WHERE t.account_id = :a"
                "  AND (CAST(:start AS timestamptz) IS NULL OR t.posted_at >= :start)"
                "  AND (CAST(:end AS timestamptz) IS NULL OR t.posted_at < :end)"
                "  AND (CAST(:kind AS text) IS NULL OR t.kind = :kind)"
                "  AND (CAST(:cts AS timestamptz) IS NULL OR (t.posted_at, t.id) < (:cts, :cid))"
                " ORDER BY t.posted_at DESC, t.id DESC LIMIT :n"
            ),
            {
                "a": account_id,
                "w": wallet_account(account_id),
                "start": start,
                "end": end,
                "kind": kind,
                "cts": after[0] if after else None,
                "cid": after[1] if after else None,
                "n": limit,
            },
        )
        return [dict(r._mapping) for r in rows]

    # ------------------------------------------------------------------ reservations
    async def reserve(
        self, conn: AsyncConnection, account_id: UUID, run_id: UUID, amount: Decimal
    ) -> UUID | None:
        """Hold demo credit for a run. None if available credit is insufficient."""
        wallet_id, bal = await self.lock_wallet(conn, account_id)
        if amount <= 0 or bal.available < amount:
            return None
        rid: UUID = (
            await conn.execute(
                text(
                    "INSERT INTO reservations (account_id, wallet_id, run_id, amount)"
                    " VALUES (:a, :w, :r, :m) RETURNING id"
                ),
                {"a": account_id, "w": wallet_id, "r": run_id, "m": amount},
            )
        ).scalar_one()
        await self._reservation_event(conn, account_id, rid, "created", amount)
        await conn.execute(
            text(
                "UPDATE wallets SET reserved = reserved + :m, updated_at = now()"
                " WHERE account_id = :a"
            ),
            {"a": account_id, "m": amount},
        )
        return rid

    async def get_reservation(
        self, conn: AsyncConnection, run_id: UUID, *, lock: bool = False
    ) -> ReservationRecord | None:
        row = (
            await conn.execute(
                text(
                    "SELECT id, account_id, run_id, amount, settled_amount, released_amount,"  # noqa: S608
                    " pending_amount, status, expires_at FROM reservations WHERE run_id = :r"
                    + (" FOR UPDATE" if lock else "")
                ),
                {"r": run_id},
            )
        ).first()
        return ReservationRecord(**row._mapping) if row else None

    async def _reservation_event(
        self,
        conn: AsyncConnection,
        account_id: UUID,
        reservation_id: UUID,
        kind: str,
        amount: Decimal,
        metadata: str = "{}",
    ) -> None:
        await conn.execute(
            text(
                "INSERT INTO reservation_events (account_id, reservation_id, kind, amount,"
                " metadata) VALUES (:a, :r, :k, :m, CAST(:md AS jsonb))"
            ),
            {"a": account_id, "r": reservation_id, "k": kind, "m": amount, "md": metadata},
        )

    async def apply_settlement(
        self,
        conn: AsyncConnection,
        res: ReservationRecord,
        split: SettlementSplit,
        pending_hours: int,
    ) -> None:
        """Resolve an *active* reservation; the caller posts the run_debit journal."""
        status = "pending" if split.pending > 0 else "settled"
        await conn.execute(
            text(
                "UPDATE reservations SET status = :s, settled_amount = :d, released_amount = :rel,"
                " pending_amount = :p, updated_at = now(),"
                " expires_at = CASE WHEN :s = 'pending'"
                "   THEN now() + make_interval(hours => :h) ELSE NULL END"
                " WHERE id = :i AND status = 'active'"
            ),
            {
                "i": res.id,
                "s": status,
                "d": split.debit,
                "rel": split.released,
                "p": split.pending,
                "h": pending_hours,
            },
        )
        await conn.execute(
            text(
                "UPDATE wallets SET reserved = reserved - :amt,"
                " pending_settlement = pending_settlement + :p, updated_at = now()"
                " WHERE account_id = :a"
            ),
            {"a": res.account_id, "amt": res.amount, "p": split.pending},
        )
        if split.debit > 0:
            await self._reservation_event(conn, res.account_id, res.id, "settled", split.debit)
        if split.released > 0:
            await self._reservation_event(conn, res.account_id, res.id, "released", split.released)
        if split.pending > 0:
            await self._reservation_event(conn, res.account_id, res.id, "pending", split.pending)

    async def release_pending(self, conn: AsyncConnection, res: ReservationRecord) -> Decimal:
        """Release a bounded pending hold (24 h policy); the platform absorbs unknown cost."""
        amount = res.pending_amount
        await conn.execute(
            text(
                "UPDATE reservations SET status = 'released', pending_amount = 0,"
                " released_amount = released_amount + :p, expires_at = NULL, updated_at = now()"
                " WHERE id = :i AND status = 'pending'"
            ),
            {"i": res.id, "p": amount},
        )
        await conn.execute(
            text(
                "UPDATE wallets SET pending_settlement = pending_settlement - :p,"
                " updated_at = now() WHERE account_id = :a"
            ),
            {"a": res.account_id, "p": amount},
        )
        await self._reservation_event(conn, res.account_id, res.id, "pending_released", amount)
        return amount

    async def due_pending(self, conn: AsyncConnection, limit: int = 100) -> list[ReservationRecord]:
        rows = await conn.execute(
            text(
                "SELECT id, account_id, run_id, amount, settled_amount, released_amount,"
                " pending_amount, status, expires_at FROM reservations"
                " WHERE status = 'pending' AND expires_at <= now() ORDER BY expires_at LIMIT :n"
            ),
            {"n": limit},
        )
        return [ReservationRecord(**r._mapping) for r in rows]
