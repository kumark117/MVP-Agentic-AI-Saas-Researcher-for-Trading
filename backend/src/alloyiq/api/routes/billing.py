"""Demo balance, ledger and simulated billing routes (Design section 15; R032, R036-R038).

Handlers translate HTTP into service calls only. No route touches ledger rows directly.
Every response carries payment_mode="simulated" and money_collected = INR 0.00 (D113).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response

from alloyiq.api.deps import Auth, CsrfProtected, Svc
from alloyiq.api.envelope import Envelope, Problem, ok
from alloyiq.api.schemas.billing import (
    Balance,
    Checkout,
    CreditCheckoutRequest,
    LedgerEntry,
    LedgerKind,
    Receipt,
    Refund,
    RefundRequest,
    SimulateAccepted,
    SimulateRequest,
)
from alloyiq.api.schemas.common import Money, Page, PageLimit
from alloyiq.application.billing import BillingService, reject_public_webhook
from alloyiq.application.wallet import WalletService

router = APIRouter(tags=["billing"])

IdempotencyKey = Annotated[
    UUID,
    Header(
        alias="Idempotency-Key",
        description="UUID; the same key and body replay the original response (D083)",
    ),
]


def _wallet(svc: Svc) -> WalletService:
    w: WalletService = svc.extra["wallet"]
    return w


def _billing(svc: Svc) -> BillingService:
    b: BillingService = svc.extra["billing"]
    return b


def _rid(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


@router.get("/account/balance", response_model=Envelope[Balance])
async def balance(request: Request, ctx: Auth, svc: Svc) -> Envelope[Balance]:
    b = await _wallet(svc).balance(ctx.account_id, ctx.user_id)
    return ok(
        request,
        Balance(
            posted_credit=Money.inr(b.posted_credit),
            posted_debit=Money.inr(b.posted_debit),
            reserved=Money.inr(b.reserved),
            pending_settlement=Money.inr(b.pending_settlement),
            available=Money.inr(b.available),
        ),
    )


@router.get("/account/ledger", response_model=Envelope[Page[LedgerEntry]])
async def ledger(
    request: Request,
    ctx: Auth,
    svc: Svc,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    event_type: LedgerKind | None = None,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: PageLimit = 20,
) -> Envelope[Page[LedgerEntry]]:
    page = await _wallet(svc).ledger(
        ctx.account_id, start=from_, end=to, kind=event_type, cursor=cursor, limit=limit
    )
    items = [
        LedgerEntry(
            transaction_id=r["id"],
            business_event_id=r["business_event_id"],
            kind=r["kind"],
            amount=Money.inr(r["amount"]),
            description=r["description"],
            run_id=r["run_id"],
            checkout_id=r["checkout_id"],
            refund_id=r["refund_id"],
            reverses_id=r["reverses_id"],
            posted_at=r["posted_at"],
        )
        for r in page.items
    ]
    return ok(request, Page[LedgerEntry](items=items, next_cursor=page.next_cursor))


@router.post(
    "/billing/credit-checkouts",
    status_code=201,
    response_model=Envelope[Checkout],
    dependencies=[CsrfProtected],
)
async def create_checkout(
    request: Request,
    response: Response,
    body: CreditCheckoutRequest,
    key: IdempotencyKey,
    ctx: Auth,
    svc: Svc,
) -> Envelope[Checkout]:
    status, checkout = await _billing(svc).create_checkout(
        ctx,
        amount=body.amount.amount,
        return_path=body.return_path,
        key=key,
        request_id=_rid(request),
    )
    response.status_code = status
    return ok(request, checkout)


@router.get("/billing/checkouts/{checkout_id}", response_model=Envelope[Checkout])
async def get_checkout(
    request: Request, checkout_id: UUID, ctx: Auth, svc: Svc
) -> Envelope[Checkout]:
    return ok(request, await _billing(svc).get_checkout(ctx, checkout_id))


@router.post(
    "/billing/checkouts/{checkout_id}/simulate",
    status_code=202,
    response_model=Envelope[SimulateAccepted],
    dependencies=[CsrfProtected],
)
async def simulate_checkout(
    request: Request,
    response: Response,
    checkout_id: UUID,
    body: SimulateRequest,
    key: IdempotencyKey,
    ctx: Auth,
    svc: Svc,
) -> Envelope[SimulateAccepted]:
    status, accepted = await _billing(svc).simulate(
        ctx, checkout_id, body.outcome, key=key, request_id=_rid(request)
    )
    response.status_code = status
    return ok(request, accepted)


@router.get("/billing/invoices", response_model=Envelope[Page[Receipt]])
async def invoices(
    request: Request,
    ctx: Auth,
    svc: Svc,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: PageLimit = 20,
) -> Envelope[Page[Receipt]]:
    items, nxt = await _billing(svc).receipts(ctx, cursor=cursor, limit=limit)
    return ok(request, Page[Receipt](items=items, next_cursor=nxt))


@router.post(
    "/billing/refund-requests",
    status_code=202,
    response_model=Envelope[Refund],
    dependencies=[CsrfProtected],
)
async def refund_request(
    request: Request,
    response: Response,
    body: RefundRequest,
    key: IdempotencyKey,
    ctx: Auth,
    svc: Svc,
) -> Envelope[Refund]:
    status, refund = await _billing(svc).request_refund(
        ctx,
        payment_id=body.payment_id,
        amount=body.amount.amount,
        reason=body.reason,
        key=key,
        request_id=_rid(request),
    )
    response.status_code = status
    return ok(request, refund)


@router.post(
    "/webhooks/payments/{provider}",
    status_code=404,
    response_model=None,
    responses={
        404: {
            "model": Problem,
            "description": "Always: public payment webhooks are disabled (D016, D064)",
        }
    },
)
async def payment_webhook(request: Request, provider: str, svc: Svc) -> None:
    raise await reject_public_webhook(svc.db, provider, _rid(request))
