"""Report statistics (R044-R048, D106). Pure functions: no I/O.

Percentiles use linear interpolation between closest ranks, the same definition as
PostgreSQL percentile_cont, over raw durations. An empty population returns null; fewer
than LOW_SAMPLE samples is flagged. Daily percentiles are never averaged.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

LOW_SAMPLE = 100


def percentile_cont(values: Sequence[float], fraction: float) -> float | None:
    if not 0 <= fraction <= 1:
        raise ValueError("fraction must be within [0, 1]")
    if not values:
        return None
    s = sorted(values)
    pos = fraction * (len(s) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return float(s[lo])
    return float(s[lo] + (s[hi] - s[lo]) * (pos - lo))


@dataclass(frozen=True, slots=True)
class Distribution:
    sample_count: int
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    population: str
    low_sample: bool


def distribution(values: Sequence[float], population: str) -> Distribution:
    return Distribution(
        sample_count=len(values),
        p50_ms=percentile_cont(values, 0.50),
        p95_ms=percentile_cont(values, 0.95),
        p99_ms=percentile_cont(values, 0.99),
        population=population,
        low_sample=len(values) < LOW_SAMPLE,
    )


@dataclass(frozen=True, slots=True)
class Rate:
    numerator: int
    denominator: int
    value: float | None


def success_rate(succeeded: int, incomplete: int, failed: int, timed_out: int) -> Rate:
    """succeeded / (succeeded + incomplete + failed + timed_out); cancellations and queue
    expiries are reported separately and are not in the denominator."""
    denom = succeeded + incomplete + failed + timed_out
    return Rate(succeeded, denom, None if denom == 0 else succeeded / denom)
