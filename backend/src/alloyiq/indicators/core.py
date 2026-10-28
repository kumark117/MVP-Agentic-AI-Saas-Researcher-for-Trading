"""Deterministic technical indicators over validated bars (R022, D029).

Pure functions. Inputs are oldest-first sequences; each function returns the full series
with None where the window is not yet filled, so callers can read the latest value and
check that enough history existed. The LLM never computes these numbers.

Conventions (documented so results are reproducible):
- SMA: arithmetic mean of the last n closes.
- EMA: alpha = 2/(n+1), seeded with the SMA of the first n values.
- RSI: Wilder's smoothing, seeded with simple averages of the first n changes.
- MACD: EMA12 - EMA26; signal = EMA9 of MACD; histogram = MACD - signal.
- ATR: Wilder's smoothing of true range, seeded with the mean of the first n TRs.
- Bollinger: SMA(n) +/- k * population standard deviation of the same window.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

Series = list[float | None]

# Minimum bars needed for a defined latest value.
MIN_BARS = {
    "SMA20": 20,
    "SMA50": 50,
    "SMA200": 200,
    "RSI14": 15,
    "MACD": 34,
    "ATR14": 15,
    "BB20": 20,
}


def sma(values: Sequence[float], n: int) -> Series:
    if n <= 0:
        raise ValueError("n must be positive")
    out: Series = [None] * len(values)
    window = 0.0
    for i, v in enumerate(values):
        window += v
        if i >= n:
            window -= values[i - n]
        if i >= n - 1:
            out[i] = window / n
    return out


def ema(values: Sequence[float], n: int) -> Series:
    if n <= 0:
        raise ValueError("n must be positive")
    out: Series = [None] * len(values)
    if len(values) < n:
        return out
    alpha = 2.0 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def rsi(closes: Sequence[float], n: int = 14) -> Series:
    out: Series = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / n, losses / n
    out[n] = _rsi_value(avg_g, avg_l)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0.0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0.0)) / n
        out[i] = _rsi_value(avg_g, avg_l)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0  # flat series: neutral by convention
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


@dataclass(frozen=True, slots=True)
class Macd:
    macd: Series
    signal: Series
    histogram: Series


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal_n: int = 9) -> Macd:
    f, s = ema(closes, fast), ema(closes, slow)
    line: Series = [
        a - b if a is not None and b is not None else None for a, b in zip(f, s, strict=True)
    ]
    start = next((i for i, v in enumerate(line) if v is not None), None)
    sig: Series = [None] * len(closes)
    if start is not None:
        defined = [v for v in line[start:] if v is not None]
        for j, v in enumerate(ema(defined, signal_n)):
            sig[start + j] = v
    hist: Series = [
        a - b if a is not None and b is not None else None for a, b in zip(line, sig, strict=True)
    ]
    return Macd(line, sig, hist)


def atr(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], n: int = 14
) -> Series:
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("high, low and close must have equal length")
    out: Series = [None] * len(closes)
    if len(closes) <= n:
        return out
    trs = [
        highs[i] - lows[i]
        if i == 0
        else max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(len(closes))
    ]
    prev = sum(trs[1 : n + 1]) / n
    out[n] = prev
    for i in range(n + 1, len(closes)):
        prev = (prev * (n - 1) + trs[i]) / n
        out[i] = prev
    return out


@dataclass(frozen=True, slots=True)
class Bands:
    middle: Series
    upper: Series
    lower: Series


def bollinger(closes: Sequence[float], n: int = 20, k: float = 2.0) -> Bands:
    mid = sma(closes, n)
    up: Series = [None] * len(closes)
    lo: Series = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        m = mid[i]
        assert m is not None  # noqa: S101 - defined for i >= n-1
        window = closes[i - n + 1 : i + 1]
        sd = math.sqrt(sum((x - m) ** 2 for x in window) / n)
        up[i], lo[i] = m + k * sd, m - k * sd
    return Bands(mid, up, lo)


def last(series: Series) -> float | None:
    return series[-1] if series else None
