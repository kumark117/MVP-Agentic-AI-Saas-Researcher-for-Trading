"""Builds the ToolService with real adapters (or fakes in fixture mode) from settings."""

from __future__ import annotations

from alloyiq.adapters.alphavantage import AlphaVantageClient
from alloyiq.adapters.fakes import FakeFx, FakeMarketData, FakeNews
from alloyiq.adapters.tavily import TavilyClient
from alloyiq.adapters.yahoo import YahooFinanceClient
from alloyiq.config import Settings, secret_or_none
from alloyiq.repositories.db import Database
from alloyiq.security.egress import EgressPolicy, SafeHttpClient
from alloyiq.tools.cache import Cache, NullCache, RedisCache
from alloyiq.tools.service import Providers, ToolService


def build_providers(settings: Settings) -> Providers:
    if settings.provider_mode == "fixtures":
        market = FakeMarketData()
        return Providers(
            market={"yahoo": market, "alphavantage": market}, fx=FakeFx(), news=FakeNews()
        )
    http = SafeHttpClient(EgressPolicy())
    av_key = secret_or_none(settings.alphavantage_api_key)
    tv_key = secret_or_none(settings.tavily_api_key)
    av = AlphaVantageClient(http, av_key)
    return Providers(
        market={"yahoo": YahooFinanceClient(settings.enable_yahoo_reference), "alphavantage": av},
        fx=av,
        news=TavilyClient(http, tv_key),
    )


def build_tool_service(settings: Settings, db: Database) -> ToolService:
    cache: Cache = RedisCache(settings.redis_url) if settings.redis_url else NullCache()
    return ToolService(db, build_providers(settings), cache=cache)
