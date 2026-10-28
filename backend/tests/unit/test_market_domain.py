"""Trading calendar, freshness and unavailable-combination rules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from alloyiq.domain.catalog import unavailable_combinations
from alloyiq.domain.market import freshness, history_plan, market_state

NY_OFFSET = timedelta(hours=4)  # EDT in late October 2026 (DST ends 1 Nov)


def ny(y: int, m: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=UTC) + NY_OFFSET


@pytest.mark.parametrize(
    ("at", "state"),
    [
        (ny(2026, 10, 28, 10), "open"),  # Wednesday morning
        (ny(2026, 10, 28, 17, 30), "closed"),  # daily maintenance break
        (ny(2026, 10, 30, 17, 1), "closed"),  # Friday after the close
        (ny(2026, 10, 31, 12), "closed"),  # Saturday
        (ny(2026, 11, 1, 17), "closed"),  # Sunday before reopen (EST from 1 Nov, still closed)
        (ny(2026, 10, 25, 19), "open"),  # Sunday evening reopen
    ],
)
def test_globex_metals_calendar(at: datetime, state: str) -> None:
    assert market_state("CME_GLOBEX_METALS", at) == state


def test_monthly_series_has_no_session() -> None:
    assert market_state("MONTHLY", datetime.now(UTC)) == "not_applicable"


def test_freshness_states() -> None:
    now = datetime(2026, 10, 28, 12, tzinfo=UTC)
    assert freshness(now - timedelta(minutes=10), now, 3600, "open") == "fresh"
    assert freshness(now - timedelta(minutes=45), now, 3600, "open") == "delayed"
    assert freshness(now - timedelta(hours=3), now, 3600, "open") == "stale"
    assert freshness(now - timedelta(hours=3), now, 3600, "closed") == "market_closed"
    assert freshness(now, now, None, "open") == "unknown"


def test_reference_series_always_monthly() -> None:
    assert history_plan("long_term", "reference_series").interval == "1mo"
    assert history_plan("swing", "futures_continuous").interval == "1d"
    assert "SMA200" in history_plan("swing", "futures_continuous").indicators


def test_unavailable_combinations_never_assume_coverage() -> None:
    combos = unavailable_combinations({("gold", "COMEX")})
    pairs = {(c.metal, c.venue): c.reason for c in combos}
    assert ("gold", "COMEX") not in pairs
    assert pairs[("gold", "MCX")] == "No licensed MCX feed configured"
    assert pairs[("copper", "LME")] == "No licensed LME feed configured"
    assert pairs[("uranium", "COMEX")] == "No verified data source"
    assert len(combos) == 14  # 5 metals x 3 venues, minus the one available
