"""The migrated database matches the metadata, and every table follows the schema rules."""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, text

from alloyiq.repositories.schema import RETENTION_CLASSES, metadata

pytestmark = pytest.mark.integration


def test_no_drift_between_migrations_and_metadata(migrator: Engine) -> None:
    with migrator.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(ctx, metadata)
    assert diff == [], f"schema drift: {diff}"


def test_every_table_documents_a_retention_class(migrator: Engine) -> None:
    with migrator.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.relname, obj_description(c.oid) FROM pg_class c"
                " JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname <> 'alembic_version'"
            )
        ).all()
    assert len(rows) >= 50
    for name, comment in rows:
        assert comment and comment.startswith("retention="), name
        assert comment.split(";")[0].removeprefix("retention=") in RETENTION_CLASSES, name


def test_every_account_owned_table_forces_rls(migrator: Engine) -> None:
    with migrator.connect() as conn:
        missing = (
            conn.execute(
                text(
                    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname IN ('account_id','user_id')"
                    " WHERE n.nspname = 'public' AND c.relkind = 'r'"
                    " AND NOT (c.relrowsecurity AND c.relforcerowsecurity)"
                )
            )
            .scalars()
            .all()
        )
    assert missing == []


def test_application_roles_are_unprivileged(migrator: Engine) -> None:
    with migrator.connect() as conn:
        roles = conn.execute(
            text(
                "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles"
                " WHERE rolname IN ('alloyiq_api','alloyiq_worker')"
            )
        ).all()
        owned = conn.execute(
            text(
                "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
                " AND tableowner IN ('alloyiq_api','alloyiq_worker')"
            )
        ).scalar()
    assert {r.rolname for r in roles} == {"alloyiq_api", "alloyiq_worker"}
    assert all(not r.rolsuper and not r.rolbypassrls for r in roles)
    assert owned == 0
