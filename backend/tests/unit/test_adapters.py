"""Provider adapters parse documented shapes and turn failures into typed errors."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from alloyiq.adapters.alphavantage import (
    AlphaVantageClient,
    parse_commodity,
    parse_errors,
    parse_fx,
)
from alloyiq.adapters.tavily import TavilyClient, parse_results
from alloyiq.adapters.yahoo import YahooFinanceClient, frame_to_bars
from alloyiq.domain.ports import ProviderError
from alloyiq.security.egress import EgressPolicy, SafeHttpClient

FX_OK = {
    "Realtime Currency Exchange Rate": {
        "1. From_Currency Code": "USD",
        "2. From_Currency Name": "United States Dollar",
        "3. To_Currency Code": "INR",
        "4. To_Currency Name": "Indian Rupee",
        "5. Exchange Rate": "84.12500000",
        "6. Last Refreshed": "2026-10-28 03:00:01",
        "7. Time Zone": "UTC",
        "8. Bid Price": "84.12",
        "9. Ask Price": "84.13",
    }
}


def test_parse_fx_ok() -> None:
    fx = parse_fx(FX_OK, "USD", "INR")
    assert fx.rate == Decimal("84.12500000")
    assert fx.observed_at == datetime(2026, 10, 28, 3, 0, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update({"3. To_Currency Code": "EUR"}),
        lambda d: d.update({"5. Exchange Rate": "-1"}),
        lambda d: d.update({"6. Last Refreshed": "yesterday"}),
        lambda d: d.update({"7. Time Zone": "US/Eastern"}),
    ],
)
def test_parse_fx_rejects_bad_payloads(mutate) -> None:  # type: ignore[no-untyped-def]
    body = {"Realtime Currency Exchange Rate": dict(FX_OK["Realtime Currency Exchange Rate"])}
    mutate(body["Realtime Currency Exchange Rate"])
    with pytest.raises(ProviderError) as e:
        parse_fx(body, "USD", "INR")
    assert e.value.code == "INVALID_RESPONSE"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (
            {"Note": "Thank you for using Alpha Vantage! Our standard API rate limit is..."},
            "RATE_LIMITED",
        ),
        ({"Information": "The **demo** API key is for demo purposes only."}, "RATE_LIMITED"),
        ({"Error Message": "Invalid API call."}, "PROVIDER_ERROR"),
    ],
)
def test_http_200_error_bodies_become_typed_errors(body: dict[str, str], code: str) -> None:
    with pytest.raises(ProviderError) as e:
        parse_errors(body)
    assert e.value.code == code


def test_parse_commodity_skips_missing_and_sorts() -> None:
    bars = parse_commodity(
        {
            "name": "Global Price of Copper",
            "interval": "monthly",
            "unit": "dollar per metric ton",
            "data": [
                {"date": "2026-09-01", "value": "9800.5"},
                {"date": "2026-08-01", "value": "."},
                {"date": "2026-07-01", "value": "9650.25"},
            ],
        }
    )
    assert [b.close for b in bars] == [Decimal("9650.25"), Decimal("9800.5")]
    assert bars[0].high is None


def _client(handler) -> SafeHttpClient:  # type: ignore[no-untyped-def]
    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    return SafeHttpClient(EgressPolicy(resolver=resolve), transport=httpx.MockTransport(handler))


async def test_alphavantage_throttle_over_http() -> None:
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["key"] = req.url.params.get("apikey")
        return httpx.Response(200, json={"Note": "rate limit"})

    av = AlphaVantageClient(_client(handler), "secret-key")
    with pytest.raises(ProviderError) as e:
        await av.get_rate("USD", "INR")
    assert e.value.code == "RATE_LIMITED" and e.value.retryable
    assert seen["key"] == "secret-key"


async def test_missing_keys_are_not_configured() -> None:
    with pytest.raises(ProviderError) as e:
        await AlphaVantageClient(_client(lambda r: httpx.Response(200)), None).get_rate(
            "USD", "INR"
        )
    assert e.value.code == "NOT_CONFIGURED"
    with pytest.raises(ProviderError) as e:
        await TavilyClient(_client(lambda r: httpx.Response(200)), "").search(
            "gold", days=1, max_results=3
        )
    assert e.value.code == "NOT_CONFIGURED"


async def test_tavily_request_is_bounded_and_authenticated() -> None:
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        import json

        captured["body"] = json.loads(req.content)
        captured["auth"] = req.headers["authorization"]
        return httpx.Response(200, json={"results": []})

    await TavilyClient(_client(handler), "tvly-x").search("g" * 500, days=900, max_results=50)
    assert len(captured["body"]["query"]) == 200
    assert captured["body"]["max_results"] == 8 and captured["body"]["days"] == 365
    assert captured["body"]["topic"] == "news" and captured["auth"] == "Bearer tvly-x"


async def test_tavily_429_is_rate_limited() -> None:
    with pytest.raises(ProviderError) as e:
        await TavilyClient(_client(lambda r: httpx.Response(429)), "k").search(
            "g", days=1, max_results=1
        )
    assert e.value.code == "RATE_LIMITED"


def test_tavily_results_drop_unsafe_links_and_truncate() -> None:
    arts = parse_results(
        {
            "results": [
                {
                    "title": "OK",
                    "url": "https://news.example.com/a",
                    "content": "x" * 900,
                    "published_date": "2026-10-27T10:00:00Z",
                    "score": 0.8,
                },
                {"title": "XSS", "url": "javascript:alert(1)", "content": "bad"},
                {"title": "", "url": "https://news.example.com/b"},
                {"title": "Data URL", "url": "data:text/html,hi"},
            ]
        }
    )
    assert [a.title for a in arts] == ["OK"]
    assert len(arts[0].excerpt) == 500 and arts[0].source_domain == "news.example.com"
    assert arts[0].published_at == datetime(2026, 10, 27, 10, tzinfo=UTC)


class _Ts:
    """Stands in for a pandas Timestamp (only to_pydatetime is used)."""

    def __init__(self, dt: datetime) -> None:
        self.dt = dt

    def to_pydatetime(self) -> datetime:
        return self.dt


class _Frame:
    """Minimal stand-in for the yfinance DataFrame; importing pandas in unit tests is slow."""

    empty = False

    def __init__(self, rows: list[tuple[datetime, dict[str, float]]]) -> None:
        self.rows = rows

    def iterrows(self):  # type: ignore[no-untyped-def]
        return ((_Ts(t), r) for t, r in self.rows)


def test_yahoo_frame_conversion_skips_nan_and_bad_rows() -> None:
    t0 = datetime(2026, 10, 27, 14, tzinfo=UTC)
    row = {"Open": 1.0, "High": 1.5, "Low": 0.5, "Volume": 10}
    frame = _Frame(
        [
            (t0.replace(tzinfo=None), {**row, "Close": 1.2}),  # naive -> treated as UTC
            (t0 + timedelta(minutes=15), {**row, "Close": math.nan}),
            (t0 + timedelta(minutes=30), {**row, "Close": -3.0}),
            (t0 + timedelta(minutes=45), {**row, "Close": 3.1}),
        ]
    )
    bars = frame_to_bars(frame)
    assert [str(b.close) for b in bars] == ["1.2", "3.1"]
    assert bars[0].ts.tzinfo is not None


async def test_yahoo_disabled_and_symbol_allowlist() -> None:
    with pytest.raises(ProviderError) as e:
        await YahooFinanceClient(enabled=False).get_history(
            "GC=F", function="", interval="1d", start=datetime.now(UTC)
        )
    assert e.value.code == "NOT_CONFIGURED"
    with pytest.raises(ProviderError):
        await YahooFinanceClient(enabled=True).get_history(
            "AAPL", function="", interval="1d", start=datetime.now(UTC)
        )
