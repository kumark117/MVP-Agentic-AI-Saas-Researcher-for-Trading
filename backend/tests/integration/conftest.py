"""Integration fixtures against the compose PostgreSQL test database (alloyiq_test).

scripts/test.ps1 migrates and seeds the database and exports one URL per role:
TEST_DATABASE_URL (api), TEST_WORKER_DATABASE_URL (worker), TEST_MIGRATOR_DATABASE_URL.
Tests create their own users/accounts with random emails, so they never collide.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy import Engine, create_engine, text

from alloyiq.repositories.db import Database

API_URL = os.environ.get("TEST_DATABASE_URL")
WORKER_URL = os.environ.get("TEST_WORKER_DATABASE_URL")
MIGRATOR_URL = os.environ.get("TEST_MIGRATOR_DATABASE_URL")

requires_db = pytest.mark.skipif(
    not (API_URL and WORKER_URL and MIGRATOR_URL),
    reason="test database URLs not set (run scripts/test.ps1)",
)


@dataclass(frozen=True)
class Tenant:
    user_id: uuid.UUID
    account_id: uuid.UUID
    email: str


@pytest.fixture(scope="session")
def migrator() -> Iterator[Engine]:
    if not MIGRATOR_URL:
        pytest.skip("TEST_MIGRATOR_DATABASE_URL not set")
    engine = create_engine(MIGRATOR_URL)
    yield engine
    engine.dispose()


@pytest.fixture
async def api_db() -> AsyncIterator[Database]:
    if not API_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    db = Database(API_URL, pool_size=2, max_overflow=0)
    yield db
    await db.dispose()


@pytest.fixture
async def worker_db() -> AsyncIterator[Database]:
    if not WORKER_URL:
        pytest.skip("TEST_WORKER_DATABASE_URL not set")
    db = Database(WORKER_URL, pool_size=2, max_overflow=0)
    yield db
    await db.dispose()


def make_tenant(engine: Engine) -> Tenant:
    """Create a verified user with a personal account and wallet (as the migrator)."""
    uid, aid = uuid.uuid4(), uuid.uuid4()
    email = f"t-{uid.hex[:12]}@example.com"
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO users (id, email, email_verified_at, password_hash, display_name,"
                " terms_version, privacy_version) VALUES (:u, :e, now(), 'x', 'Test', 't1', 'p1')"
            ),
            {"u": uid, "e": email},
        )
        c.execute(
            text(
                "INSERT INTO accounts (id, owner_user_id, plan_id)"
                " VALUES (:a, :u, (SELECT id FROM plans WHERE slug = 'demo'))"
            ),
            {"a": aid, "u": uid},
        )
        c.execute(
            text(
                "INSERT INTO account_memberships (account_id, user_id, role) VALUES (:a, :u, 'member')"
            ),
            {"a": aid, "u": uid},
        )
        c.execute(text("INSERT INTO wallets (account_id) VALUES (:a)"), {"a": aid})
    return Tenant(uid, aid, email)


def make_settings(**overrides: object) -> object:
    from pydantic import SecretStr

    from alloyiq.config import Settings

    base: dict[str, object] = {
        "app_env": "test",
        "public_origin": "http://test",
        "database_url": SecretStr(API_URL or ""),
        "database_worker_url": SecretStr(WORKER_URL or ""),
        "session_secret": SecretStr("test-session-secret-0123456789"),
        "argon2_time_cost": 1,
        "argon2_memory_kib": 1024,
        "argon2_parallelism": 1,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture
async def app(api_db: Database):  # type: ignore[no-untyped-def]
    from alloyiq.api.app import create_app
    from alloyiq.api.deps import build_services
    from alloyiq.security.ratelimit import InMemoryRateLimiter

    settings = make_settings()
    services = build_services(settings, db=api_db, limiter=InMemoryRateLimiter())  # type: ignore[arg-type]

    async def ok() -> None:
        return None

    yield create_app(settings, services=services, readiness_probe=ok)  # type: ignore[arg-type]


@pytest.fixture
def tenant_a(migrator: Engine) -> Tenant:
    return make_tenant(migrator)


@pytest.fixture
def tenant_b(migrator: Engine) -> Tenant:
    return make_tenant(migrator)
