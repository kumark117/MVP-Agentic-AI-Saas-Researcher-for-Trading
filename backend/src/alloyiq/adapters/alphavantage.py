"""Alpha Vantage adapter: FX (CURRENCY_EXCHANGE_RATE) and commodity reference series.

Alpha Vantage answers HTTP 200 with a "Note", "Information" or "Error Message" body when it
throttles or rejects a call; those become typed ProviderErrors, never data (build prompt
section 11). The API key is server-held and never logged.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from alloyiq.domain.market import Bar, FxRate, Quote
from alloyiq.domain.ports import ProviderError
from alloyiq.security.egress import EgressBlocked, SafeHttpClient

BASE = "https://www.alphavantage.co/query"
SOURCE = "alphavantage"

# Commodity functions we may use, with their declared unit (verified by `catalog verify`).
COMMODITY_UNITS = {"COPPER": ("USD", "metric_ton"), "ALUMINUM": ("USD", "metric_ton")}


def _dec(v: object) -> Decimal | None:
    if v in (None, "", ".", "None"):
        return None
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


class AlphaVantageClient:
    name = SOURCE

    def __init__(self, http: SafeHttpClient, api_key: str | None) -> None:
        self.http = http
        self._key = api_key

    async def _get(self, params: dict[str, str]) -> dict[str, Any]:
        if not self._key:
            raise ProviderError("NOT_CONFIGURED", "ALPHAVANTAGE_API_KEY is not set")
        try:
            resp = await self.http.request("GET", BASE, params={**params, "apikey": self._key})
        except EgressBlocked:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "PROVIDER_UNAVAILABLE", "Alpha Vantage timed out", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                "PROVIDER_UNAVAILABLE", "Alpha Vantage unreachable", retryable=True
            ) from exc
        if resp.status_code >= 500:
            raise ProviderError(
                "PROVIDER_UNAVAILABLE", f"Alpha Vantage HTTP {resp.status_code}", retryable=True
            )
        if resp.status_code != 200:
            raise ProviderError("PROVIDER_ERROR", f"Alpha Vantage HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise ProviderError("INVALID_RESPONSE", "Alpha Vantage returned non-JSON") from exc
        if not isinstance(body, dict):
            raise ProviderError("INVALID_RESPONSE", "Alpha Vantage returned an unexpected shape")
        return parse_errors(body)

    async def get_rate(self, base: str, quote: str) -> FxRate:
        body = await self._get(
            {"function": "CURRENCY_EXCHANGE_RATE", "from_currency": base, "to_currency": quote}
        )
        return parse_fx(body, base, quote)

    async def get_history(
        self, symbol: str, *, function: str, interval: str, start: datetime
    ) -> list[Bar]:
        if symbol not in COMMODITY_UNITS:
            raise ProviderError("PROVIDER_ERROR", f"unsupported Alpha Vantage function {symbol}")
        body = await self._get({"function": symbol, "interval": function or "monthly"})
        return [b for b in parse_commodity(body) if b.ts >= start]

    async def get_quote(self, symbol: str, *, function: str, currency: str, unit: str) -> Quote:
        bars = await self.get_history(
            symbol, function=function, interval="1mo", start=datetime(1990, 1, 1, tzinfo=UTC)
        )
        if not bars:
            raise ProviderError("INSUFFICIENT_DATA", f"no {symbol} observations")
        latest = bars[-1]
        cur, u = COMMODITY_UNITS[symbol]
        if (cur, u) != (currency, unit):
            raise ProviderError("INVALID_RESPONSE", f"{symbol} is {cur}/{u}, not {currency}/{unit}")
        return Quote(
            price=latest.close,
            observed_at=latest.ts,
            currency=cur,
            unit=u,
            source=SOURCE,
            delay_note="Monthly global reference price (published with a lag)",
        )


def parse_errors(body: dict[str, Any]) -> dict[str, Any]:
    for key in ("Note", "Information"):
        if key in body:
            raise ProviderError(
                "RATE_LIMITED", "Alpha Vantage throttled the request", retryable=True
            )
    if "Error Message" in body:
        raise ProviderError("PROVIDER_ERROR", "Alpha Vantage rejected the request")
    return body


def parse_fx(body: dict[str, Any], base: str, quote: str) -> FxRate:
    data = body.get("Realtime Currency Exchange Rate")
    if not isinstance(data, dict):
        raise ProviderError("INVALID_RESPONSE", "missing 'Realtime Currency Exchange Rate'")
    if data.get("1. From_Currency Code") != base or data.get("3. To_Currency Code") != quote:
        raise ProviderError("INVALID_RESPONSE", "FX currency pair mismatch")
    rate = _dec(data.get("5. Exchange Rate"))
    if rate is None or rate <= 0:
        raise ProviderError("INVALID_RESPONSE", "FX rate missing or not positive")
    tz = str(data.get("7. Time Zone", "UTC"))
    if tz != "UTC":
        raise ProviderError("INVALID_RESPONSE", f"unexpected FX timezone {tz}")
    try:
        ts = datetime.strptime(str(data.get("6. Last Refreshed")), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=UTC
        )
    except ValueError as exc:
        raise ProviderError("INVALID_RESPONSE", "FX timestamp unparseable") from exc
    return FxRate(base=base, quote=quote, rate=rate, observed_at=ts, source=SOURCE)


def parse_commodity(body: dict[str, Any]) -> list[Bar]:
    rows = body.get("data")
    if not isinstance(rows, list):
        raise ProviderError("INVALID_RESPONSE", "commodity response has no data array")
    bars: list[Bar] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = _dec(row.get("value"))
        try:
            d = date.fromisoformat(str(row.get("date")))
        except ValueError:
            continue
        if value is None or value <= 0:
            continue  # Alpha Vantage uses "." for missing observations
        bars.append(
            Bar(
                ts=datetime(d.year, d.month, d.day, tzinfo=UTC),
                open=None,
                high=None,
                low=None,
                close=value,
            )
        )
    bars.sort(key=lambda b: b.ts)
    return bars
