"""BillingService: simulated top-up checkout, dummy payment processing, refunds and sample
receipts (R036-R038; D054, D064, D113-D115).

Payments are simulated. No money is collected, no payment network is called, and no
card, bank or UPI detail is ever requested. The browser can only choose an outcome for
its own checkout; the trusted event is generated on the server and applied by an
asynchronous processor (the worker), which is idempotent and order-tolerant:

- success adds exactly one top-up journal (business_event_id = topup:{checkout_id});
- failure and expiry add nothing;
- duplicate or reordered events for a final checkout have no further effect;
- refunds reverse demo credit only.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from alloyiq.api.schemas.billing import Checkout, Receipt, Refund, SimulateAccepted
from alloyiq.api.schemas.common import Money
from alloyiq.application.budget import seconds_until_next_utc_day
from alloyiq.application.idempotent import once
from alloyiq.application.wallet import WalletService, insufficient_credit
from alloyiq.billing import posting
from alloyiq.domain.errors import DomainError, ErrorCode, not_found, rate_limited
from alloyiq.domain.identity import AccountContext
from alloyiq.domain.money import is_paise
from alloyiq.domain.payments import Outcome, PaymentProvider, apply_checkout_event
from alloyiq.observability.logging import get_logger
from alloyiq.repositories.audit import audit, security_event
from alloyiq.repositories.billing import BillingRepository
from alloyiq.repositories.db import Database, DbContext
from alloyiq.repositories.paging import decode_cursor, encode_cursor
from alloyiq.security.ratelimit import LIMITS, RateLimiter

CHECKOUT_TTL = timedelta(minutes=30)
MAX_EVENT_RETRIES = 5
log = get_logger(__name__)


def _ctx(ctx: AccountContext) -> DbContext:
    return DbContext(account_id=ctx.account_id, user_id=ctx.user_id)


def _field_error(field: str, title: str, detail: str) -> DomainError:
    return DomainError(
        422, ErrorCode.VALIDATION_FAILED, title, detail, field_errors=[(field, detail)]
    )


class BillingService:
    def __init__(
        self,
        db: Database,
        wallet: WalletService,
        provider: PaymentProvider,
        limiter: RateLimiter,
        *,
        checkout_max: Decimal,
        repo: BillingRepository | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.db = db
        self.wallet = wallet
        self.provider = provider
        self.limiter = limiter
        self.checkout_max = checkout_max
        self.repo = repo or BillingRepository()
        self.now = clock

    async def _limit(self, ctx: AccountContext) -> None:
        decision = await self.limiter.hit(
            f"checkout:account:{ctx.account_id}", LIMITS["checkout:account"]
        )
        if not decision.allowed:
            raise rate_limited(decision.retry_after_s)

    async def _checkout_view(self, conn: AsyncConnection, row: dict[str, Any]) -> Checkout:
        applied = await self.wallet.repo.find_transaction(conn, f"topup:{row['id']}")
        return Checkout(
            checkout_id=row["id"],
            status=row["status"],
            amount=Money.inr(row["amount"]),
            hosted_url=self.provider.hosted_url(row["id"]),
            return_path=row["return_path"],
            expires_at=row["expires_at"],
            credit_applied=applied is not None,
            created_at=row["created_at"],
        )

    async def _daily_limit(self, conn: AsyncConnection, account_id: UUID) -> Decimal:
        limit = (
            await conn.execute(
                text(
                    "SELECT p.daily_topup_limit_inr FROM accounts a"
                    " JOIN plans p ON p.id = a.plan_id"
                    " WHERE a.id = :a"
                ),
                {"a": account_id},
            )
        ).scalar()
        return Decimal(limit) if limit is not None else Decimal(0)

    # ------------------------------------------------------------------ checkout
    async def create_checkout(
        self,
        ctx: AccountContext,
        *,
        amount: Decimal,
        return_path: str,
        key: UUID,
        request_id: str | None,
    ) -> tuple[int, Checkout]:
        await self._limit(ctx)
        if not is_paise(amount):
            raise _field_error("amount", "Invalid amount", "Use whole paise (2 decimal places).")
        if amount > self.checkout_max:
            raise _field_error(
                "amount",
                "Amount too large",
                f"A single simulated top-up is limited to INR {self.checkout_max}.",
            )
        body = {"amount": str(amount), "return_path": return_path}
        async with self.db.transaction(_ctx(ctx)) as conn:

            async def create() -> Checkout:
                await self.wallet.repo.lock_wallet(conn, ctx.account_id)  # serialise the limit
                now = self.now()
                day_start = datetime.combine(now.astimezone(UTC).date(), time(0), tzinfo=UTC)
                used = await self.repo.topups_since(conn, ctx.account_id, day_start)
                limit = await self._daily_limit(conn, ctx.account_id)
                if used + amount > limit:
                    raise DomainError(
                        429,
                        ErrorCode.PLAN_LIMIT_REACHED,
                        "Daily top-up limit reached",
                        f"Simulated top-ups are limited to INR {limit} per UTC day "
                        f"(INR {max(limit - used, Decimal(0))} left today).",
                        headers={"Retry-After": str(seconds_until_next_utc_day(now))},
                    )
                row = await self.repo.create_checkout(
                    conn,
                    account_id=ctx.account_id,
                    user_id=ctx.user_id,
                    amount=amount,
                    return_path=return_path,
                    expires_at=now + CHECKOUT_TTL,
                )
                await audit(
                    conn,
                    action="billing.checkout_created",
                    outcome="created",
                    account_id=ctx.account_id,
                    actor_user_id=ctx.user_id,
                    target_type="checkout",
                    target_id=str(row["id"]),
                    request_id=request_id,
                    metadata={"amount": str(amount), "payment_mode": "simulated"},
                )
                return await self._checkout_view(conn, row)

            return await once(
                conn,
                account_id=ctx.account_id,
                method="POST",
                route="/billing/credit-checkouts",
                path="/billing/credit-checkouts",
                key=key,
                request=body,
                model=Checkout,
                status=201,
                action=create,
                resource_id=lambda c: c.checkout_id,
            )

    async def get_checkout(self, ctx: AccountContext, checkout_id: UUID) -> Checkout:
        async with self.db.transaction(_ctx(ctx)) as conn:
            row = await self.repo.get_checkout(conn, checkout_id)
            if row is None:  # RLS: another account's checkout is simply not visible
                raise not_found("Checkout")
            return await self._checkout_view(conn, row)

    async def simulate(
        self,
        ctx: AccountContext,
        checkout_id: UUID,
        outcome: Outcome,
        *,
        key: UUID,
        request_id: str | None,
    ) -> tuple[int, SimulateAccepted]:
        await self._limit(ctx)
        path = f"/billing/checkouts/{checkout_id}/simulate"
        async with self.db.transaction(_ctx(ctx)) as conn:

            async def simulate() -> SimulateAccepted:
                row = await self.repo.get_checkout(conn, checkout_id, lock=True)
                if row is None:
                    raise not_found("Checkout")
                draft = self.provider.checkout_event(checkout_id, outcome)
                await self.repo.insert_event(conn, ctx.account_id, draft, checkout_id=checkout_id)
                await audit(
                    conn,
                    action="billing.checkout_simulated",
                    outcome=outcome,
                    account_id=ctx.account_id,
                    actor_user_id=ctx.user_id,
                    target_type="checkout",
                    target_id=str(checkout_id),
                    request_id=request_id,
                    metadata={"event_id": draft.event_id, "status_at_request": row["status"]},
                )
                return SimulateAccepted(checkout_id=checkout_id, event_id=draft.event_id)

            return await once(
                conn,
                account_id=ctx.account_id,
                method="POST",
                route="/billing/checkouts/{checkout_id}/simulate",
                path=path,
                key=key,
                request={"outcome": outcome},
                model=SimulateAccepted,
                status=202,
                action=simulate,
            )

    # ------------------------------------------------------------------ refunds
    async def request_refund(
        self,
        ctx: AccountContext,
        *,
        payment_id: UUID,
        amount: Decimal,
        reason: str,
        key: UUID,
        request_id: str | None,
    ) -> tuple[int, Refund]:
        await self._limit(ctx)
        if not is_paise(amount):
            raise _field_error("amount", "Invalid amount", "Use whole paise (2 decimal places).")
        body = {"payment_id": str(payment_id), "amount": str(amount), "reason": reason}
        async with self.db.transaction(_ctx(ctx)) as conn:

            async def create() -> Refund:
                await self.wallet.repo.lock_wallet(conn, ctx.account_id)
                checkout = await self.repo.get_checkout(conn, payment_id, lock=True)
                if checkout is None:
                    raise not_found("Payment")
                if checkout["status"] != "paid":
                    raise _field_error(
                        "payment_id",
                        "Not refundable",
                        "Only a paid (simulated) top-up can be refunded.",
                    )
                left = Decimal(checkout["amount"]) - await self.repo.refunded_total(
                    conn, payment_id
                )
                if amount > left:
                    raise _field_error(
                        "amount",
                        "Refund too large",
                        f"At most INR {left} of this top-up can still be refunded.",
                    )
                bal = await self.wallet.repo.computed_balance(conn, ctx.account_id)
                if bal.available < amount:
                    raise insufficient_credit()
                row = await self.repo.create_refund(
                    conn,
                    account_id=ctx.account_id,
                    user_id=ctx.user_id,
                    checkout_id=payment_id,
                    amount=amount,
                    reason=reason,
                )
                await self.repo.insert_event(
                    conn, ctx.account_id, self.provider.refund_event(row["id"]), refund_id=row["id"]
                )
                await audit(
                    conn,
                    action="billing.refund_requested",
                    outcome="requested",
                    account_id=ctx.account_id,
                    actor_user_id=ctx.user_id,
                    target_type="refund",
                    target_id=str(row["id"]),
                    request_id=request_id,
                    metadata={"amount": str(amount), "payment_mode": "simulated"},
                )
                return _refund_view(row)

            return await once(
                conn,
                account_id=ctx.account_id,
                method="POST",
                route="/billing/refund-requests",
                path="/billing/refund-requests",
                key=key,
                request=body,
                model=Refund,
                status=202,
                action=create,
                resource_id=lambda r: r.refund_id,
            )

    # ------------------------------------------------------------------ receipts
    async def receipts(
        self, ctx: AccountContext, *, cursor: str | None, limit: int
    ) -> tuple[list[Receipt], str | None]:
        after = decode_cursor(cursor)
        async with self.db.transaction(_ctx(ctx)) as conn:
            rows = await self.repo.list_receipts(conn, ctx.account_id, after=after, limit=limit + 1)
        more = len(rows) > limit
        rows = rows[:limit]
        items = [
            Receipt(
                receipt_id=r["id"],
                kind=r["kind"],
                amount=Money.inr(r["amount"]),
                checkout_id=r["checkout_id"],
                refund_id=r["refund_id"],
                issued_at=r["issued_at"],
            )
            for r in rows
        ]
        nxt = encode_cursor(rows[-1]["issued_at"], rows[-1]["id"]) if more and rows else None
        return items, nxt

    # ------------------------------------------------------------------ async processor (worker)
    async def process_events(self, batch: int = 50) -> int:
        """Apply pending dummy notifications. Safe to run on many workers at once."""
        async with self.db.transaction(DbContext.for_system()) as conn:
            pending = await self.repo.pending_events(conn, batch)
        done = 0
        for ev in pending:
            try:
                async with self.db.transaction(DbContext(account_id=ev["account_id"])) as conn:
                    locked = await self.repo.lock_event(conn, ev["id"])
                    if locked is None:
                        continue  # processed or claimed by another worker
                    status = await self._apply(conn, locked)
                    await self.repo.mark_event(conn, locked["id"], status)
                    done += 1
            except Exception:
                log.exception("payment_event_failed", payment_event_id=str(ev["id"]))
                async with self.db.transaction(DbContext(account_id=ev["account_id"])) as conn:
                    await self.repo.fail_event(conn, ev["id"], MAX_EVENT_RETRIES)
        return done

    async def _apply(self, conn: AsyncConnection, ev: dict[str, Any]) -> str:
        if ev["event_type"].startswith("refund."):
            return await self._apply_refund(conn, ev)
        account_id: UUID = ev["account_id"]
        await self.wallet.repo.lock_wallet(conn, account_id)
        checkout = await self.repo.get_checkout(conn, ev["checkout_id"], lock=True)
        if checkout is None:  # pragma: no cover - FK guarantees the row
            return "ignored"
        t = apply_checkout_event(
            checkout["status"],
            ev["event_type"],
            received_at=ev["received_at"],
            expires_at=checkout["expires_at"],
        )
        if not t.changed:
            return "ignored"
        await self.repo.set_checkout_status(
            conn, checkout["id"], t.status, final=t.status in ("paid", "failed", "expired")
        )
        if t.credit:
            amount = Decimal(checkout["amount"])
            posted = await self.wallet.post(
                conn,
                account_id,
                posting.topup(account_id, checkout["id"], amount),
                checkout_id=checkout["id"],
            )
            await self.repo.create_receipt(
                conn,
                account_id=account_id,
                kind="topup",
                amount=amount,
                txn_id=posted.transaction_id,
                checkout_id=checkout["id"],
                refund_id=None,
            )
        await audit(
            conn,
            action="billing.checkout_" + t.status,
            outcome=t.reason,
            account_id=account_id,
            actor_kind="system",
            target_type="checkout",
            target_id=str(checkout["id"]),
            metadata={"event_id": ev["event_id"], "payment_mode": "simulated"},
        )
        return "processed"

    async def _apply_refund(self, conn: AsyncConnection, ev: dict[str, Any]) -> str:
        account_id: UUID = ev["account_id"]
        await self.wallet.repo.lock_wallet(conn, account_id)
        refund = await self.repo.get_refund(conn, ev["refund_id"], lock=True)
        if refund is None or refund["status"] != "requested":
            return "ignored"
        amount = Decimal(refund["amount"])
        bal = await self.wallet.repo.computed_balance(conn, account_id)
        if ev["event_type"] == "refund.failed" or bal.available < amount:
            await self.repo.finish_refund(conn, refund["id"], "rejected", None)
            await audit(
                conn,
                action="billing.refund_rejected",
                outcome="insufficient_credit" if bal.available < amount else "failed",
                account_id=account_id,
                actor_kind="system",
                target_type="refund",
                target_id=str(refund["id"]),
            )
            return "processed"
        topup_txn = await self.wallet.repo.find_transaction(conn, f"topup:{refund['checkout_id']}")
        posted = await self.wallet.post(
            conn,
            account_id,
            posting.refund(account_id, refund["id"], amount),
            refund_id=refund["id"],
            checkout_id=refund["checkout_id"],
            reverses_id=topup_txn,
        )
        await self.repo.finish_refund(conn, refund["id"], "succeeded", posted.transaction_id)
        await self.repo.create_receipt(
            conn,
            account_id=account_id,
            kind="refund",
            amount=amount,
            txn_id=posted.transaction_id,
            checkout_id=refund["checkout_id"],
            refund_id=refund["id"],
        )
        return "processed"

    async def expire_checkouts(self) -> int:
        """Scheduler: open checkouts past their 30-minute window get a trusted expiry event."""
        async with self.db.transaction(DbContext.for_system()) as conn:
            due = await self.repo.expired_open_checkouts(conn)
        n = 0
        for c in due:
            async with self.db.transaction(DbContext(account_id=c["account_id"])) as conn:
                if await self.repo.insert_event(
                    conn, c["account_id"], self.provider.expiry_event(c["id"]), checkout_id=c["id"]
                ):
                    n += 1
        return n


async def reject_public_webhook(db: Database, provider: str, request_id: str | None) -> DomainError:
    """POST /webhooks/payments/{provider} is reserved for a future live integration: 404."""
    async with db.transaction(DbContext.anonymous()) as conn:
        await security_event(
            conn,
            kind="billing.public_webhook_rejected",
            severity="low",
            request_id=request_id,
            metadata={"provider": provider[:40]},
        )
    return not_found("Resource")


def _refund_view(row: dict[str, Any]) -> Refund:
    return Refund(
        refund_id=row["id"],
        checkout_id=row["checkout_id"],
        amount=Money.inr(row["amount"]),
        status=row["status"],
        reason=row["reason"],
        created_at=row["created_at"],
    )
