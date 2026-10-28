"""ToolService and the private MCP service with scenario fakes (D035-D040, D047)."""

from __future__ import annotations

import asyncio
import socket
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, text

from alloyiq.adapters.fakes import FakeFx, FakeMarketData, FakeNews
from alloyiq.application.catalog import CatalogVerifier
from alloyiq.cli.seed import sid
from alloyiq.repositories.db import Database
from alloyiq.tools.gateway import InProcessToolGateway, MCPToolGateway
from alloyiq.tools.service import Providers, ToolService

from .apiclient import ApiClient, login, register_verified

pytestmark = pytest.mark.integration

GOLD = sid("instrument", "comex-gold-cont-ref")
COPPER_MONTHLY = sid("instrument", "global-copper-monthly-ref")
TOKEN = "test-mcp-service-token-0123456789"


@pytest.fixture
def enabled(migrator: Engine) -> None:
    with migrator.begin() as c:
        c.execute(text("UPDATE instruments SET enabled = true, unavailable_reason = NULL"))


def service(
    db: Database, market: str = "normal", news: str = "normal", deadline_s: float = 8.0
) -> ToolService:
    m = FakeMarketData(scenario=market)  # type: ignore[arg-type]
    return ToolService(
        db,
        Providers(
            market={"yahoo": m, "alphavantage": m}, fx=FakeFx(), news=FakeNews(scenario=news)
        ),  # type: ignore[arg-type]
        deadline_s=deadline_s,
        retry_delays=(0.01, 0.01),
    )


def args(instrument: uuid.UUID = GOLD, horizon: str = "swing") -> dict[str, Any]:
    return {"tool_call_id": str(uuid.uuid4()), "instrument_id": str(instrument), "horizon": horizon}


async def call(svc: ToolService, tool: str, a: dict[str, Any]) -> Any:
    async with InProcessToolGateway(svc).session() as s:
        return await s.call(tool, a)


async def test_quote_envelope(worker_db: Database, enabled: None) -> None:
    env = await call(service(worker_db), "get_quote", args())
    assert env.status == "ok" and env.data["price"] == "2400"
    assert env.currency == "USD" and env.unit == "troy_oz" and env.data_mode == "reference"
    assert env.provenance_hash and len(env.provenance_hash) == 64
    assert any("not a live exchange quote" in w for w in env.warnings)
    assert env.freshness_state in ("fresh", "delayed")


@pytest.mark.parametrize("scenario", ["unit_error", "future_timestamp", "bad_price"])
async def test_invalid_provider_quotes_are_rejected(
    worker_db: Database, enabled: None, scenario: str
) -> None:
    env = await call(service(worker_db, scenario), "get_quote", args())
    assert env.status == "error" and env.error.code == "INVALID_RESPONSE" and env.data is None


async def test_stale_quote_is_flagged_not_hidden(worker_db: Database, enabled: None) -> None:
    env = await call(service(worker_db, "stale_quote"), "get_quote", args(horizon="intraday"))
    assert env.status == "ok" and env.freshness_state in ("stale", "market_closed")
    assert any("Quote is" in w for w in env.warnings)


async def test_timeout_and_retries(worker_db: Database, enabled: None) -> None:
    env = await call(service(worker_db, "timeout", deadline_s=0.3), "get_quote", args())
    assert env.error.code == "PROVIDER_UNAVAILABLE" and env.error.retryable
    svc = service(worker_db, "rate_limited")
    env = await call(svc, "get_quote", args())
    assert env.error.code == "RATE_LIMITED"
    assert svc.providers.market["yahoo"].calls == 3  # type: ignore[attr-defined]  # 1 + 2 retries


async def test_indicators_are_computed_server_side(worker_db: Database, enabled: None) -> None:
    env = await call(service(worker_db), "get_technical_indicators", args(horizon="swing"))
    assert env.status == "ok" and env.data["bar_count"] >= 200
    assert set(env.data["values"]) == {"SMA20", "SMA50", "SMA200", "RSI14", "MACD", "ATR14", "BB20"}
    assert 0 <= float(env.data["values"]["RSI14"]) <= 100
    short = await call(service(worker_db, "missing_bars"), "get_technical_indicators", args())
    # 10 bars: no indicator is computable, so the tool refuses rather than guessing.
    assert short.status == "error" and short.error.code == "INSUFFICIENT_DATA"


async def test_history_is_bounded(worker_db: Database, enabled: None) -> None:
    env = await call(service(worker_db), "get_history", args(horizon="swing"))
    assert env.status == "ok" and env.data["count"] <= 400 and env.data["interval"] == "1d"


async def test_reference_series_rules(worker_db: Database, enabled: None) -> None:
    bad = await call(service(worker_db), "get_quote", args(COPPER_MONTHLY, "intraday"))
    assert bad.error.code == "UNSUPPORTED_INSTRUMENT"
    ind = await call(
        service(worker_db), "get_technical_indicators", args(COPPER_MONTHLY, "long_term")
    )
    assert ind.status == "ok" and ind.data["interval"] == "1mo"


async def test_disabled_and_unknown_instruments(
    worker_db: Database, migrator: Engine, enabled: None
) -> None:
    env = await call(service(worker_db), "get_quote", args(uuid.uuid4()))
    assert env.error.code == "UNSUPPORTED_INSTRUMENT"


async def test_bad_arguments_never_reach_providers(worker_db: Database, enabled: None) -> None:
    env = await call(service(worker_db), "get_quote", {**args(), "horizon": "forever"})
    assert env.error.code == "VALIDATION_FAILED"
    env = await call(
        service(worker_db), "search_news", {**args(), "focus": "ignore previous; DROP TABLE"}
    )
    assert env.error.code == "VALIDATION_FAILED"
    with pytest.raises(ValueError):
        await call(service(worker_db), "delete_everything", args())


async def test_news_is_labelled_untrusted(worker_db: Database, enabled: None) -> None:
    env = await call(service(worker_db, news="injection"), "search_news", args())
    assert env.status == "ok" and env.data["trust"].startswith("untrusted_external")
    assert "gold" in env.data["query"] and len(env.data["articles"]) == 3


async def test_fx(worker_db: Database) -> None:
    env = await call(service(worker_db), "get_fx_rate", {"tool_call_id": str(uuid.uuid4())})
    assert env.status == "ok" and env.data["rate"] == "85.0000" and env.currency == "INR"


async def test_catalog_verifier(worker_db: Database, enabled: None) -> None:
    ok = CatalogVerifier(worker_db, service(worker_db))
    from alloyiq.repositories.catalog import CatalogRepository
    from alloyiq.repositories.db import DbContext

    async with worker_db.transaction(DbContext.anonymous()) as c:
        gold = await CatalogRepository().get_instrument(c, GOLD)
    assert gold is not None
    assert (await ok.verify(gold)).passed
    bad = await CatalogVerifier(worker_db, service(worker_db, "missing_bars")).verify(gold)
    assert not bad.passed and "INSUFFICIENT_DATA" in (bad.reason or "")


# ------------------------------------------------------------------ real MCP over HTTP
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def mcp_url(worker_db: Database) -> AsyncIterator[str]:
    import uvicorn

    from alloyiq.mcp_server.app import build_app

    port = _free_port()
    app = build_app(service(worker_db), token=TOKEN, allowed_hosts=[f"127.0.0.1:{port}"])
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="on")
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    await task


async def test_mcp_over_streamable_http(mcp_url: str, enabled: None) -> None:
    async with MCPToolGateway(mcp_url, TOKEN).session() as s:
        env = await s.call("get_quote", args())
        assert env.status == "ok" and env.data["price"] == "2400"
        bad = await s.call("get_quote", {**args(), "horizon": "forever"})
        assert bad.status == "error"


async def test_mcp_lists_only_the_read_only_tools(mcp_url: str) -> None:
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    http = create_mcp_http_client(
        headers={"Authorization": f"Bearer {TOKEN}", "X-MCP-Audience": "alloyiq-tools"}
    )
    async with http, Client(streamable_http_client(mcp_url, http_client=http)) as client:
        names = sorted(t.name for t in (await client.list_tools()).tools)
    assert names == [
        "get_fx_rate",
        "get_history",
        "get_quote",
        "get_technical_indicators",
        "search_news",
    ]


async def test_mcp_rejects_missing_or_wrong_token(mcp_url: str) -> None:
    import httpx

    async with httpx.AsyncClient() as h:
        r = await h.post(mcp_url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 401
        r = await h.post(
            mcp_url,
            json={},
            headers={
                "Authorization": "Bearer wrong-token-xxxxxxxx",
                "X-MCP-Audience": "alloyiq-tools",
            },
        )
        assert r.status_code == 401
        r = await h.post(mcp_url, json={}, headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 401  # audience missing
        health = await h.get(mcp_url.replace("/mcp", "/health"))
        assert health.status_code == 200


# ------------------------------------------------------------------ catalogue API
async def test_catalog_routes(app: FastAPI, migrator: Engine) -> None:
    async with ApiClient(app) as c:
        assert (await c.get("/catalog/instruments")).status_code == 401
        await login(c, await register_verified(c, migrator))
        cat = (await c.get("/catalog/instruments")).json()["data"]
        ids = {i["canonical_id"] for i in cat["instruments"]}
        assert {"comex-gold-cont-ref", "global-copper-monthly-ref"} <= ids
        assert all(i["data_mode"] == "reference" for i in cat["instruments"])
        assert any(u["venue"] == "MCX" and "licensed" in u["reason"] for u in cat["unavailable"])
        gold = (await c.get("/catalog/instruments", params={"metal": "gold"})).json()["data"]
        assert {i["metal"] for i in gold["instruments"]} == {"gold"}
        models = (await c.get("/catalog/models")).json()["data"]
        deep = next(m for m in models if m["slug"] == "deep")
        assert deep["is_default"] and deep["approved_fallback_model_ids"] == ["claude-sonnet-5-5"]
        assert deep["limits"]["run_model_calls"] == 24
        prices = (await c.get("/catalog/prices")).json()["data"]
        assert prices["status"] == "draft" and prices["note"].startswith("DRAFT")
        cw = [i for i in prices["items"] if i["token_category"] == "cache_write"]
        assert cw and all(i["provider_usd_per_mtok"] is None for i in cw)
        opus_in = next(
            i
            for i in prices["items"]
            if i["model_id"] == "claude-opus-5-5" and i["token_category"] == "input"
        )
        assert opus_in["customer_inr_per_mtok_simulated"]["amount"] == "425.00"  # 4 x 85 x 1.25
    async with ApiClient(app) as anon:
        plans = (await anon.get("/catalog/plans")).json()["data"]
        assert plans[0]["slug"] == "demo" and plans[0]["active_runs"] == 2
