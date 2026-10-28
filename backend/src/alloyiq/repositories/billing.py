"""Simulated checkouts, dummy payment events, refunds and sample receipts (D054, D064, D114)."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from alloyiq.domain.payments import PaymentEventDraft

_CHECKOUT_COLS = (
    "id, account_id, user_id, amount, currency, status, return_path, expires_at,"
    " created_at, updated_at, completed_at"
)
_REFUND_COLS = (
    "id, account_id, user_id, checkout_id, amount, currency, reason, status,"
    " ledger_transaction_id, created_at, completed_at"
)


def _row(r: Any) -> dict[str, Any] | None:
    return dict(r._mapping) if r is not None else None


class BillingRepository:
    # ------------------------------------------------------------------ checkouts
    async def create_checkout(
        self,
        conn: AsyncConnection,
        *,
        account_id: UUID,
        user_id: UUID,
        amount: Decimal,
        return_path: str,
        expires_at: datetime,
    ) -> dict[str, Any]:
        row = (
            await conn.execute(
                text(
                    "INSERT INTO checkouts (account_id, user_id, amount, return_path, expires_at)"  # noqa: S608
                    f" VALUES (:a, :u, :m, :p, :e) RETURNING {_CHECKOUT_COLS}"
                ),
                {"a": account_id, "u": user_id, "m": amount, "p": return_path, "e": expires_at},
            )
        ).one()
        return dict(row._mapping)

    async def get_checkout(
        self, conn: AsyncConnection, checkout_id: UUID, *, lock: bool = False
    ) -> dict[str, Any] | None:
        row = (
            await conn.execute(
                text(
                    f"SELECT {_CHECKOUT_COLS} FROM checkouts WHERE id = :i"  # noqa: S608
                    + (" FOR UPDATE" if lock else "")
                ),
                {"i": checkout_id},
            )
        ).first()
        return _row(row)

    async def topups_since(
        self, conn: AsyncConnection, account_id: UUID, since: datetime
    ) -> Decimal:
        """Checkout amounts opened or paid since `since` (failed/expired do not count)."""
        total = (
            await conn.execute(
                text(
                    "SELECT coalesce(sum(amount), 0) FROM checkouts WHERE account_id = :a"
                    " AND created_at >= :s AND status IN ('created','pending','paid')"
                ),
                {"a": account_id, "s": since},
            )
        ).scalar_one()
        return Decimal(total)

    async def set_checkout_status(
        self, conn: AsyncConnection, checkout_id: UUID, status: str, *, final: bool
    ) -> None:
        await conn.execute(
            text(
                "UPDATE checkouts SET status = :s, updated_at = now(),"
                " completed_at = CASE WHEN :f THEN now() ELSE completed_at END WHERE id = :i"
            ),
            {"i": checkout_id, "s": status, "f": final},
        )

    async def expired_open_checkouts(
        self, conn: AsyncConnection, limit: int = 100
    ) -> list[dict[str, Any]]:
        rows = await conn.execute(
            text(
                "SELECT id, account_id FROM checkouts WHERE status IN ('created','pending')"
                " AND expires_at <= now() ORDER BY expires_at LIMIT :n"
            ),
            {"n": limit},
        )
        return [dict(r._mapping) for r in rows]

    # ------------------------------------------------------------------ payment events
    async def insert_event(
        self,
        conn: AsyncConnection,
        account_id: UUID,
        draft: PaymentEventDraft,
        *,
        checkout_id: UUID | None = None,
        refund_id: UUID | None = None,
    ) -> bool:
        """Persist a trusted dummy notification. Duplicate event IDs are ignored (False)."""
        inserted = (
            await conn.execute(
                text(
                    "INSERT INTO payment_events (account_id, event_id, checkout_id, refund_id,"
                    " event_type, sequence, trusted_origin, payload)"
                    " SELECT CAST(:a AS uuid), CAST(:e AS text), CAST(:c AS uuid),"
                    "  CAST(:r AS uuid), CAST(:t AS text), coalesce(max(sequence), 0) + 1,"
                    "  CAST(:o AS text), CAST(:p AS jsonb) FROM payment_events"
                    "  WHERE checkout_id = CAST(:c AS uuid) OR refund_id = CAST(:r AS uuid)"
                    " ON CONFLICT (event_id) DO NOTHING RETURNING id"
                ),
                {
                    "a": account_id,
                    "e": draft.event_id,
                    "c": checkout_id,
                    "r": refund_id,
                    "t": draft.event_type,
                    "o": draft.trusted_origin,
                    "p": json.dumps(draft.payload),
                },
            )
        ).first()
        if inserted:
            await conn.execute(
                text("SELECT pg_notify('payment_events', :e)"), {"e": draft.event_id}
            )
        return inserted is not None

    async def pending_events(self, conn: AsyncConnection, limit: int) -> list[dict[str, Any]]:
        rows = await conn.execute(
            text(
                "SELECT id, account_id FROM payment_events WHERE processing_status = 'pending'"
                " ORDER BY received_at, sequence LIMIT :n"
            ),
            {"n": limit},
        )
        return [dict(r._mapping) for r in rows]

    async def lock_event(self, conn: AsyncConnection, event_row_id: UUID) -> dict[str, Any] | None:
        row = (
            await conn.execute(
                text(
                    "SELECT id, account_id, event_id, checkout_id, refund_id, event_type,"
                    " sequence, received_at, retry_count FROM payment_events"
                    " WHERE id = :i AND processing_status = 'pending' FOR UPDATE SKIP LOCKED"
                ),
                {"i": event_row_id},
            )
        ).first()
        return _row(row)

    async def mark_event(self, conn: AsyncConnection, event_row_id: UUID, status: str) -> None:
        await conn.execute(
            text(
                "UPDATE payment_events SET processing_status = :s, processed_at = now()"
                " WHERE id = :i"
            ),
            {"i": event_row_id, "s": status},
        )

    async def fail_event(self, conn: AsyncConnection, event_row_id: UUID, max_retries: int) -> None:
        await conn.execute(
            text(
                "UPDATE payment_events SET retry_count = retry_count + 1,"
                " processing_status = CASE WHEN retry_count + 1 >= :m THEN 'failed'"
                "   ELSE 'pending' END WHERE id = :i"
            ),
            {"i": event_row_id, "m": max_retries},
        )

    # ------------------------------------------------------------------ refunds
    async def create_refund(
        self,
        conn: AsyncConnection,
        *,
        account_id: UUID,
        user_id: UUID,
        checkout_id: UUID,
        amount: Decimal,
        reason: str,
    ) -> dict[str, Any]:
        row = (
            await conn.execute(
                text(
                    "INSERT INTO refunds (account_id, user_id, checkout_id, amount, reason)"  # noqa: S608
                    f" VALUES (:a, :u, :c, :m, :r) RETURNING {_REFUND_COLS}"
                ),
                {"a": account_id, "u": user_id, "c": checkout_id, "m": amount, "r": reason},
            )
        ).one()
        return dict(row._mapping)

    async def get_refund(
        self, conn: AsyncConnection, refund_id: UUID, *, lock: bool = False
    ) -> dict[str, Any] | None:
        row = (
            await conn.execute(
                text(
                    f"SELECT {_REFUND_COLS} FROM refunds WHERE id = :i"  # noqa: S608
                    + (" FOR UPDATE" if lock else "")
                ),
                {"i": refund_id},
            )
        ).first()
        return _row(row)

    async def refunded_total(self, conn: AsyncConnection, checkout_id: UUID) -> Decimal:
        total = (
            await conn.execute(
                text(
                    "SELECT coalesce(sum(amount), 0) FROM refunds WHERE checkout_id = :c"
                    " AND status IN ('requested','succeeded')"
                ),
                {"c": checkout_id},
            )
        ).scalar_one()
        return Decimal(total)

    async def finish_refund(
        self, conn: AsyncConnection, refund_id: UUID, status: str, txn_id: UUID | None
    ) -> None:
        await conn.execute(
            text(
                "UPDATE refunds SET status = :s, ledger_transaction_id = :t, completed_at = now()"
                " WHERE id = :i AND status = 'requested'"
            ),
            {"i": refund_id, "s": status, "t": txn_id},
        )

    # ------------------------------------------------------------------ receipts
    async def create_receipt(
        self,
        conn: AsyncConnection,
        *,
        account_id: UUID,
        kind: str,
        amount: Decimal,
        txn_id: UUID,
        checkout_id: UUID | None,
        refund_id: UUID | None,
    ) -> None:
        await conn.execute(
            text(
                "INSERT INTO receipts (account_id, checkout_id, refund_id, kind, amount,"
                " ledger_transaction_id) VALUES (:a, :c, :r, :k, :m, :t)"
                " ON CONFLICT (ledger_transaction_id) DO NOTHING"
            ),
            {
                "a": account_id,
                "c": checkout_id,
                "r": refund_id,
                "k": kind,
                "m": amount,
                "t": txn_id,
            },
        )

    async def list_receipts(
        self,
        conn: AsyncConnection,
        account_id: UUID,
        *,
        after: tuple[datetime, UUID] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows = await conn.execute(
            text(
                "SELECT id, kind, amount, currency, checkout_id, refund_id, issued_at"
                " FROM receipts WHERE account_id = :a"
                "  AND (CAST(:cts AS timestamptz) IS NULL OR (issued_at, id) < (:cts, :cid))"
                " ORDER BY issued_at DESC, id DESC LIMIT :n"
            ),
            {
                "a": account_id,
                "cts": after[0] if after else None,
                "cid": after[1] if after else None,
                "n": limit,
            },
        )
        return [dict(r._mapping) for r in rows]
