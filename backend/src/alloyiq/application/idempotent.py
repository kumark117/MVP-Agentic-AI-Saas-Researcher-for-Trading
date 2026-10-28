"""Run a mutation at most once per Idempotency-Key, in one transaction with its effects (D083)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncConnection

from alloyiq.repositories import idempotency


async def once[M: BaseModel](
    conn: AsyncConnection,
    *,
    account_id: UUID,
    method: str,
    route: str,
    path: str,
    key: UUID,
    request: Any,
    model: type[M],
    status: int,
    action: Callable[[], Awaitable[M]],
    resource_id: Callable[[M], UUID | None] = lambda _: None,
) -> tuple[int, M]:
    """Returns (status, response). A replay returns the stored response unchanged."""
    fp = idempotency.body_fingerprint(path, request)
    replay = await idempotency.claim(
        conn, account_id=account_id, method=method, route=route, key=key, fingerprint=fp
    )
    if replay is not None:
        return replay.status, model.model_validate(replay.body)
    result = await action()
    await idempotency.save(
        conn,
        account_id=account_id,
        method=method,
        route=route,
        key=key,
        status=status,
        body=result.model_dump(mode="json"),
        resource_id=resource_id(result),
    )
    return status, result
