"""Simulated payment lifecycle (D054, D064, D114). Pure: no I/O.

Checkout states: created -> pending | paid | failed | expired; pending -> paid | failed |
expired. paid, failed and expired are final, so duplicate or reordered notifications for
a final checkout have no further effect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

CheckoutStatus = Literal["created", "pending", "paid", "failed", "expired"]
Outcome = Literal["success", "failure", "pending"]
PaymentEventType = Literal[
    "checkout.pending",
    "checkout.paid",
    "checkout.failed",
    "checkout.expired",
    "refund.succeeded",
    "refund.failed",
]
FINAL: frozenset[str] = frozenset({"paid", "failed", "expired"})

_TARGET: dict[str, CheckoutStatus] = {
    "checkout.pending": "pending",
    "checkout.paid": "paid",
    "checkout.failed": "failed",
    "checkout.expired": "expired",
}


@dataclass(frozen=True, slots=True)
class PaymentEventDraft:
    event_id: str
    event_type: PaymentEventType
    trusted_origin: str
    payload: dict[str, Any] = field(default_factory=dict)


def outcome_event(outcome: Outcome) -> PaymentEventType:
    return {
        "success": "checkout.paid",
        "failure": "checkout.failed",
        "pending": "checkout.pending",
    }[outcome]  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class Transition:
    status: CheckoutStatus
    changed: bool
    credit: bool  # post exactly one top-up journal
    reason: str


def apply_checkout_event(
    current: CheckoutStatus,
    event_type: str,
    *,
    received_at: datetime,
    expires_at: datetime,
) -> Transition:
    """Decide the effect of one notification on a checkout. Deterministic and total."""
    target = _TARGET.get(event_type)
    if target is None:
        return Transition(current, False, False, "not a checkout event")
    if current in FINAL:
        return Transition(current, False, False, f"checkout already {current}")
    # A success or pending notice that arrives after the checkout window expires it instead.
    if target in ("paid", "pending") and received_at > expires_at:
        return Transition("expired", True, False, "received after expiry")
    if target == current:
        return Transition(current, False, False, "no change")
    return Transition(target, True, target == "paid", "applied")


class PaymentProvider(Protocol):
    """Extension point (D054). Only DummyPaymentProvider is enabled in this release."""

    name: str

    def hosted_url(self, checkout_id: UUID) -> str: ...

    def checkout_event(self, checkout_id: UUID, outcome: Outcome) -> PaymentEventDraft: ...

    def expiry_event(self, checkout_id: UUID) -> PaymentEventDraft: ...

    def refund_event(self, refund_id: UUID) -> PaymentEventDraft: ...
