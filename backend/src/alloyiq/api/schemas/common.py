"""Shared boundary primitives (D080, D104).

- Request models forbid unknown fields (422); responses may grow optional fields.
- Money is {currency, amount} with amount a decimal string of at most 6 fractional digits.
- Timestamps are RFC 3339 UTC; durations are integer milliseconds; null means unknown.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
)

AMOUNT_PATTERN = r"^-?\d{1,14}(\.\d{1,6})?$"


def _parse_amount(v: object) -> Decimal:
    if isinstance(v, Decimal):
        d = v
    elif isinstance(v, str):
        import re

        if not re.fullmatch(AMOUNT_PATTERN, v):
            raise ValueError("amount must be a decimal string with at most 6 fractional digits")
        try:
            d = Decimal(v)
        except InvalidOperation as exc:  # pragma: no cover - pattern already guards
            raise ValueError("invalid decimal") from exc
    elif isinstance(v, int) and not isinstance(v, bool):
        d = Decimal(v)
    else:
        # floats are rejected: money is never binary floating point (D058)
        raise ValueError("amount must be a decimal string")
    if d.as_tuple().exponent < -6:  # type: ignore[operator]
        raise ValueError("at most 6 fractional digits")
    return d


def _format_amount(d: Decimal) -> str:
    # Plain notation, at least 2 decimals (paise), no exponent, at most 6 decimals.
    q = d.quantize(Decimal("0.000001")).normalize()
    s = format(q, "f")
    if "." not in s:
        s += ".00"
    elif len(s.split(".")[1]) == 1:
        s += "0"
    return s


Amount = Annotated[
    Decimal,
    BeforeValidator(_parse_amount),
    PlainSerializer(_format_amount, return_type=str),
    WithJsonSchema({"type": "string", "pattern": AMOUNT_PATTERN, "examples": ["25.00"]}),
]

Currency = Literal["INR", "USD"]


class Strict(BaseModel):
    """Base for request bodies: unknown fields are rejected with 422."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Out(BaseModel):
    """Base for response bodies."""

    model_config = ConfigDict(frozen=True, from_attributes=True)


class Money(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    currency: Currency
    amount: Amount

    @classmethod
    def inr(cls, amount: Decimal | str | int) -> Money:
        return cls(currency="INR", amount=amount)  # type: ignore[arg-type]

    @classmethod
    def usd(cls, amount: Decimal | str | int) -> Money:
        return cls(currency="USD", amount=amount)  # type: ignore[arg-type]


def _positive(m: Money) -> Money:
    if m.amount <= 0:
        raise ValueError("amount must be positive")
    return m


PositiveMoney = Annotated[Money, AfterValidator(_positive)]

ZERO_INR = Money.inr("0.00")


class SimulationFields(Out):
    """D113: every billing-relevant response declares simulation and zero collection."""

    payment_mode: Literal["simulated"] = "simulated"
    money_collected: Money = Field(default=ZERO_INR)


class Page[T](Out):
    items: list[T]
    next_cursor: str | None = None


PageLimit = Annotated[int, Field(ge=1, le=100)]


def utcnow() -> datetime:
    return datetime.now(UTC)


# Stable enums shared with the database CHECK constraints (repositories/schema.py).
RunStatus = Literal[
    "queued",
    "running",
    "cancel_requested",
    "succeeded",
    "incomplete",
    "failed",
    "cancelled",
    "timed_out",
]
BillingStatus = Literal["reserved", "pending_settlement", "settled", "released", "disputed"]
Horizon = Literal["intraday", "swing", "long_term"]
Signal = Literal["BUY", "SELL", "HOLD", "NO_SIGNAL"]
UsageStatus = Literal["actual", "unknown", "estimated"]
CostStatus = Literal["calculated", "reconciled", "unknown"]
