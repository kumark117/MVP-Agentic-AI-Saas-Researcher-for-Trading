"""Database access and the unit of work that carries the RLS context.

Every transaction sets app.account_id, app.user_id and app.system with
set_config(..., is_local => true), the function form of SET LOCAL. The values vanish
at COMMIT/ROLLBACK, so a pooled connection can never carry one account's context into
the next transaction (tests/integration/test_rls.py proves it).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

_SET_CONTEXT = text(
    "SELECT set_config('app.account_id', :account_id, true),"
    " set_config('app.user_id', :user_id, true),"
    " set_config('app.system', :system, true)"
)


@dataclass(frozen=True, slots=True)
class DbContext:
    """Who a transaction acts for. Built from AccountContext, never from client input."""

    account_id: UUID | None = None
    user_id: UUID | None = None
    system: bool = False  # honoured by the database only for the worker/migrator roles

    @classmethod
    def anonymous(cls) -> DbContext:
        return cls()

    @classmethod
    def for_system(cls) -> DbContext:
        return cls(system=True)


class Database:
    def __init__(self, url: str, *, pool_size: int = 10, max_overflow: int = 10) -> None:
        self.engine: AsyncEngine = create_async_engine(
            url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=True,
            pool_recycle=1800,
        )

    @asynccontextmanager
    async def transaction(self, ctx: DbContext) -> AsyncIterator[AsyncConnection]:
        async with self.engine.begin() as conn:
            await conn.execute(
                _SET_CONTEXT,
                {
                    "account_id": str(ctx.account_id) if ctx.account_id else "",
                    "user_id": str(ctx.user_id) if ctx.user_id else "",
                    "system": "on" if ctx.system else "",
                },
            )
            yield conn

    async def dispose(self) -> None:
        await self.engine.dispose()
