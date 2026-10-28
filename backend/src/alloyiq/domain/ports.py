"""Provider interfaces (D028). Adapters implement these; nothing here performs I/O."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from alloyiq.domain.market import Bar, FxRate, NewsArticle, Quote

ProviderErrorCode = Literal[
    "PROVIDER_UNAVAILABLE",  # network, 5xx, timeout
    "RATE_LIMITED",  # provider throttling (e.g. Alpha Vantage "Note" body)
    "PROVIDER_ERROR",  # provider said the request failed
    "INVALID_RESPONSE",  # shape/units/timestamps failed validation
    "NOT_CONFIGURED",  # missing key or disabled adapter
    "INSUFFICIENT_DATA",
]


class ProviderError(Exception):
    def __init__(self, code: ProviderErrorCode, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code: ProviderErrorCode = code
        self.retryable = retryable


class MarketDataProvider(Protocol):
    name: str

    async def get_quote(self, symbol: str, *, function: str, currency: str, unit: str) -> Quote: ...

    async def get_history(
        self, symbol: str, *, function: str, interval: str, start: datetime
    ) -> list[Bar]: ...


class FxProvider(Protocol):
    name: str

    async def get_rate(self, base: str, quote: str) -> FxRate: ...


class NewsProvider(Protocol):
    name: str

    async def search(self, query: str, *, days: int, max_results: int) -> list[NewsArticle]: ...
