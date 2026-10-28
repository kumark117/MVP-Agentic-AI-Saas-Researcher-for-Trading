"""HTTP test client helpers: one ApiClient per simulated browser/device."""

from __future__ import annotations

import re
import uuid
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy import Engine, text

ORIGIN = "http://test"
PASSWORD = "correct horse battery staple"


class ApiClient:
    """A browser: keeps its own cookies, sends Origin and the CSRF header on mutations."""

    def __init__(self, app: FastAPI, ip: str = "127.0.0.1") -> None:
        transport = httpx.ASGITransport(app=app, client=(ip, 50000))
        self.http = httpx.AsyncClient(
            transport=transport,
            base_url=ORIGIN,
            headers={
                "Origin": ORIGIN,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/130.0 Safari/537.36",
            },
        )
        self.csrf: str | None = None

    async def __aenter__(self) -> ApiClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.http.aclose()

    async def refresh_csrf(self) -> str:
        r = await self.http.get("/api/v1/auth/csrf")
        r.raise_for_status()
        self.csrf = r.json()["data"]["csrf_token"]
        return self.csrf

    async def get(self, path: str, **kw: Any) -> httpx.Response:
        return await self.http.get(f"/api/v1{path}", **kw)

    async def send(
        self, method: str, path: str, json: Any = None, *, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        if self.csrf is None:
            await self.refresh_csrf()
        h = {"X-CSRF-Token": self.csrf or "", **(headers or {})}
        r = await self.http.request(method, f"/api/v1{path}", json=json, headers=h)
        body = r.json() if r.headers.get("content-type", "").endswith("json") else {}
        token = (body.get("data") or {}).get("csrf_token") if isinstance(body, dict) else None
        if token:
            self.csrf = token
        return r

    async def post(self, path: str, json: Any = None, **kw: Any) -> httpx.Response:
        return await self.send("POST", path, json, **kw)

    async def put(self, path: str, json: Any = None, **kw: Any) -> httpx.Response:
        return await self.send("PUT", path, json, **kw)


def new_email() -> str:
    return f"u-{uuid.uuid4().hex[:12]}@example.com"


def latest_link(engine: Engine, email: str, template: str) -> str:
    with engine.connect() as c:
        payload = c.execute(
            text(
                "SELECT payload FROM email_outbox WHERE to_email = :e AND template = :t"
                " ORDER BY created_at DESC LIMIT 1"
            ),
            {"e": email, "t": template},
        ).scalar_one()
    return str(payload["link"])


def token_from(link: str) -> str:
    m = re.search(r"#token=([\w-]+)$", link)
    assert m, link
    return m.group(1)


async def register_verified(
    client: ApiClient, engine: Engine, email: str | None = None, password: str = PASSWORD
) -> str:
    email = email or new_email()
    r = await client.post(
        "/auth/register",
        {
            "email": email,
            "password": password,
            "display_name": "Tester",
            "terms_version": "t1",
            "privacy_version": "p1",
        },
    )
    assert r.status_code == 202, r.text
    token = token_from(latest_link(engine, email, "verify_email"))
    r = await client.post("/auth/verify-email", {"token": token})
    assert r.status_code == 200, r.text
    return email


async def login(client: ApiClient, email: str, password: str = PASSWORD) -> httpx.Response:
    r = await client.post("/auth/login", {"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r
