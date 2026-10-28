"""Indicator correctness against hand-computed reference values and properties."""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from alloyiq.indicators.core import atr, bollinger, ema, last, macd, rsi, sma

LINE = [float(x) for x in range(1, 41)]  # 1..40


def test_sma_reference() -> None:
    s = sma([1, 2, 3, 4, 5], 3)
    assert s == [None, None, 2.0, 3.0, 4.0]


def test_ema_reference() -> None:
    # alpha = 0.5 for n=3; seed = mean(1,2,3)=2; then 0.5*4+0.5*2=3; 0.5*5+0.5*3=4
    assert ema([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]
    assert ema([2, 4, 6, 8], 3)[3] == pytest.approx(0.5 * 8 + 0.5 * 4)


def test_rsi_extremes_and_reference() -> None:
    assert last(rsi(LINE, 14)) == 100.0  # only gains
    assert last(rsi(list(reversed(LINE)), 14)) == 0.0  # only losses
    assert last(rsi([5.0] * 20, 14)) == 50.0  # flat: neutral convention
    # Alternating +1/-1 from 10: equal average gain and loss -> 50.
    alt = [10.0 + (i % 2) for i in range(30)]
    assert last(rsi(alt, 14)) == pytest.approx(50.0, abs=4.0)
    # Hand-computed Wilder seed: changes +1,+1,-1 for n=3 -> avg_g=2/3, avg_l=1/3 -> RS=2
    assert rsi([1, 2, 3, 2], 3)[3] == pytest.approx(100 - 100 / 3)


def test_macd_on_linear_series_converges_to_constant() -> None:
    m = macd(LINE)
    # For a linear series the EMA lag is constant: EMA_n lags by (n-1)/2 * slope.
    assert last(m.macd) == pytest.approx((26 - 1) / 2 - (12 - 1) / 2, rel=1e-6)
    assert last(m.signal) == pytest.approx(last(m.macd), rel=1e-6)
    assert last(m.histogram) == pytest.approx(0.0, abs=1e-9)
    assert m.macd[24] is None and m.macd[25] is not None
    assert m.signal[32] is None and m.signal[33] is not None  # needs 34 bars


def test_atr_constant_range() -> None:
    highs = [11.0] * 20
    lows = [9.0] * 20
    closes = [10.0] * 20
    assert last(atr(highs, lows, closes, 14)) == pytest.approx(2.0)


def test_bollinger_flat_series_has_zero_width() -> None:
    b = bollinger([3.0] * 25, 20, 2)
    assert last(b.upper) == last(b.lower) == last(b.middle) == 3.0


def test_bollinger_reference() -> None:
    b = bollinger([1, 2, 3, 4], 4, 2)
    sd = math.sqrt(((1 - 2.5) ** 2 + (2 - 2.5) ** 2 + (3 - 2.5) ** 2 + (4 - 2.5) ** 2) / 4)
    assert last(b.upper) == pytest.approx(2.5 + 2 * sd)


def test_insufficient_history_yields_none() -> None:
    assert last(sma([1.0] * 19, 20)) is None
    assert last(rsi([1.0] * 14, 14)) is None
    assert last(macd([1.0] * 33).signal) is None


@given(st.lists(st.floats(min_value=1, max_value=1e5, allow_nan=False), min_size=30, max_size=200))
def test_properties(closes: list[float]) -> None:
    r = last(rsi(closes))
    assert r is not None and 0.0 <= r <= 100.0
    s = last(sma(closes, 20))
    window = closes[-20:]
    assert s is not None and min(window) - 1e-6 <= s <= max(window) + 1e-6
    b = bollinger(closes)
    assert last(b.lower) <= last(b.middle) <= last(b.upper)  # type: ignore[operator]
