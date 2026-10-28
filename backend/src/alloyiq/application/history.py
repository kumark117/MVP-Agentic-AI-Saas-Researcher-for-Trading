# ruff: noqa: E501 - SQL kept on readable single lines
"""Conversations, immutable messages and feedback (R033, R034; D100-D103).

- Messages are immutable and at most 4,000 characters; client_message_id deduplicates
  retries within a conversation. Posting a message never starts research.
- A follow-up is a new estimate on the same conversation, allowed only after the
  conversation's previous run is terminal (checked in ResearchService.estimate).
- One editable rating per user and run.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text

from alloyiq.api.schemas.history import Conversation, Message
from alloyiq.api.schemas.research import Feedback
from alloyiq.domain.errors import DomainError, ErrorCode, not_found
from alloyiq.domain.identity import AccountContext
from alloyiq.repositories.db import Database, DbContext
from alloyiq.repositories.paging import decode_cursor, encode_cursor
from alloyiq.repositories.runs import RunRepository


def _ctx(ctx: AccountContext) -> DbContext:
    return DbContext(account_id=ctx.account_id, user_id=ctx.user_id)


def _conv(r: Any) -> Conversation:
    return Conversation(
        conversation_id=r.id, title=r.title, created_at=r.created_at, updated_at=r.updated_at
    )


def _msg(r: Any) -> Message:
    return Message(
        message_id=r.id,
        conversation_id=r.conversation_id,
        role=r.role,
        content=r.content,
        run_id=r.run_id,
        created_at=r.created_at,
    )


class HistoryService:
    def __init__(self, db: Database, runs: RunRepository | None = None) -> None:
        self.db = db
        self.runs = runs or RunRepository()

    async def create_conversation(self, ctx: AccountContext, title: str | None) -> Conversation:
        async with self.db.transaction(_ctx(ctx)) as conn:
            cid = await self.runs.create_conversation(conn, ctx.account_id, ctx.user_id, title)
            row = (
                await conn.execute(text("SELECT * FROM conversations WHERE id = :c"), {"c": cid})
            ).one()
        return _conv(row)

    async def list_conversations(
        self, ctx: AccountContext, *, cursor: str | None, limit: int
    ) -> tuple[list[Conversation], str | None]:
        after = decode_cursor(cursor)
        async with self.db.transaction(_ctx(ctx)) as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT * FROM conversations WHERE account_id = :a"
                        " AND (CAST(:cts AS timestamptz) IS NULL OR (updated_at, id) < (:cts, :cid))"
                        " ORDER BY updated_at DESC, id DESC LIMIT :n"
                    ),
                    {
                        "a": ctx.account_id,
                        "cts": after[0] if after else None,
                        "cid": after[1] if after else None,
                        "n": limit + 1,
                    },
                )
            ).all()
        more = len(rows) > limit
        rows = rows[:limit]
        nxt = encode_cursor(rows[-1].updated_at, rows[-1].id) if more and rows else None
        return [_conv(r) for r in rows], nxt

    async def _own(self, conn: Any, conversation_id: UUID) -> None:
        if not await self.runs.conversation_exists(conn, conversation_id):
            raise not_found("Conversation")

    async def messages(
        self, ctx: AccountContext, conversation_id: UUID, *, cursor: str | None, limit: int
    ) -> tuple[list[Message], str | None]:
        after = decode_cursor(cursor)
        async with self.db.transaction(_ctx(ctx)) as conn:
            await self._own(conn, conversation_id)
            rows = (
                await conn.execute(
                    text(
                        "SELECT * FROM messages WHERE conversation_id = :c"
                        " AND (CAST(:cts AS timestamptz) IS NULL OR (created_at, id) > (:cts, :cid))"
                        " ORDER BY created_at, id LIMIT :n"
                    ),
                    {
                        "c": conversation_id,
                        "cts": after[0] if after else None,
                        "cid": after[1] if after else None,
                        "n": limit + 1,
                    },
                )
            ).all()
        more = len(rows) > limit
        rows = rows[:limit]
        nxt = encode_cursor(rows[-1].created_at, rows[-1].id) if more and rows else None
        return [_msg(r) for r in rows], nxt

    async def post_message(
        self, ctx: AccountContext, conversation_id: UUID, content: str, client_message_id: UUID
    ) -> tuple[bool, Message]:
        """(created, message). A repeated client_message_id returns the original."""
        async with self.db.transaction(_ctx(ctx)) as conn:
            await self._own(conn, conversation_id)
            existing = (
                await conn.execute(
                    text(
                        "SELECT * FROM messages WHERE conversation_id = :c AND client_message_id = :m"
                    ),
                    {"c": conversation_id, "m": client_message_id},
                )
            ).first()
            if existing:
                if existing.content != content:
                    raise DomainError(
                        409,
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "Message ID reused",
                        "This client_message_id was already used with different content.",
                    )
                return False, _msg(existing)
            mid = await self.runs.insert_message(
                conn,
                account_id=ctx.account_id,
                conversation_id=conversation_id,
                user_id=ctx.user_id,
                role="user",
                content=content,
                run_id=None,
                client_message_id=client_message_id,
            )
            row = (
                await conn.execute(text("SELECT * FROM messages WHERE id = :m"), {"m": mid})
            ).one()
        return True, _msg(row)

    async def set_feedback(
        self, ctx: AccountContext, run_id: UUID, rating: str, comment: str | None
    ) -> tuple[bool, Feedback]:
        async with self.db.transaction(_ctx(ctx)) as conn:
            run = await self.runs.get_run(conn, run_id)
            if run is None:
                raise not_found("Run")
            if run["terminal_at"] is None:
                raise DomainError(
                    409,
                    ErrorCode.INVALID_RUN_STATE,
                    "Run still in progress",
                    "Feedback opens when the run has finished.",
                )
            row = (
                await conn.execute(
                    text(
                        "INSERT INTO feedback (account_id, run_id, user_id, rating, comment)"
                        " VALUES (:a, :r, :u, :rating, :c) ON CONFLICT (run_id, user_id) DO UPDATE"
                        " SET rating = EXCLUDED.rating, comment = EXCLUDED.comment, updated_at = now()"
                        " RETURNING id, run_id, rating, comment, updated_at, (xmax = 0) AS created"
                    ),
                    {
                        "a": ctx.account_id,
                        "r": run_id,
                        "u": ctx.user_id,
                        "rating": rating,
                        "c": comment,
                    },
                )
            ).one()
        return bool(row.created), Feedback(
            feedback_id=row.id,
            run_id=row.run_id,
            rating=row.rating,
            comment=row.comment,
            updated_at=row.updated_at,
        )
