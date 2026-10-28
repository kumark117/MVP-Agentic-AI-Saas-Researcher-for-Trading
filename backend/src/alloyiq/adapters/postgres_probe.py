"""Readiness probe that proves PostgreSQL accepts connections and queries.

Phase 1 replaces the per-call connection with the shared SQLAlchemy engine.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import asyncpg


def _asyncpg_dsn(sqlalchemy_url: str) -> str:
    # The app-wide URL uses the SQLAlchemy dialect form postgresql+asyncpg://.
    return sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def postgres_probe(
    database_url: str | None, timeout_s: float = 2.0
) -> Callable[[], Awaitable[None]]:
    async def probe() -> None:
        if not database_url:
            raise RuntimeError("DATABASE_URL is not configured")
        async with asyncio.timeout(timeout_s):
            conn = await asyncpg.connect(_asyncpg_dsn(database_url))
            try:
                await conn.fetchval("SELECT 1")
            finally:
                await conn.close()

    return probe
