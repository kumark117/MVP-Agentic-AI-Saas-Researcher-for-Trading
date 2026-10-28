"""Alembic environment. Always runs as the alloyiq_migrator role (ADR-007)."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool

from alloyiq.repositories.schema import metadata

config = context.config


def _url() -> str:
    url = os.environ.get("ALEMBIC_DATABASE_URL") or os.environ.get("DATABASE_MIGRATOR_URL")
    if not url:
        raise RuntimeError("Set ALEMBIC_DATABASE_URL (scripts/migrate.ps1 does this)")
    # Migrations use the synchronous psycopg driver; the app uses asyncpg.
    return url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=metadata,
            compare_type=True,
            compare_server_default=False,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
