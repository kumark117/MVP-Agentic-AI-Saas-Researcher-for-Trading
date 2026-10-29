"""Hosted deployment (Render; DV-15): the Postgres ArtifactStore and in-process tools."""

from __future__ import annotations

import uuid

from sqlalchemy import Engine, text

from alloyiq.adapters.artifacts import PgArtifactStore
from alloyiq.repositories.db import Database, DbContext
from alloyiq.tools.gateway import InProcessToolGateway
from alloyiq.workflows.factory import build_workflow

from .conftest import Tenant, make_settings
from .test_hardening import _purge


async def test_pg_artifact_store_round_trip_is_tenant_scoped(
    worker_db: Database, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    store, key = PgArtifactStore(), f"export/{uuid.uuid4()}.csv"
    async with worker_db.transaction(DbContext(account_id=tenant_a.account_id)) as c:
        await store.put(
            c,
            account_id=tenant_a.account_id,
            key=key,
            data=b"a,b\n1,2\n",
            content_type="text/csv",
            retention_class="ephemeral_7d",
        )
    async with worker_db.transaction(DbContext(account_id=tenant_a.account_id)) as c:
        assert await store.get(c, key) == b"a,b\n1,2\n"
    async with worker_db.transaction(DbContext(account_id=tenant_b.account_id)) as c:
        assert await store.get(c, key) is None  # RLS: another tenant never sees it


async def test_pg_artifact_store_rejects_tampered_bytes(
    migrator: Engine, worker_db: Database, tenant_a: Tenant
) -> None:
    store, key = PgArtifactStore(), f"export/{uuid.uuid4()}.csv"
    async with worker_db.transaction(DbContext(account_id=tenant_a.account_id)) as c:
        await store.put(
            c, account_id=tenant_a.account_id, key=key, data=b"original", content_type="text/csv"
        )
    with migrator.begin() as c:
        c.execute(text("UPDATE artifacts SET content = 'changed' WHERE key = :k"), {"k": key})
    async with worker_db.transaction(DbContext(account_id=tenant_a.account_id)) as c:
        assert await store.get(c, key) is None


async def test_retention_purge_clears_artifact_bytes(
    migrator: Engine, worker_db: Database, tenant_a: Tenant, tmp_path: object
) -> None:
    store, key = PgArtifactStore(), f"export/{uuid.uuid4()}.csv"
    async with worker_db.transaction(DbContext(account_id=tenant_a.account_id)) as c:
        await store.put(
            c,
            account_id=tenant_a.account_id,
            key=key,
            data=b"old export",
            content_type="text/csv",
            retention_class="ephemeral_7d",
        )
    with migrator.begin() as c:
        c.execute(
            text("UPDATE artifacts SET created_at = now() - interval '8 days' WHERE key = :k"),
            {"k": key},
        )
    await _purge(worker_db, tmp_path).run_once()  # type: ignore[arg-type]
    with migrator.connect() as c:
        row = c.execute(
            text("SELECT deleted_at, content FROM artifacts WHERE key = :k"), {"k": key}
        ).one()
    assert row.deleted_at is not None and row.content is None


async def test_in_process_tool_gateway_is_selected_by_setting(worker_db: Database) -> None:
    settings = make_settings(tool_gateway="in_process", llm_mode="fake", provider_mode="fixtures")
    workflow = build_workflow(settings, worker_db)  # type: ignore[arg-type]
    assert isinstance(workflow.tool_sessions.__self__, InProcessToolGateway)  # type: ignore[attr-defined]
