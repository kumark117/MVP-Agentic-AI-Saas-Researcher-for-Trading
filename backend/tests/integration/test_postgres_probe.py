"""Readiness probe against the real compose PostgreSQL, as the api role."""

from __future__ import annotations

import os

import pytest

from alloyiq.adapters.postgres_probe import postgres_probe

pytestmark = pytest.mark.integration

URL = os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set (scripts/test.ps1 sets it)")
async def test_probe_succeeds_against_compose_postgres() -> None:
    await postgres_probe(URL)()


async def test_probe_fails_fast_on_unreachable_host() -> None:
    probe = postgres_probe("postgresql+asyncpg://u:p@127.0.0.1:1/x", timeout_s=2.0)
    with pytest.raises(OSError):
        await probe()


async def test_probe_fails_without_url() -> None:
    with pytest.raises(RuntimeError):
        await postgres_probe(None)()
