"""Account isolation through PostgreSQL row-level security (R012, D076)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alloyiq.repositories.db import Database, DbContext

from .conftest import Tenant

pytestmark = pytest.mark.integration

INSERT_CONVERSATION = text(
    "INSERT INTO conversations (account_id, user_id, title) VALUES (:a, :u, :t) RETURNING id"
)
OWN_CONVERSATIONS = text("SELECT account_id FROM conversations WHERE title = :t")


def _ctx(t: Tenant) -> DbContext:
    return DbContext(account_id=t.account_id, user_id=t.user_id)


async def _seed_conversations(db: Database, a: Tenant, b: Tenant, title: str) -> None:
    for t in (a, b):
        async with db.transaction(_ctx(t)) as c:
            await c.execute(INSERT_CONVERSATION, {"a": t.account_id, "u": t.user_id, "t": title})


async def test_api_sees_only_its_own_account(
    api_db: Database, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    title = f"rls-{uuid.uuid4()}"
    await _seed_conversations(api_db, tenant_a, tenant_b, title)
    async with api_db.transaction(_ctx(tenant_a)) as c:
        owners = (await c.execute(OWN_CONVERSATIONS, {"t": title})).scalars().all()
    assert owners == [tenant_a.account_id]


async def test_no_context_sees_nothing(
    api_db: Database, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    title = f"rls-{uuid.uuid4()}"
    await _seed_conversations(api_db, tenant_a, tenant_b, title)
    async with api_db.transaction(DbContext.anonymous()) as c:
        assert (await c.execute(OWN_CONVERSATIONS, {"t": title})).all() == []


async def test_cannot_write_into_another_account(
    api_db: Database, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    with pytest.raises(DBAPIError, match="row-level security"):
        async with api_db.transaction(_ctx(tenant_a)) as c:
            await c.execute(
                INSERT_CONVERSATION, {"a": tenant_b.account_id, "u": tenant_b.user_id, "t": "x"}
            )


async def test_api_cannot_enter_system_scope(
    api_db: Database, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    title = f"rls-{uuid.uuid4()}"
    await _seed_conversations(api_db, tenant_a, tenant_b, title)
    async with api_db.transaction(DbContext.for_system()) as c:
        assert (await c.execute(OWN_CONVERSATIONS, {"t": title})).all() == []


async def test_worker_system_scope_is_explicit(
    worker_db: Database, api_db: Database, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    title = f"rls-{uuid.uuid4()}"
    await _seed_conversations(api_db, tenant_a, tenant_b, title)
    async with worker_db.transaction(DbContext.anonymous()) as c:
        assert (await c.execute(OWN_CONVERSATIONS, {"t": title})).all() == []
    async with worker_db.transaction(DbContext.for_system()) as c:
        owners = set((await c.execute(OWN_CONVERSATIONS, {"t": title})).scalars().all())
    assert owners == {tenant_a.account_id, tenant_b.account_id}


async def test_stale_pooled_connection_never_leaks_context(
    tenant_a: Tenant, tenant_b: Tenant
) -> None:
    from .conftest import API_URL

    assert API_URL
    db = Database(API_URL, pool_size=1, max_overflow=0)  # force connection reuse
    try:
        title = f"rls-{uuid.uuid4()}"
        await _seed_conversations(db, tenant_a, tenant_b, title)
        async with db.transaction(_ctx(tenant_a)) as c:
            pid_a = (await c.execute(text("SELECT pg_backend_pid()"))).scalar()
            assert len((await c.execute(OWN_CONVERSATIONS, {"t": title})).all()) == 1
        # Same physical connection, next transaction, no context: nothing visible.
        async with db.transaction(DbContext.anonymous()) as c:
            assert (await c.execute(text("SELECT pg_backend_pid()"))).scalar() == pid_a
            assert (await c.execute(text("SELECT app_account_id()"))).scalar() is None
            assert (await c.execute(OWN_CONVERSATIONS, {"t": title})).all() == []
        # A raw transaction that never sets context also sees nothing.
        async with db.engine.begin() as raw:
            assert (await raw.execute(text("SELECT app_account_id()"))).scalar() is None
    finally:
        await db.dispose()


async def test_users_table_isolated_by_user(
    api_db: Database, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    async with api_db.transaction(_ctx(tenant_a)) as c:
        emails = (await c.execute(text("SELECT email FROM users"))).scalars().all()
    assert emails == [tenant_a.email]
