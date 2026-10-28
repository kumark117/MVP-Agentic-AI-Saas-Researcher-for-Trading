"""What a workflow sees while a run executes (D026, D038, D050, D053).

RunHandle is the only way graph code touches durable state. Every write opens a short
transaction, re-checks the fencing generation (lost authority -> LostAuthority, no side
effects), and never stays open across a provider call. check() raises once cancellation,
the active deadline or lost authority is observed, so no new call can start after that.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from alloyiq.api.schemas.events import ActivityAppended, NodeStarted
from alloyiq.repositories.db import Database, DbContext
from alloyiq.repositories.events import append_run_event
from alloyiq.repositories.runs import RunRepository

_FIRST_ACTIVITY = text(
    "UPDATE research_runs SET time_to_first_activity_ms ="
    " coalesce(time_to_first_activity_ms,"
    "  (extract(epoch FROM (now() - submitted_at)) * 1000)::bigint) WHERE id = :r"
)


class LostAuthority(Exception):
    """A newer worker generation owns the run; stop without side effects."""


class RunCancelled(Exception):
    """Cancellation was observed; no new calls may start."""


class DeadlineExceeded(Exception):
    """The run's active-execution deadline passed."""


Outcome = Literal["succeeded", "incomplete", "failed", "timed_out"]


@dataclass(frozen=True, slots=True)
class RunResult:
    outcome: Outcome
    outcome_reason: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class RunContext:
    run_id: UUID
    account_id: UUID
    user_id: UUID
    generation: int
    instrument_id: UUID
    instrument_snapshot: dict[str, Any]
    horizon: str
    model_profile_id: UUID
    question: str | None
    limits: dict[str, Any]
    versions: dict[str, Any]
    approved_models: list[str]
    max_real_usd: Decimal
    started_at: datetime
    recovering: bool

    @classmethod
    def from_run(cls, run: dict[str, Any], question: str | None, recovering: bool) -> RunContext:
        limits = run["limits"] or {}
        return cls(
            run_id=run["id"],
            account_id=run["account_id"],
            user_id=run["user_id"],
            generation=run["generation"],
            instrument_id=run["instrument_id"],
            instrument_snapshot=run["instrument_snapshot"],
            horizon=run["horizon"],
            model_profile_id=run["model_profile_id"],
            question=question,
            limits=limits,
            versions=run["versions"] or {},
            approved_models=list(limits.get("approved_models", [])),
            max_real_usd=Decimal(str(limits.get("max_real_usd", "0"))),
            started_at=run["started_at"],
            recovering=recovering,
        )


@dataclass
class RunHandle:
    db: Database
    ctx: RunContext
    runs: RunRepository = field(default_factory=RunRepository)
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    deadline: float = 0.0  # time.monotonic() value
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    lost_event: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_seen_at: datetime | None = None
    _first_activity: bool = False

    # ------------------------------------------------------------------ guards
    def observe_cancel(self) -> None:
        if not self.cancel_event.is_set():
            self.cancel_seen_at = self.clock()
            self.cancel_event.set()

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def remaining_s(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def check(self) -> None:
        """Call before starting any model or tool call (D038 call intent protocol)."""
        if self.lost_event.is_set():
            raise LostAuthority()
        if self.cancel_event.is_set():
            raise RunCancelled()
        if self.deadline and time.monotonic() >= self.deadline:
            raise DeadlineExceeded()

    # ------------------------------------------------------------------ fenced writes
    @asynccontextmanager
    async def tx(self) -> AsyncIterator[AsyncConnection]:
        async with self.db.transaction(DbContext(account_id=self.ctx.account_id)) as conn:
            if await self.runs.fence(conn, self.ctx.run_id, self.ctx.generation) is None:
                self.lost_event.set()
                raise LostAuthority()
            yield conn

    async def emit(self, event_type: str, payload: BaseModel | dict[str, Any]) -> None:
        async with self.tx() as conn:
            await append_run_event(conn, self.ctx.account_id, self.ctx.run_id, event_type, payload)

    async def activity(
        self,
        agent: str | None,
        action: str,
        summary: str,
        *,
        tool_name: str | None = None,
        elapsed_ms: int | None = None,
        retry: int = 0,
        validation_status: str | None = None,
    ) -> None:
        async with self.tx() as conn:
            await append_run_event(
                conn,
                self.ctx.account_id,
                self.ctx.run_id,
                "activity.appended",
                ActivityAppended(
                    agent=agent,
                    action=action,
                    tool_name=tool_name,
                    summary=summary[:2000],
                    elapsed_ms=elapsed_ms,
                    retry=retry,
                    validation_status=validation_status,
                ),
            )
            if not self._first_activity:
                self._first_activity = True
                await conn.execute(
                    _FIRST_ACTIVITY,
                    {"r": self.ctx.run_id},
                )

    async def node_started(self, node_id: str) -> int:
        async with self.tx() as conn:
            attempt = await self.runs.node_started(
                conn, self.ctx.account_id, self.ctx.run_id, node_id, self.ctx.generation
            )
            await append_run_event(
                conn,
                self.ctx.account_id,
                self.ctx.run_id,
                "node.started",
                NodeStarted(node_id=node_id, attempt=attempt),
            )
            return attempt

    async def node_finished(
        self,
        node_id: str,
        status: str = "completed",
        *,
        output_ref: str | None = None,
        error_code: str | None = None,
    ) -> None:
        async with self.tx() as conn:
            await self.runs.node_finished(
                conn, self.ctx.run_id, node_id, status, output_ref=output_ref, error_code=error_code
            )

    async def completed_output(self, node_id: str) -> str | None:
        """On recovery, a completed node's stored output is reused, never recomputed."""
        async with self.tx() as conn:
            return await self.runs.completed_output(conn, self.ctx.run_id, node_id)


class RunExecutor(Protocol):
    """The workflow. P6 provides the LangGraph implementation."""

    async def execute(self, handle: RunHandle) -> RunResult: ...
