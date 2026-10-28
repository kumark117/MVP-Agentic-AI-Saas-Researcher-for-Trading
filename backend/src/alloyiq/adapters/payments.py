"""DummyPaymentProvider: the only enabled payment adapter (D054, D064, D114).

It never makes a network call and never sees card, bank or UPI details. It generates
trusted server-side notifications for the local demo checkout; the browser can only pick
an outcome for its own checkout, never post an event or an amount.
"""

from __future__ import annotations

import uuid
from uuid import UUID

from alloyiq.domain.payments import Outcome, PaymentEventDraft, outcome_event


class DummyPaymentProvider:
    name = "dummy"

    def hosted_url(self, checkout_id: UUID) -> str:
        return f"/account/checkout/{checkout_id}"  # same-origin local demo page

    def checkout_event(self, checkout_id: UUID, outcome: Outcome) -> PaymentEventDraft:
        return PaymentEventDraft(
            event_id=f"dummy_evt_{uuid.uuid4().hex}",
            event_type=outcome_event(outcome),
            trusted_origin="dummy:simulate",
            payload={"checkout_id": str(checkout_id), "outcome": outcome, "simulated": True},
        )

    def expiry_event(self, checkout_id: UUID) -> PaymentEventDraft:
        # Deterministic ID: the scheduler can run repeatedly without duplicating it.
        return PaymentEventDraft(
            event_id=f"dummy_expire_{checkout_id.hex}",
            event_type="checkout.expired",
            trusted_origin="dummy:scheduler",
            payload={"checkout_id": str(checkout_id), "simulated": True},
        )

    def refund_event(self, refund_id: UUID) -> PaymentEventDraft:
        return PaymentEventDraft(
            event_id=f"dummy_refund_{refund_id.hex}",
            event_type="refund.succeeded",
            trusted_origin="dummy:refund",
            payload={"refund_id": str(refund_id), "simulated": True},
        )
