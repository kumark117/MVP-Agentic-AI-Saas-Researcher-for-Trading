"""Yahoo Finance reference adapter (yfinance). DEV-ONLY: unofficial and unlicensed (ADR-006).

Gated by ENABLE_YAHOO_REFERENCE=1. Symbols are continuous front-month futures (GC=F, SI=F,
HG=F), not exact expiries. yfinance blocks, so calls run in a thread with a timeout. Its
own HTTP stack does not go through SafeHttpClient (ADR-013).
"""

from __future__ import annotations

import asyncio
import math
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from alloyiq.domain.market import Bar, Quote
from alloyiq.domain.ports import ProviderError

SOURCE = "yahoo-reference"
SYMBOL_UNITS = {"GC=F": ("USD", "troy_oz"), "SI=F": ("USD", "troy_oz"), "HG=F": ("USD", "lb")}
# Provider-neutral interval -> yfinance interval and maximum lookback it serves.
INTERVALS = {
    "15m": ("15m", timedelta(days=59)),
    "1d": ("1d", timedelta(days=3650)),
    "1wk": ("1wk", timedelta(days=36500)),
    "1mo": ("1mo", timedelta(days=36500)),
}


def _dec(v: Any) -> Decimal | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return Decimal(repr(f))


def frame_to_bars(frame: Any) -> list[Bar]:
    """Convert a yfinance history DataFrame (index = timestamps) into validated bars."""
    bars: list[Bar] = []
    if frame is None or getattr(frame, "empty", True):
        return bars
    for ts, row in frame.iterrows():
        close = _dec(row.get("Close"))
        if close is None or close <= 0:
            continue
        t = ts.to_pydatetime()
        t = t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)
        vol = row.get("Volume")
        bars.append(
            Bar(
                ts=t,
                open=_dec(row.get("Open")),
                high=_dec(row.get("High")),
                low=_dec(row.get("Low")),
                close=close,
                volume=int(vol) if _dec(vol) is not None else None,
            )
        )
    bars.sort(key=lambda b: b.ts)
    return bars


def _import_yfinance() -> Any:
    import yfinance

    return yfinance


class YahooFinanceClient:
    name = SOURCE

    def __init__(self, enabled: bool, timeout_s: float = 3.0, loader: Any = None) -> None:
        # Per attempt, shorter than the 8 s tool deadline so a stalled call can be retried.
        self.enabled = enabled
        self.timeout_s = timeout_s
        # yfinance shares session and cookie state across threads and is not safe to call
        # concurrently; parallel branches would otherwise stall each other (observed live).
        self._lock = asyncio.Lock()
        # `import yfinance` takes ~8 s (pandas, curl_cffi). Done lazily inside the first call
        # it blew that call's timeout, so start it in the background as soon as we exist.
        self._imported = threading.Event()
        self._loader = loader or _import_yfinance
        self._yf: Any = None
        if enabled:
            threading.Thread(target=self._preload, name="yfinance-import", daemon=True).start()

    def _preload(self) -> None:
        try:
            self._yf = self._loader()
        finally:
            self._imported.set()

    async def warm(self, timeout_s: float = 30.0) -> bool:
        """Wait (outside any per-call timeout) until the library is imported."""
        if not self.enabled:
            return False
        return await asyncio.to_thread(self._imported.wait, timeout_s)

    def _check(self, symbol: str) -> None:
        if not self.enabled:
            raise ProviderError(
                "NOT_CONFIGURED", "Yahoo reference data is disabled (ENABLE_YAHOO_REFERENCE)"
            )
        if symbol not in SYMBOL_UNITS:
            raise ProviderError("PROVIDER_ERROR", f"symbol {symbol} is not allowlisted")

    async def _history(self, symbol: str, interval: str, start: datetime) -> list[Bar]:
        self._check(symbol)
        yf_interval, max_back = INTERVALS[interval]
        start = max(start, datetime.now(UTC) - max_back)

        yf = self._yf
        if yf is None and self._imported.is_set():
            raise ProviderError("NOT_CONFIGURED", "yfinance could not be imported")

        def call() -> Any:
            return yf.Ticker(symbol).history(
                start=start,
                interval=yf_interval,
                auto_adjust=False,
                actions=False,
                raise_errors=True,
            )

        if not self._imported.is_set():
            raise ProviderError(
                "PROVIDER_UNAVAILABLE", "Yahoo client is still starting", retryable=True
            )
        try:
            async with self._lock:
                frame = await asyncio.wait_for(asyncio.to_thread(call), timeout=self.timeout_s)
        except TimeoutError as exc:
            raise ProviderError("PROVIDER_UNAVAILABLE", "Yahoo timed out", retryable=True) from exc
        except Exception as exc:
            raise ProviderError(
                "PROVIDER_UNAVAILABLE", "Yahoo request failed", retryable=True
            ) from exc
        return frame_to_bars(frame)

    async def get_history(
        self, symbol: str, *, function: str, interval: str, start: datetime
    ) -> list[Bar]:
        return await self._history(symbol, interval, start)

    async def get_quote(self, symbol: str, *, function: str, currency: str, unit: str) -> Quote:
        if SYMBOL_UNITS.get(symbol) != (currency, unit):
            raise ProviderError("INVALID_RESPONSE", f"{symbol} unit/currency mismatch")
        bars = await self._history(symbol, "15m", datetime.now(UTC) - timedelta(days=5))
        if not bars:
            raise ProviderError("INSUFFICIENT_DATA", f"no recent {symbol} bars")
        last = bars[-1]
        # Bar end time, but never in the future while the latest bar is still forming.
        observed = min(last.ts + timedelta(minutes=15), datetime.now(UTC))
        return Quote(
            price=last.close,
            observed_at=observed,
            currency=currency,
            unit=unit,
            source=SOURCE,
            delay_note="Unofficial delayed reference (continuous front month)",
        )
