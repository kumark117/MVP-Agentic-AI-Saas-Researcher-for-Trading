"""Hosted deployment settings (Render + Vercel; DV-15): URLs, SMTP, prod checks."""

from __future__ import annotations

import os
import sys
import time
from typing import Any, ClassVar
from urllib.parse import unquote, urlsplit

import pytest
from pydantic import SecretStr

from alloyiq.adapters.artifacts import LocalArtifactStore, PgArtifactStore, build_artifact_store
from alloyiq.adapters.email import OutgoingEmail, SmtpEmailSender
from alloyiq.cli.render import role_statement, role_urls, supervise
from alloyiq.config import Settings

ENV = {
    "DB_HOST": "dpg-x-a",
    "DB_PORT": "5432",
    "DB_NAME": "alloyiq",
    "ALLOYIQ_API_DB_PASSWORD": "a+b/c=",  # base64-style, needs URL encoding
    "ALLOYIQ_WORKER_DB_PASSWORD": "w@rk:er",
    "ALLOYIQ_MIGRATOR_DB_PASSWORD": "m1",
}


def test_role_urls_encode_passwords_and_use_each_role() -> None:
    urls = role_urls(ENV)
    api = urlsplit(urls["DATABASE_URL"])
    assert api.scheme == "postgresql+asyncpg" and api.username == "alloyiq_api"
    assert unquote(api.password or "") == "a+b/c=" and api.hostname == "dpg-x-a"
    assert unquote(urlsplit(urls["DATABASE_WORKER_URL"]).password or "") == "w@rk:er"
    assert urlsplit(urls["DATABASE_MIGRATOR_URL"]).username == "alloyiq_migrator"
    assert urls["ALEMBIC_DATABASE_URL"].startswith("postgresql+psycopg://alloyiq_migrator:")


def test_role_urls_require_every_password() -> None:
    with pytest.raises(SystemExit, match="ALLOYIQ_WORKER_DB_PASSWORD"):
        role_urls({**ENV, "ALLOYIQ_WORKER_DB_PASSWORD": ""})


class _FakeSmtp:
    calls: ClassVar[list[tuple[str, Any]]] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        _FakeSmtp.calls = [("connect", (host, port))]

    def __enter__(self) -> _FakeSmtp:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def starttls(self, context: object) -> None:
        _FakeSmtp.calls.append(("starttls", None))

    def login(self, user: str, password: str) -> None:
        _FakeSmtp.calls.append(("login", user))

    def send_message(self, msg: object) -> None:
        _FakeSmtp.calls.append(("send", None))


async def test_smtp_uses_starttls_and_login_for_hosted_smtp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("alloyiq.adapters.email.smtplib.SMTP", _FakeSmtp)
    sender = SmtpEmailSender(
        "smtp.gmail.com",
        587,
        "me@gmail.com",
        username="me@gmail.com",
        password="app-pw",
        starttls=True,
    )
    await sender.send(OutgoingEmail("you@example.com", "Hi", "Body"))
    assert [c[0] for c in _FakeSmtp.calls] == ["connect", "starttls", "login", "send"]


async def test_smtp_stays_plain_for_mailpit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("alloyiq.adapters.email.smtplib.SMTP", _FakeSmtp)
    await SmtpEmailSender("mailpit", 1025, "no-reply@x").send(OutgoingEmail("a@b.c", "s", "b"))
    assert [c[0] for c in _FakeSmtp.calls] == ["connect", "send"]


def test_prod_requires_an_https_public_origin() -> None:
    s = Settings(
        app_env="prod", public_origin="http://x.vercel.app", session_secret=SecretStr("s" * 32)
    )
    with pytest.raises(RuntimeError, match="https"):
        s.check()
    Settings(
        app_env="prod", public_origin="https://x.vercel.app", session_secret=SecretStr("s" * 32)
    ).check()


def test_artifact_backend_selects_the_store(tmp_path: object) -> None:
    assert isinstance(build_artifact_store(Settings(artifact_backend="postgres")), PgArtifactStore)
    assert isinstance(
        build_artifact_store(Settings(artifact_dir=str(tmp_path))), LocalArtifactStore
    )


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


SLEEP = _py("import time; time.sleep(60)")


def test_supervisor_stops_mcp_when_the_worker_exits() -> None:
    started = time.monotonic()
    code = supervise(SLEEP, _py("raise SystemExit(3)"), dict(os.environ), ready=lambda: True)
    assert code == 3  # the worker's code, so Render restarts the container
    assert time.monotonic() - started < 30  # MCP was stopped, not waited out


def test_supervisor_stops_the_worker_when_mcp_exits() -> None:
    mcp = _py("import time; time.sleep(1); raise SystemExit(4)")
    assert supervise(mcp, SLEEP, dict(os.environ), ready=lambda: True) == 4


def test_supervisor_never_starts_the_worker_if_mcp_fails_to_start() -> None:
    code = supervise(
        _py("raise SystemExit(5)"),
        _py("raise SystemExit(0)"),
        dict(os.environ),
        ready=lambda: False,
    )
    assert code == 5


def test_supervisor_gives_up_when_mcp_never_becomes_ready() -> None:
    code = supervise(
        SLEEP,
        _py("raise SystemExit(0)"),
        dict(os.environ),
        ready=lambda: False,
        ready_timeout_s=1.0,
    )
    assert code == 1


def test_role_bootstrap_never_names_superuser_attributes_on_alter() -> None:
    # Render's admin is not a superuser: ALTER ROLE ... NOSUPERUSER fails even as a no-op.
    alter = role_statement(exists=True)
    assert "PASSWORD" in alter
    assert not any(a in alter for a in ("SUPERUSER", "BYPASSRLS", "CREATEROLE", "CREATEDB"))
    create = role_statement(exists=False)
    assert "NOSUPERUSER" in create and "NOBYPASSRLS" in create
