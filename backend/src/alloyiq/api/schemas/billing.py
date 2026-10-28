"""Demo billing boundary models (Design sections 15, 20; R032, R036-R038, D113-D115)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from alloyiq.api.schemas.common import Money, PositiveMoney, SimulationFields, Strict

CheckoutStatus = Literal["created", "pending", "paid", "failed", "expired"]
LedgerKind = Literal["topup", "run_debit", "refund", "adjustment", "reversal", "opening_grant"]


class Balance(SimulationFields):
    currency: Literal["INR"] = "INR"
    posted_credit: Money
    posted_debit: Money
    reserved: Money
    pending_settlement: Money
    available: Money
    label: str = "Simulated demo credit: no cash value"


class LedgerEntry(SimulationFields):
    transaction_id: UUID
    business_event_id: str
    kind: LedgerKind
    amount: Money  # signed movement on the member's wallet
    description: str
    run_id: UUID | None
    checkout_id: UUID | None
    refund_id: UUID | None
    reverses_id: UUID | None
    posted_at: datetime


class CreditCheckoutRequest(Strict):
    amount: PositiveMoney
    return_path: str = Field(pattern=r"^/account(/[a-z0-9/_-]*)?$", max_length=200)


class Checkout(SimulationFields):
    checkout_id: UUID
    status: CheckoutStatus
    amount: Money
    hosted_url: str
    return_path: str
    expires_at: datetime
    credit_applied: bool
    created_at: datetime


class SimulateRequest(Strict):
    outcome: Literal["success", "failure", "pending"]


class SimulateAccepted(SimulationFields):
    checkout_id: UUID
    event_id: str
    accepted: Literal[True] = True


class Receipt(SimulationFields):
    receipt_id: UUID
    kind: Literal["topup", "refund"]
    amount: Money
    sample: Literal[True] = True
    label: str = "Simulation - not a tax invoice"
    checkout_id: UUID | None
    refund_id: UUID | None
    issued_at: datetime


class RefundRequest(Strict):
    payment_id: UUID  # the checkout being refunded
    amount: PositiveMoney
    reason: str = Field(min_length=1, max_length=500)


class Refund(SimulationFields):
    refund_id: UUID
    checkout_id: UUID
    amount: Money
    status: Literal["requested", "succeeded", "rejected"]
    reason: str
    created_at: datetime
