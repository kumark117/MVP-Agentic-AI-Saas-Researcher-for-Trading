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
from decimal import Decimal

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


def make_run(engine: Engine, t: Tenant, max_charge: str = "25.00") -> uuid.UUID:
    """A minimal queued run (with its estimate) so reservations have a run to belong to."""
    eid, rid = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as c:
        ins, prof = c.execute(
            text(
                "SELECT (SELECT id FROM instruments ORDER BY canonical_id LIMIT 1),"
                " (SELECT id FROM model_profiles WHERE slug = 'deep')"
            )
        ).one()
        c.execute(
            text(
                "INSERT INTO estimates (id, account_id, user_id, normalized_request,"
                " request_fingerprint, instrument_id, horizon, model_profile_id, approved_models,"
                " limits, rate_card_version, min_charge, estimated_charge, max_charge,"
                " max_real_usd, expires_at) VALUES (:e, :a, :u, '{}', 'fp', :i, 'swing', :p,"
                " ARRAY['claude-opus-5-5'], '{}', 'v-test', 0, 0, :m, 1, now() + interval '5 min')"
            ),
            {"e": eid, "a": t.account_id, "u": t.user_id, "i": ins, "p": prof, "m": max_charge},
        )
        c.execute(
            text(
                "INSERT INTO research_runs (id, account_id, user_id, estimate_id, instrument_id,"
                " instrument_snapshot, horizon, model_profile_id, versions, limits,"
                " authorized_max_charge, reserved_amount) VALUES (:r, :a, :u, :e, :i, '{}',"
                " 'swing', :p, '{}', '{}', :m, :m)"
            ),
            {
                "r": rid,
                "a": t.account_id,
                "u": t.user_id,
                "e": eid,
                "i": ins,
                "p": prof,
                "m": max_charge,
            },
        )
    return rid


def grant(engine: Engine, t: Tenant, amount: str) -> None:
    """Fund a test wallet with an adjustment journal (as the migrator, like grant-credit)."""
    tid = uuid.uuid4()
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO ledger_transactions (id, account_id, business_event_id, kind,"
                " description) VALUES (:t, :a, :b, 'adjustment', 'test funding')"
            ),
            {"t": tid, "a": t.account_id, "b": f"adjustment:test-{tid}"},
        )
        amt_d = Decimal(amount)
        for acct, amt in ((f"wallet:{t.account_id}", str(amt_d)), ("sim_adjustments", str(-amt_d))):
            c.execute(
                text(
                    "INSERT INTO ledger_postings (transaction_id, account_id, ledger_account,"
                    " amount, currency) VALUES (:t, :a, :l, CAST(:m AS numeric), 'INR')"
                ),
                {"t": tid, "a": t.account_id, "l": acct, "m": amt},
            )
        c.execute(
            text(
                "UPDATE wallets SET posted_credit = posted_credit + greatest(CAST(:m AS numeric), 0),"
                " posted_debit = posted_debit + greatest(-CAST(:m AS numeric), 0)"
                " WHERE account_id = :a"
            ),
            {"a": t.account_id, "m": str(amt_d)},
        )


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
        # The shared test database accumulates today's platform spend holds across tests;
        # budget behaviour has its own tests with explicit caps (test_wallet_ledger.py).
        "real_spend_account_day_usd": Decimal("1000000"),
        "real_spend_platform_day_usd": Decimal("1000000"),
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


GOLD = "comex-gold-cont-ref"


@pytest.fixture
def enabled_gold(migrator: Engine) -> Iterator[uuid.UUID]:
    """Enable the gold reference instrument for the test (seed leaves all disabled)."""
    with migrator.begin() as c:
        row = c.execute(
            text("SELECT id, enabled, unavailable_reason FROM instruments WHERE canonical_id = :c"),
            {"c": GOLD},
        ).one()
        c.execute(
            text("UPDATE instruments SET enabled = true, unavailable_reason = NULL WHERE id = :i"),
            {"i": row.id},
        )
    yield row.id
    with migrator.begin() as c:
        c.execute(
            text("UPDATE instruments SET enabled = :e, unavailable_reason = :r WHERE id = :i"),
            {"i": row.id, "e": row.enabled, "r": row.unavailable_reason},
        )


def profile_id(engine: Engine, slug: str = "deep") -> uuid.UUID:
    with engine.connect() as c:
        pid: uuid.UUID = c.execute(
            text("SELECT id FROM model_profiles WHERE slug = :s"), {"s": slug}
        ).scalar_one()
    return pid
