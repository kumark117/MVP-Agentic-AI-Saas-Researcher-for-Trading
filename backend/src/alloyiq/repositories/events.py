"""Persisted, ordered SSE events (D085, D086). Sequence numbers are allocated in the same
transaction as the business change, and NOTIFY is delivered only at COMMIT."""

from __future__ import annotations

import json
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from alloyiq.api.schemas.events import validate_payload

RETENTION_DAYS = 30  # D078: replayable SSE for 30 days
ACCOUNT_CHANNEL = "account_events"
RUN_CHANNEL = "run_events"


def event_id(scope_id: UUID, seq: int) -> str:
    return f"{scope_id}:{seq}"


async def append_account_event(
    conn: AsyncConnection,
    account_id: UUID,
    event_type: str,
    payload: BaseModel | dict[str, Any],
    *,
    request_id: str | None = None,
) -> int:
    data = validate_payload("account", event_type, payload)
    seq = (
        await conn.execute(
            text(
                "INSERT INTO account_event_cursors (account_id, last_seq) VALUES (:a, 1)"
                " ON CONFLICT (account_id) DO UPDATE"
                " SET last_seq = account_event_cursors.last_seq + 1 RETURNING last_seq"
            ),
            {"a": account_id},
        )
    ).scalar_one()
    await conn.execute(
        text(
            "INSERT INTO account_events (account_id, scope_id, seq, event_type, payload,"
            " request_id, expires_at) VALUES (:a, :a, :s, :t, CAST(:p AS jsonb), :r,"
            " now() + make_interval(days => :days))"
        ),
        {
            "a": account_id,
            "s": seq,
            "t": event_type,
            "p": json.dumps(data),
            "r": request_id,
            "days": RETENTION_DAYS,
        },
    )
    await conn.execute(
        text("SELECT pg_notify(:c, :p)"),
        {"c": ACCOUNT_CHANNEL, "p": json.dumps({"scope_id": str(account_id), "seq": seq})},
    )
    return int(seq)


async def append_run_event(
    conn: AsyncConnection,
    account_id: UUID,
    run_id: UUID,
    event_type: str,
    payload: BaseModel | dict[str, Any],
    *,
    request_id: str | None = None,
) -> int:
    """Allocates the next run sequence from research_runs.latest_event_seq (row-locked)."""
    data = validate_payload("run", event_type, payload)
    seq = (
        await conn.execute(
            text(
                "UPDATE research_runs SET latest_event_seq = latest_event_seq + 1"
                " WHERE id = :r RETURNING latest_event_seq"
            ),
            {"r": run_id},
        )
    ).scalar_one()
    await conn.execute(
        text(
            "INSERT INTO run_events (account_id, scope_id, seq, event_type, payload,"
            " request_id, expires_at) VALUES (:a, :r, :s, :t, CAST(:p AS jsonb), :q,"
            " now() + make_interval(days => :days))"
        ),
        {
            "a": account_id,
            "r": run_id,
            "s": seq,
            "t": event_type,
            "p": json.dumps(data),
            "q": request_id,
            "days": RETENTION_DAYS,
        },
    )
    await conn.execute(
        text("SELECT pg_notify(:c, :p)"),
        {"c": RUN_CHANNEL, "p": json.dumps({"scope_id": str(run_id), "seq": seq})},
    )
    return int(seq)


async def list_events(
    conn: AsyncConnection,
    scope: Literal["run", "account"],
    scope_id: UUID,
    *,
    after: int,
    limit: int = 500,
) -> list[dict[str, Any]]:
    table = "run_events" if scope == "run" else "account_events"
    rows = await conn.execute(
        text(
            f"SELECT seq, event_type, payload, request_id, occurred_at FROM {table}"  # noqa: S608
            " WHERE scope_id = :s AND seq > :after AND expires_at > now()"
            " ORDER BY seq LIMIT :n"
        ),
        {"s": scope_id, "after": after, "n": limit},
    )
    return [dict(r._mapping) for r in rows]


async def oldest_retained_seq(
    conn: AsyncConnection, scope: Literal["run", "account"], scope_id: UUID
) -> tuple[int | None, int]:
    """(min retained seq or None, max seq). Used to answer 410 for expired cursors."""
    table = "run_events" if scope == "run" else "account_events"
    row = (
        await conn.execute(
            text(
                f"SELECT min(seq) FILTER (WHERE expires_at > now()), coalesce(max(seq), 0)"  # noqa: S608
                f" FROM {table} WHERE scope_id = :s"
            ),
            {"s": scope_id},
        )
    ).one()
    return (int(row[0]) if row[0] is not None else None), int(row[1])
