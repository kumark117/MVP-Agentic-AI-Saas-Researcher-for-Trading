"""Tavily news search adapter (POST https://api.tavily.com/search).

Bounded query and result count, recency window by horizon, topic=news. Only titles, URLs,
dates and short excerpts are kept until the text licence is confirmed (OD-4).
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from alloyiq.domain.market import NewsArticle
from alloyiq.domain.ports import ProviderError
from alloyiq.security.egress import SafeHttpClient

URL = "https://api.tavily.com/search"
MAX_RESULTS = 8
MAX_QUERY_CHARS = 200
EXCERPT_CHARS = 500


class TavilyClient:
    name = "tavily"

    def __init__(
        self, http: SafeHttpClient, api_key: str | None, include_domains: list[str] | None = None
    ) -> None:
        self.http = http
        self._key = api_key
        self.include_domains = include_domains or []

    async def search(self, query: str, *, days: int, max_results: int) -> list[NewsArticle]:
        if not self._key:
            raise ProviderError("NOT_CONFIGURED", "TAVILY_API_KEY is not set")
        body: dict[str, Any] = {
            "query": query[:MAX_QUERY_CHARS],
            "topic": "news",
            "days": max(1, min(days, 365)),
            "max_results": max(1, min(max_results, MAX_RESULTS)),
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
        }
        if self.include_domains:
            body["include_domains"] = self.include_domains
        try:
            resp = await self.http.request(
                "POST", URL, json=body, headers={"Authorization": f"Bearer {self._key}"}
            )
        except httpx.TimeoutException as exc:
            raise ProviderError("PROVIDER_UNAVAILABLE", "Tavily timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                "PROVIDER_UNAVAILABLE", "Tavily unreachable", retryable=True
            ) from exc
        if resp.status_code == 429:
            raise ProviderError("RATE_LIMITED", "Tavily rate limit", retryable=True)
        if resp.status_code >= 500:
            raise ProviderError(
                "PROVIDER_UNAVAILABLE", f"Tavily HTTP {resp.status_code}", retryable=True
            )
        if resp.status_code != 200:
            raise ProviderError("PROVIDER_ERROR", f"Tavily HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError("INVALID_RESPONSE", "Tavily returned non-JSON") from exc
        return parse_results(data)


def _published(v: object) -> datetime | None:
    if not v:
        return None
    s = str(v)
    for parse in (
        lambda x: datetime.fromisoformat(x.replace("Z", "+00:00")),
        parsedate_to_datetime,
    ):
        try:
            dt = parse(s)
        except (ValueError, TypeError):
            continue
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def parse_results(data: object) -> list[NewsArticle]:
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ProviderError("INVALID_RESPONSE", "Tavily response has no results array")
    out: list[NewsArticle] = []
    for r in data["results"][:MAX_RESULTS]:
        if not isinstance(r, dict):
            continue
        url, title = str(r.get("url") or ""), str(r.get("title") or "").strip()
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or not title:
            continue  # unsafe or unusable link: dropped, never rendered
        score = r.get("score")
        out.append(
            NewsArticle(
                title=title[:300],
                url=url[:2000],
                published_at=_published(r.get("published_date")),
                excerpt=str(r.get("content") or "")[:EXCERPT_CHARS],
                source_domain=parts.hostname.lower(),
                score=float(score) if isinstance(score, int | float) else None,
            )
        )
    return out
