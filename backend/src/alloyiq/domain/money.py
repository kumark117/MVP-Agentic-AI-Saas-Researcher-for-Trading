"""Exact money arithmetic for demo INR and real USD (D058). No I/O, never binary floats.

- Internal precision is micro-units (6 decimal places), matching NUMERIC(20,6).
- Customer postings are rounded to the paisa once per settled run, half-up, and the
  difference is recorded as a rounding adjustment.
- Price ceilings (estimate max charge) round *up* to the next paisa so the ceiling is never
  below the exact worst case.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

MICRO = Decimal("0.000001")
PAISA = Decimal("0.01")
ZERO = Decimal("0")
MTOK = Decimal(1_000_000)


def micro(d: Decimal) -> Decimal:
    """Quantise to micro-units (half-up); the storage precision."""
    return d.quantize(MICRO, rounding=ROUND_HALF_UP)


def round_paise(d: Decimal) -> Decimal:
    """The one settlement rounding: half-up to the paisa."""
    return d.quantize(PAISA, rounding=ROUND_HALF_UP)


def ceil_paise(d: Decimal) -> Decimal:
    """Ceilings (maximum charges): up to the next paisa."""
    return d.quantize(PAISA, rounding=ROUND_CEILING)


def is_paise(d: Decimal) -> bool:
    """True when the amount has at most two decimal places (a valid top-up amount)."""
    return d == d.quantize(PAISA)


def per_mtok(tokens: int, rate_per_mtok: Decimal) -> Decimal:
    """Exact cost of `tokens` at a per-million-token rate, at micro precision."""
    return micro(Decimal(tokens) * rate_per_mtok / MTOK)
