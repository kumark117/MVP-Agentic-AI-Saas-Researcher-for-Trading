"""Idempotency-Key records (D083), used inside the caller's business transaction.

claim() inserts the key or, if it already exists, waits for the row (the unique index
blocks a concurrent duplicate until the first transaction commits or rolls back) and
returns the stored outcome. The business writes and the saved response commit together,
so a lost HTTP response replays the committed result instead of acting twice.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from alloyiq.domain.errors import DomainError, ErrorCode

RETENTION_DAYS = 7


def body_fingerprint(path: str, body: Any) -> str:
    canonical = json.dumps(
        {"path": path, "body": body}, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Replay:
    status: int
    body: dict[str, Any]


def conflict() -> DomainError:
    return DomainError(
        409,
        ErrorCode.IDEMPOTENCY_CONFLICT,
        "Idempotency key reused",
        "This Idempotency-Key was already used with a different request. Use a new key.",
    )


async def claim(
    conn: AsyncConnection,
    *,
    account_id: UUID,
    method: str,
    route: str,
    key: UUID,
    fingerprint: str,
) -> Replay | None:
    """None: this request owns the key and must call save(). Otherwise the saved replay."""
    inserted = (
        await conn.execute(
            text(
                "INSERT INTO idempotency_records (account_id, method, route, key, body_hash,"
                " expires_at) VALUES (:a, :m, :r, :k, :h, now() + make_interval(days => :days))"
                " ON CONFLICT (account_id, method, route, key) DO NOTHING RETURNING id"
            ),
            {
                "a": account_id,
                "m": method,
                "r": route,
                "k": key,
                "h": fingerprint,
                "days": RETENTION_DAYS,
            },
        )
    ).first()
    if inserted:
        return None
    row = (
        await conn.execute(
            text(
                "SELECT body_hash, response_status, response_body FROM idempotency_records"
                " WHERE account_id = :a AND method = :m AND route = :r AND key = :k FOR UPDATE"
            ),
            {"a": account_id, "m": method, "r": route, "k": key},
        )
    ).one()
    if row.body_hash != fingerprint:
        raise conflict()
    if row.response_status is None:  # pragma: no cover - the owner always saves before commit
        raise conflict()
    return Replay(int(row.response_status), dict(row.response_body))


async def save(
    conn: AsyncConnection,
    *,
    account_id: UUID,
    method: str,
    route: str,
    key: UUID,
    status: int,
    body: dict[str, Any],
    resource_id: UUID | None = None,
) -> None:
    await conn.execute(
        text(
            "UPDATE idempotency_records SET response_status = :s,"
            " response_body = CAST(:b AS jsonb), resource_id = :res"
            " WHERE account_id = :a AND method = :m AND route = :r AND key = :k"
        ),
        {
            "a": account_id,
            "m": method,
            "r": route,
            "k": key,
            "s": status,
            "b": json.dumps(body, default=str),
            "res": resource_id,
        },
    )
