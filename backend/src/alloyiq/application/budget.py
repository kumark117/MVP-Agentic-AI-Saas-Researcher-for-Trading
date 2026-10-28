"""BudgetService: independent REAL provider-spend caps (R042, D060, D115).

Three caps, all from operator configuration and never raised by demo credit, top-ups,
plan changes or retries: per run, per account per UTC day, and platform per UTC day.
They are checked at admission against the worst case, and before every provider call
against the run's remaining reservation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from alloyiq.domain.errors import DomainError, ErrorCode
from alloyiq.domain.money import ZERO
from alloyiq.repositories.audit import audit
from alloyiq.repositories.budget import PLATFORM_KEY, BudgetRepository, SpendReservation


@dataclass(frozen=True, slots=True)
class SpendCaps:
    per_run_usd: Decimal
    account_day_usd: Decimal
    platform_day_usd: Decimal


def seconds_until_next_utc_day(now: datetime) -> int:
    tomorrow = datetime.combine(now.date() + timedelta(days=1), time(0), tzinfo=UTC)
    return max(1, int((tomorrow - now).total_seconds()))


class BudgetService:
    def __init__(
        self,
        caps: SpendCaps,
        repo: BudgetRepository | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.caps = caps
        self.repo = repo or BudgetRepository()
        self.now = clock

    def period(self) -> date:
        return self.now().astimezone(UTC).date()

    def check_per_run(self, worst_case_usd: Decimal) -> None:
        if worst_case_usd > self.caps.per_run_usd:
            raise DomainError(
                422,
                ErrorCode.BUDGET_EXCEEDED,
                "Run exceeds the real-spend limit",
                "The worst-case provider cost of this request exceeds the per-run real-spend "
                "limit. Choose a smaller model profile.",
            )

    def _capacity_error(self) -> DomainError:
        # The platform cap is a capacity condition: 503 with a stable BUDGET_EXCEEDED code.
        return DomainError(
            503,
            ErrorCode.BUDGET_EXCEEDED,
            "Research capacity reached",
            "Today's real provider-spend limit has been reached. Try again after it resets "
            "(00:00 UTC).",
            headers={"Retry-After": str(seconds_until_next_utc_day(self.now()))},
        )

    async def reserve(
        self, conn: AsyncConnection, account_id: UUID, run_id: UUID, amount_usd: Decimal
    ) -> UUID:
        """Hold the worst-case USD in both daily budgets (caller already locked the wallet)."""
        self.check_per_run(amount_usd)
        period = self.period()
        acct = await self.repo.lock(
            conn, "account_day", str(account_id), period, self.caps.account_day_usd
        )
        plat = await self.repo.lock(
            conn, "platform_day", PLATFORM_KEY, period, self.caps.platform_day_usd
        )
        if acct.headroom < amount_usd or plat.headroom < amount_usd:
            await audit(
                conn,
                action="budget.reserve",
                outcome="refused",
                account_id=account_id,
                actor_kind="system",
                metadata={
                    "scope": "account_day" if acct.headroom < amount_usd else "platform_day",
                    "requested_usd": str(amount_usd),
                },
            )
            raise self._capacity_error()
        await self.repo.adjust(conn, acct.id, reserved=amount_usd, spent=ZERO)
        await self.repo.adjust(conn, plat.id, reserved=amount_usd, spent=ZERO)
        return await self.repo.create_reservation(conn, account_id, run_id, period, amount_usd)

    async def remaining(self, conn: AsyncConnection, run_id: UUID) -> Decimal:
        res = await self.repo.get_reservation(conn, run_id)
        return res.remaining if res and res.status == "active" else ZERO

    async def authorize_call(
        self, conn: AsyncConnection, run_id: UUID, worst_call_usd: Decimal
    ) -> bool:
        """Before each provider call: the call's worst case must fit what is left."""
        return worst_call_usd <= await self.remaining(conn, run_id)

    async def record_spend(self, conn: AsyncConnection, run_id: UUID, usd: Decimal) -> None:
        """Measured cost of a completed call (money already spent: always recorded)."""
        res = await self.repo.get_reservation(conn, run_id, lock=True)
        if res is not None and usd > 0:
            await self.repo.add_spend(conn, res.id, usd)

    async def settle(
        self, conn: AsyncConnection, account_id: UUID, run_id: UUID, *, unknown_usage: bool
    ) -> SpendReservation | None:
        """Move the run's hold into spent. Idempotent: only an active reservation settles.

        With unknown usage, the whole reservation counts as spent (a bounded conservative
        allowance) and stays `pending`: an unknown cost never becomes zero (D061).
        """
        res = await self.repo.get_reservation(conn, run_id, lock=True)
        if res is None or res.status != "active":
            return res
        acct = await self.repo.lock(
            conn, "account_day", str(account_id), res.period, self.caps.account_day_usd
        )
        plat = await self.repo.lock(
            conn, "platform_day", PLATFORM_KEY, res.period, self.caps.platform_day_usd
        )
        spent = res.amount_usd if unknown_usage else res.spent_usd
        spent = max(spent, res.spent_usd)
        for row in (acct, plat):
            await self.repo.adjust(conn, row.id, reserved=-res.amount_usd, spent=spent)
        released = max(res.amount_usd - spent, ZERO)
        await self.repo.close_reservation(
            conn,
            res.id,
            status="pending" if unknown_usage else "settled",
            spent=spent,
            released=released,
        )
        return await self.repo.get_reservation(conn, run_id)
