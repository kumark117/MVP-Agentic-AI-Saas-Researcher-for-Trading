"""Money boundary type: decimal strings only, at most 6 places, never floats (D058, D080)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from alloyiq.api.schemas.common import Money, SimulationFields
from alloyiq.api.schemas.research import StartRunRequest


@pytest.mark.parametrize(
    ("raw", "out"),
    [
        ("25", "25.00"),
        ("25.5", "25.50"),
        ("0.000001", "0.000001"),
        ("1234.567800", "1234.5678"),
    ],
)
def test_money_round_trips_as_decimal_string(raw: str, out: str) -> None:
    m = Money(currency="INR", amount=raw)  # type: ignore[arg-type]
    assert m.model_dump(mode="json") == {"currency": "INR", "amount": out}
    assert m.amount == Decimal(raw)


@pytest.mark.parametrize("bad", [1.5, "1e3", "0.0000001", "abc", "", True])
def test_money_rejects_floats_and_bad_strings(bad: object) -> None:
    with pytest.raises(ValidationError):
        Money(currency="INR", amount=bad)  # type: ignore[arg-type]


def test_requests_reject_unknown_fields_and_non_positive_caps() -> None:
    eid = "6f1c1f0e-3a39-4c63-9a4b-a11091000000"
    with pytest.raises(ValidationError):
        StartRunRequest.model_validate(
            {
                "estimate_id": eid,
                "max_charge": {"currency": "INR", "amount": "1"},
                "account_id": eid,
            }
        )
    with pytest.raises(ValidationError):
        StartRunRequest.model_validate(
            {"estimate_id": eid, "max_charge": {"currency": "INR", "amount": "0"}}
        )


def test_simulation_fields_default_to_zero_collected() -> None:
    assert SimulationFields().model_dump(mode="json") == {
        "payment_mode": "simulated",
        "money_collected": {"currency": "INR", "amount": "0.00"},
    }
