"""Market data value objects, horizon data plans, freshness and trading calendars. Pure.

All instants are timezone-aware UTC. Prices are Decimal; indicator maths uses float
internally but every observation keeps its exact source value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

Horizon = Literal["intraday", "swing", "long_term"]
FreshnessState = Literal["fresh", "delayed", "stale", "market_closed", "unknown"]
MarketState = Literal["open", "closed", "not_applicable", "unknown"]


@dataclass(frozen=True, slots=True)
class Bar:
    ts: datetime  # bar open time, UTC
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal
    volume: int | None = None


@dataclass(frozen=True, slots=True)
class Quote:
    price: Decimal
    observed_at: datetime
    currency: str
    unit: str
    source: str
    delay_note: str


@dataclass(frozen=True, slots=True)
class FxRate:
    base: str
    quote: str
    rate: Decimal
    observed_at: datetime
    source: str


@dataclass(frozen=True, slots=True)
class NewsArticle:
    title: str
    url: str
    published_at: datetime | None
    excerpt: str
    source_domain: str
    score: float | None


@dataclass(frozen=True, slots=True)
class HistoryPlan:
    interval: str  # provider-neutral: 15m, 1d, 1wk, 1mo
    lookback: timedelta
    indicators: tuple[str, ...]


INDICATORS_INTRADAY = ("SMA20", "SMA50", "RSI14", "MACD", "ATR14", "BB20")
INDICATORS_SWING = (*INDICATORS_INTRADAY, "SMA200")
INDICATORS_LONG = ("SMA20", "SMA50", "RSI14", "MACD")

HISTORY_PLANS: dict[Horizon, HistoryPlan] = {
    "intraday": HistoryPlan("15m", timedelta(days=5), INDICATORS_INTRADAY),
    "swing": HistoryPlan("1d", timedelta(days=370), INDICATORS_SWING),
    "long_term": HistoryPlan("1wk", timedelta(days=5 * 366), INDICATORS_LONG),
}
# Reference series are monthly regardless of horizon (only long_term is offered).
MONTHLY_PLAN = HistoryPlan("1mo", timedelta(days=12 * 366), INDICATORS_LONG)

NEWS_RECENCY_DAYS: dict[Horizon, int] = {"intraday": 2, "swing": 7, "long_term": 30}


def history_plan(horizon: Horizon, instrument_type: str) -> HistoryPlan:
    return MONTHLY_PLAN if instrument_type == "reference_series" else HISTORY_PLANS[horizon]


# ------------------------------------------------------------------ trading calendars
_NY = ZoneInfo("America/New_York")


def market_state(calendar: str, at: datetime) -> MarketState:
    """Coarse session state. Holidays are not modelled; the report says so (limitation)."""
    if calendar == "MONTHLY":
        return "not_applicable"
    if calendar == "CME_GLOBEX_METALS":
        # Sunday 18:00 to Friday 17:00 New York time, with a daily 17:00-18:00 break.
        local = at.astimezone(_NY)
        wd, t = local.weekday(), local.time()  # Monday = 0
        if wd == 5:
            return "closed"
        if wd == 6:
            return "open" if t >= time(18) else "closed"
        if wd == 4 and t >= time(17):
            return "closed"
        return "closed" if time(17) <= t < time(18) else "open"
    return "unknown"


def freshness(
    observed_at: datetime, now: datetime, max_age_s: int | None, state: MarketState
) -> FreshnessState:
    if max_age_s is None:
        return "unknown"
    age = (now - observed_at).total_seconds()
    if age <= max_age_s:
        return "fresh" if age <= 30 * 60 else "delayed"
    return "market_closed" if state == "closed" else "stale"


def utcnow() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------ plausibility bounds
# Wide sanity ranges per (currency, unit); a value outside is a data error, not a signal.
PRICE_BOUNDS: dict[tuple[str, str], tuple[Decimal, Decimal]] = {
    ("USD", "troy_oz"): (Decimal("1"), Decimal("50000")),
    ("USD", "lb"): (Decimal("0.1"), Decimal("100")),
    ("USD", "metric_ton"): (Decimal("100"), Decimal("200000")),
}
FX_BOUNDS: dict[tuple[str, str], tuple[Decimal, Decimal]] = {
    ("USD", "INR"): (Decimal("30"), Decimal("300")),
}
