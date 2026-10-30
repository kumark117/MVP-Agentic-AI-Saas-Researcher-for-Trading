"""SSE streams with durable replay (build prompt section 14; D084-D090).

Order of operations: subscribe (LISTEN) first -> read the replay high-water mark ->
replay persisted events -> drain buffered notifications -> go live. Notifications are
only wake-ups; every event is read from PostgreSQL, so delivery is at least once and
clients deduplicate by event_id. A slow client stops pulling from the generator, so its
bounded inbox (100 notifications) overflows and it gets stream.reset_required; nothing
upstream ever waits for it, and no event payloads are buffered in memory. A run stream
closes after run.finished.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text

from alloyiq.adapters.pg_listen import PgListener
from alloyiq.domain.errors import DomainError, ErrorCode, not_found
from alloyiq.repositories.db import Database, DbContext
from alloyiq.repositories.events import (
    ACCOUNT_CHANNEL,
    RUN_CHANNEL,
    event_id,
    list_events,
    oldest_retained_seq,
)

Scope = Literal["run", "account"]
HEARTBEAT_S = 15.0
SESSION_CHECK_S = 10.0
POLL_S = 2.0
MAX_BUFFER_EVENTS = 100
# A stream ends after this long and the client reconnects from its Last-Event-ID, missing
# nothing. Behind a proxy that keeps the upstream open after the browser leaves (Vercel
# rewrites), the API never sees the disconnect; without a cap such streams would hold the
# member's 3-stream limit forever and every new page would get 429 (DV-13).
MAX_STREAM_S = 60.0


def parse_cursor(raw: str | None, scope_id: UUID) -> int:
    """'{scope_uuid}:{seq}' -> seq. A cursor from another stream or malformed is 400."""
    if raw is None or raw == "":
        return 0
    try:
        scope, seq_s = raw.rsplit(":", 1)
        seq = int(seq_s)
        if UUID(scope) != scope_id or seq < 0:
            raise ValueError
    except ValueError as exc:
        raise DomainError(
            400,
            ErrorCode.VALIDATION_FAILED,
            "Invalid event cursor",
            "The Last-Event-ID or after cursor does not belong to this stream.",
        ) from exc
    return seq


def frame(scope_id: UUID, row: dict[str, Any], run_id: UUID | None) -> str:
    env = {
        "schema_version": "1.0",
        "event_id": event_id(scope_id, row["seq"]),
        "sequence": row["seq"],
        "run_id": str(run_id) if run_id else None,
        "occurred_at": row["occurred_at"].astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "request_id": row.get("request_id"),
        "payload": row["payload"],
    }
    data = json.dumps(env, separators=(",", ":"))
    return f"id: {env['event_id']}\nevent: {row['event_type']}\ndata: {data}\n\n"


def reset_frame(scope_id: UUID, last_seq: int, reason: str) -> str:
    payload = {
        "reason": reason,
        "latest_event_id": event_id(scope_id, last_seq) if last_seq else None,
    }
    env = {
        "schema_version": "1.0",
        "event_id": event_id(scope_id, last_seq),
        "sequence": max(last_seq, 1),
        "occurred_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "payload": payload,
    }
    return f"event: stream.reset_required\ndata: {json.dumps(env, separators=(',', ':'))}\n\n"


@dataclass
class StreamPlan:
    scope: Scope
    scope_id: UUID
    account_id: UUID
    after: int
    terminal_at_open: bool


class StreamService:
    def __init__(
        self,
        db: Database,
        listener_factory: Callable[[], Awaitable[PgListener | None]],
        *,
        heartbeat_s: float = HEARTBEAT_S,
        session_check_s: float = SESSION_CHECK_S,
        poll_s: float = POLL_S,
        max_stream_s: float = MAX_STREAM_S,
    ) -> None:
        self.db = db
        self.listener_factory = listener_factory
        self.heartbeat_s = heartbeat_s
        self.session_check_s = session_check_s
        self.poll_s = poll_s
        self.max_stream_s = max_stream_s
        self._open: dict[UUID, int] = defaultdict(int)  # per-account concurrent streams

    async def prepare(
        self, *, scope: Scope, scope_id: UUID, account_id: UUID, cursor: str | None
    ) -> StreamPlan:
        """All checks that must answer before the stream opens (400/404/410/429)."""
        after = parse_cursor(cursor, scope_id)
        async with self.db.transaction(DbContext(account_id=account_id)) as conn:
            terminal = False
            if scope == "run":
                row = (
                    await conn.execute(
                        text("SELECT status FROM research_runs WHERE id = :r"), {"r": scope_id}
                    )
                ).first()
                if row is None:
                    raise not_found("Run")
                terminal = row.status in (
                    "succeeded",
                    "incomplete",
                    "failed",
                    "cancelled",
                    "timed_out",
                )
            oldest, latest = await oldest_retained_seq(conn, scope, scope_id)
            limit = (
                await conn.execute(
                    text(
                        "SELECT p.sse_streams FROM accounts a JOIN plans p ON p.id = a.plan_id"
                        " WHERE a.id = :a"
                    ),
                    {"a": account_id},
                )
            ).scalar() or 3
        if after > latest:
            raise DomainError(
                400,
                ErrorCode.VALIDATION_FAILED,
                "Invalid event cursor",
                "The cursor is ahead of this stream.",
            )
        expired = (oldest is None and latest > 0 and after < latest) or (
            oldest is not None and after < oldest - 1
        )
        if after and expired:
            raise DomainError(
                410,
                ErrorCode.EVENT_CURSOR_EXPIRED,
                "Event cursor expired",
                "Those events are past retention. Reload the snapshot and reopen from its "
                "latest_event_id.",
            )
        if self._open[account_id] >= int(limit):
            raise DomainError(
                429,
                ErrorCode.RATE_LIMITED,
                "Too many live streams",
                f"At most {limit} live connections per member. Close another tab or device.",
                headers={"Retry-After": "5"},
            )
        return StreamPlan(scope, scope_id, account_id, after, terminal)

    async def stream(
        self,
        plan: StreamPlan,
        session_active: Callable[[], Awaitable[bool]],
    ) -> AsyncIterator[str]:
        self._open[plan.account_id] += 1
        listener = await self.listener_factory()
        channel = RUN_CHANNEL if plan.scope == "run" else ACCOUNT_CHANNEL
        sub = (
            listener.subscribe(channel, str(plan.scope_id), maxsize=MAX_BUFFER_EVENTS)
            if listener
            else None
        )
        run_id = plan.scope_id if plan.scope == "run" else None
        last = plan.after
        try:
            yield ": connected\n\n"
            next_beat = time.monotonic() + self.heartbeat_s
            next_check = time.monotonic() + self.session_check_s
            end_at = time.monotonic() + self.max_stream_s
            while True:
                if sub is not None and sub.overflowed:
                    # The client fell >100 notifications behind: tell it to resync from a
                    # snapshot rather than buffering for it (D090).
                    yield reset_frame(plan.scope_id, last, "buffer_overflow")
                    return
                # Replay / catch up from the durable log (also the live path).
                finished = False
                while True:
                    async with self.db.transaction(DbContext(account_id=plan.account_id)) as conn:
                        rows = await list_events(
                            conn, plan.scope, plan.scope_id, after=last, limit=MAX_BUFFER_EVENTS
                        )
                    for row in rows:
                        f = frame(plan.scope_id, row, run_id)
                        last = row["seq"]
                        yield f
                        if plan.scope == "run" and row["event_type"] == "run.finished":
                            finished = True
                    if len(rows) < MAX_BUFFER_EVENTS or finished:
                        break
                if finished or (plan.terminal_at_open and plan.scope == "run"):
                    return  # D090: close after run.finished once replay is done
                now = time.monotonic()
                if now >= end_at:
                    return  # lifetime cap: the client resumes from its Last-Event-ID
                if now >= next_check:
                    next_check = now + self.session_check_s
                    if not await session_active():
                        return  # revoked or expired session: close (R012)
                if now >= next_beat:
                    next_beat = now + self.heartbeat_s
                    yield ": heartbeat\n\n"
                wait = max(0.05, min(next_beat, next_check, end_at) - time.monotonic())
                wait = min(wait, self.poll_s)
                if sub is not None:
                    await sub.get(wait)
                else:
                    await asyncio.sleep(wait)
        finally:
            if sub is not None:
                sub.close()
            self._open[plan.account_id] -= 1
