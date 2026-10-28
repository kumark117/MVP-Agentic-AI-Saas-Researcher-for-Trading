"""Real provider-spend budgets in USD (D060, D115). These are REAL money guards.

Rows exist per (scope, scope_key, UTC day). They are locked account_day first, then
platform_day, always after the wallet (fixed order, so concurrent admissions cannot
deadlock). The limit comes from operator configuration only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

Scope = Literal["account_day", "platform_day"]
PLATFORM_KEY = "platform"


@dataclass(frozen=True, slots=True)
class BudgetRow:
    id: UUID
    scope: Scope
    scope_key: str
    period: date
    limit_usd: Decimal
    spent_usd: Decimal
    reserved_usd: Decimal

    @property
    def headroom(self) -> Decimal:
        return self.limit_usd - self.spent_usd - self.reserved_usd


@dataclass(frozen=True, slots=True)
class SpendReservation:
    id: UUID
    account_id: UUID
    run_id: UUID
    period: date
    amount_usd: Decimal
    spent_usd: Decimal
    released_usd: Decimal
    status: str

    @property
    def remaining(self) -> Decimal:
        return self.amount_usd - self.spent_usd


_COLS = "id, scope, scope_key, period, limit_usd, spent_usd, reserved_usd"
_RES_COLS = "id, account_id, run_id, period, amount_usd, spent_usd, released_usd, status"


class BudgetRepository:
    async def lock(
        self, conn: AsyncConnection, scope: Scope, key: str, period: date, limit_usd: Decimal
    ) -> BudgetRow:
        """Create-or-lock the budget row and apply the operator's current limit."""
        row = (
            await conn.execute(
                text(
                    "INSERT INTO real_budget_accounts (scope, scope_key, period, limit_usd)"  # noqa: S608
                    " VALUES (:s, :k, :p, :l) ON CONFLICT (scope, scope_key, period)"
                    " DO UPDATE SET limit_usd = EXCLUDED.limit_usd, updated_at = now()"
                    f" RETURNING {_COLS}"
                ),
                {"s": scope, "k": key, "p": period, "l": limit_usd},
            )
        ).one()
        return BudgetRow(**row._mapping)

    async def get(
        self, conn: AsyncConnection, scope: Scope, key: str, period: date
    ) -> BudgetRow | None:
        row = (
            await conn.execute(
                text(
                    f"SELECT {_COLS} FROM real_budget_accounts"  # noqa: S608
                    " WHERE scope = :s AND scope_key = :k AND period = :p"
                ),
                {"s": scope, "k": key, "p": period},
            )
        ).first()
        return BudgetRow(**row._mapping) if row else None

    async def adjust(
        self, conn: AsyncConnection, row_id: UUID, *, reserved: Decimal, spent: Decimal
    ) -> None:
        await conn.execute(
            text(
                "UPDATE real_budget_accounts SET reserved_usd = greatest(reserved_usd + :r, 0),"
                " spent_usd = spent_usd + :s, updated_at = now() WHERE id = :i"
            ),
            {"i": row_id, "r": reserved, "s": spent},
        )

    async def create_reservation(
        self, conn: AsyncConnection, account_id: UUID, run_id: UUID, period: date, amount: Decimal
    ) -> UUID:
        rid: UUID = (
            await conn.execute(
                text(
                    "INSERT INTO real_spend_reservations (account_id, run_id, period, amount_usd)"
                    " VALUES (:a, :r, :p, :m) RETURNING id"
                ),
                {"a": account_id, "r": run_id, "p": period, "m": amount},
            )
        ).scalar_one()
        return rid

    async def get_reservation(
        self, conn: AsyncConnection, run_id: UUID, *, lock: bool = False
    ) -> SpendReservation | None:
        row = (
            await conn.execute(
                text(
                    f"SELECT {_RES_COLS} FROM real_spend_reservations WHERE run_id = :r"  # noqa: S608
                    + (" FOR UPDATE" if lock else "")
                ),
                {"r": run_id},
            )
        ).first()
        return SpendReservation(**row._mapping) if row else None

    async def add_spend(self, conn: AsyncConnection, reservation_id: UUID, usd: Decimal) -> None:
        await conn.execute(
            text(
                "UPDATE real_spend_reservations SET spent_usd = spent_usd + :u, updated_at = now()"
                " WHERE id = :i"
            ),
            {"i": reservation_id, "u": usd},
        )

    async def close_reservation(
        self,
        conn: AsyncConnection,
        reservation_id: UUID,
        *,
        status: str,
        spent: Decimal,
        released: Decimal,
    ) -> None:
        await conn.execute(
            text(
                "UPDATE real_spend_reservations SET status = :s, spent_usd = :sp,"
                " released_usd = :rel, updated_at = now() WHERE id = :i"
            ),
            {"i": reservation_id, "s": status, "sp": spent, "rel": released},
        )
