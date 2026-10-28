"""Percentiles, success rate and the CSV neutraliser (R035, R044-R048)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from alloyiq.security.csv_safe import neutralise, to_csv
from alloyiq.stats.core import distribution, percentile_cont, success_rate


def test_percentiles_match_known_samples() -> None:
    xs = [100.0, 200.0, 300.0, 400.0, 500.0]
    assert percentile_cont(xs, 0.5) == 300.0
    assert percentile_cont(xs, 0.95) == pytest.approx(480.0)  # 400 + 0.8 * 100
    assert percentile_cont(xs, 0.0) == 100.0 and percentile_cont(xs, 1.0) == 500.0
    assert percentile_cont([7.0], 0.99) == 7.0


def test_empty_population_is_null_and_small_samples_warn() -> None:
    d = distribution([], "none")
    assert d.sample_count == 0 and d.p50_ms is None and d.p99_ms is None and d.low_sample
    assert distribution([1.0] * 99, "x").low_sample
    assert not distribution([1.0] * 100, "x").low_sample


@given(st.lists(st.floats(min_value=0, max_value=1e7, allow_nan=False), min_size=1, max_size=200))
def test_percentiles_are_ordered_and_bounded(xs: list[float]) -> None:
    d = distribution(xs, "p")
    assert d.p50_ms is not None and d.p95_ms is not None and d.p99_ms is not None
    assert min(xs) <= d.p50_ms <= d.p95_ms <= d.p99_ms <= max(xs)


def test_success_rate_excludes_cancellations_and_queue_expiry() -> None:
    r = success_rate(succeeded=3, incomplete=1, failed=0, timed_out=0)
    assert (r.numerator, r.denominator, r.value) == (3, 4, 0.75)
    assert success_rate(0, 0, 0, 0).value is None


@pytest.mark.parametrize("cell", ["=1+1", "+cmd", "-2+3", "@SUM(A1)", "\tx", "\rx"])
def test_formula_prefixes_are_neutralised(cell: str) -> None:
    assert neutralise(cell) == "'" + cell


def test_csv_output_keeps_trusted_numbers_and_quotes_text() -> None:
    data = to_csv(["a", "b", "c"], [['=HYPERLINK("http://x")', Decimal("-3.46"), None]]).decode(
        "utf-8-sig"
    )
    lines = data.splitlines()
    assert lines[0] == "a,b,c"
    assert lines[1].startswith("\"'=HYPERLINK(") and lines[1].endswith(",-3.46,")
