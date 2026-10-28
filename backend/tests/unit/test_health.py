"""Phase 0 contract checks for the API shell: envelope, problem+json, correlation."""

from __future__ import annotations

import uuid

import httpx
import pytest

from alloyiq.api.app import create_app
from alloyiq.config import Settings


async def _ok_probe() -> None:
    return None


async def _failing_probe() -> None:
    raise ConnectionError("db down: postgresql://user:secret@db/x")


def _client(probe=_ok_probe) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    app = create_app(Settings(app_env="test"), readiness_probe=probe)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_live_returns_envelope_with_meta() -> None:
    async with _client() as c:
        r = await c.get("/api/v1/health/live")
    assert r.status_code == 200
    body = r.json()
    assert body["data"] == {"status": "ok"}
    assert uuid.UUID(body["meta"]["request_id"])
    assert body["meta"]["server_time"].endswith("Z") or "+00:00" in body["meta"]["server_time"]
    assert r.headers["x-request-id"] == body["meta"]["request_id"]


async def test_ready_ok_when_probe_passes() -> None:
    async with _client() as c:
        r = await c.get("/api/v1/health/ready")
    assert r.status_code == 200
    assert r.json()["data"] == {"status": "ok"}


async def test_ready_503_problem_json_without_leaking_details() -> None:
    async with _client(_failing_probe) as c:
        r = await c.get("/api/v1/health/ready")
    assert r.status_code == 503
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.headers["retry-after"] == "5"
    body = r.json()
    assert body["code"] == "PROVIDER_UNAVAILABLE"
    assert body["instance"] == "/api/v1/health/ready"
    assert body["request_id"] == r.headers["x-request-id"]
    assert "secret" not in r.text and "postgresql" not in r.text


async def test_unknown_route_is_problem_404() -> None:
    async with _client() as c:
        r = await c.get("/api/v1/nope")
    assert r.status_code == 404
    assert r.json()["code"] == "RESOURCE_NOT_FOUND"


@pytest.mark.parametrize(
    ("incoming", "kept"),
    [(str(uuid.UUID(int=7)), True), ("not-a-uuid\r\nx: y", False), ("", False)],
)
async def test_public_request_id_accepted_only_if_uuid(incoming: str, kept: bool) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/v1/health/live", headers={"X-Request-ID": incoming} if incoming else {}
        )
    rid = r.headers["x-request-id"]
    assert (rid == incoming) is kept
    assert uuid.UUID(rid)


def test_compose_inline_comment_is_not_a_secret() -> None:
    from pydantic import SecretStr

    from alloyiq.config import secret_or_none

    assert secret_or_none(SecretStr("# optional; empty = local trace sink")) is None
    assert secret_or_none(SecretStr("   ")) is None and secret_or_none(None) is None
    assert secret_or_none(SecretStr(" lsv2_abc ")) == "lsv2_abc"
