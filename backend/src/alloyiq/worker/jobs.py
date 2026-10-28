"""JobRunner: claim durable jobs, hold a fenced lease, execute the workflow, and finish the
run exactly once (build prompt sections 8.4, 8.6; D026, D050, D053).

- Claim in a short system-scope transaction (SKIP LOCKED); lease 30 s, heartbeat 10 s.
  A failed heartbeat means another worker may take over: stop work, no side effects.
- A cancel watcher (LISTEN run_cancel plus a 1 s poll) stops new calls the moment the
  flag is seen and cancels the workflow task; both stop latencies are recorded (R026).
- The active deadline (limits.run_active_seconds, default 120 s) ends the run timed_out.
- No database transaction is held open while the workflow runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from alloyiq.adapters.pg_listen import PgListener
from alloyiq.api.schemas.events import RunStarted
from alloyiq.application.lifecycle import RunLifecycle
from alloyiq.domain.runs import TERMINAL
from alloyiq.observability.logging import get_logger
from alloyiq.repositories.db import Database, DbContext
from alloyiq.repositories.events import append_run_event
from alloyiq.repositories.runs import CANCEL_CHANNEL, RunRepository
from alloyiq.repositories.usage import UsageRepository
from alloyiq.workflows.runner import (
    DeadlineExceeded,
    LostAuthority,
    RunCancelled,
    RunContext,
    RunExecutor,
    RunHandle,
    RunResult,
)

log = get_logger(__name__)


def _ms_between(a: datetime | None, b: datetime | None) -> int | None:
    if a is None or b is None:
        return None
    return max(0, int((b - a).total_seconds() * 1000))


class JobRunner:
    def __init__(
        self,
        db: Database,
        executor: RunExecutor,
        lifecycle: RunLifecycle,
        *,
        worker_id: str,
        listener: PgListener | None = None,
        concurrency: int = 2,
        heartbeat_s: float = 10.0,
        cancel_poll_s: float = 1.0,
        runs: RunRepository | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.db = db
        self.executor = executor
        self.lifecycle = lifecycle
        self.worker_id = worker_id
        self.listener = listener
        self.concurrency = concurrency
        self.heartbeat_s = heartbeat_s
        self.cancel_poll_s = cancel_poll_s
        self.runs = runs or RunRepository()
        self.now = clock
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ loop
    async def run_forever(self, stop: asyncio.Event, poll_s: float = 2.0) -> None:
        wake = self.listener.subscribe("jobs", maxsize=1000) if self.listener else None
        try:
            while not stop.is_set():
                while len(self._tasks) < self.concurrency and await self.claim_and_start():
                    pass
                if wake is not None:
                    await wake.get(poll_s)
                else:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), poll_s)
        finally:
            if wake is not None:
                wake.close()
            for t in list(self._tasks):
                t.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def claim_and_start(self) -> bool:
        async with self.db.transaction(DbContext.for_system()) as conn:
            job = await self.runs.claim_job(conn, self.worker_id)
        if job is None:
            return False
        task = asyncio.create_task(self.process(job), name=f"run-{job['run_id']}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def run_one(self) -> bool:
        """Claim and fully process one job (tests and simple loops)."""
        async with self.db.transaction(DbContext.for_system()) as conn:
            job = await self.runs.claim_job(conn, self.worker_id)
        if job is None:
            return False
        await self.process(job)
        return True

    # ------------------------------------------------------------------ one job
    async def process(self, job: dict[str, Any]) -> None:
        run_id, gen = job["run_id"], job["generation"]
        try:
            handle = await self._start(job)
            if handle is None:
                return
            result, latencies = await self._execute(job, handle)
            if result is None:
                return  # lost authority: the new owner finishes the run
            target = "cancelled" if handle.cancelled else result.outcome
            async with self.db.transaction(DbContext(account_id=job["account_id"])) as conn:
                await self.lifecycle.finish(
                    conn,
                    run_id,
                    target,
                    generation=gen,
                    outcome_reason=(
                        "cancelled_by_user" if handle.cancelled else result.outcome_reason
                    ),
                    error_code=None if handle.cancelled else result.error_code,
                    sets=latencies,
                )
        except LostAuthority:
            log.info("run_lost_authority", run_id=str(run_id), generation=gen)
        except Exception:
            log.exception("run_processing_failed", run_id=str(run_id))
            with contextlib.suppress(Exception):
                async with self.db.transaction(DbContext(account_id=job["account_id"])) as conn:
                    await self.lifecycle.finish(
                        conn,
                        run_id,
                        "failed",
                        generation=gen,
                        outcome_reason="worker_error",
                        error_code="INTERNAL_ERROR",
                    )

    async def _start(self, job: dict[str, Any]) -> RunHandle | None:
        run_id, gen = job["run_id"], job["generation"]
        async with self.db.transaction(DbContext(account_id=job["account_id"])) as conn:
            run = await self.runs.fence(conn, run_id, gen)
            if run is None:
                return None
            if run["status"] in TERMINAL:
                await self.runs.finish_job(conn, run_id, generation=gen)
                return None
            if run["status"] == "cancel_requested" and run["started_at"] is None:
                await self.lifecycle.finish(
                    conn,
                    run_id,
                    "cancelled",
                    generation=gen,
                    outcome_reason="cancelled_before_start",
                    sets={"cancel_stop_calls_ms": 0, "cancel_stop_branches_ms": 0},
                )
                return None
            recovering = run["started_at"] is not None
            if run["status"] == "queued":
                now = self.now()
                if not await self.runs.transition(
                    conn, run_id, "running", generation=gen, sets={"started_at": now}
                ):
                    return None
                run = await self.runs.get_run(conn, run_id)
                assert run is not None  # noqa: S101
                await append_run_event(
                    conn,
                    run["account_id"],
                    run_id,
                    "run.started",
                    RunStarted(
                        started_at=now,
                        queue_ms=_ms_between(run["submitted_at"], now) or 0,
                        recovering=False,
                    ),
                )
                await self.lifecycle.emit_run_updated(conn, run)
            else:
                # D038: an intent with no completion is ambiguous, never blindly retried.
                await UsageRepository().mark_ambiguous(
                    conn, run_id, (run["versions"] or {}).get("rate_card", "")
                )
                await append_run_event(
                    conn,
                    run["account_id"],
                    run_id,
                    "activity.appended",
                    {
                        "agent": None,
                        "action": "recovering",
                        "summary": "A worker resumed this run after an interruption. "
                        "Completed steps are reused, not repeated.",
                        "retry": job["attempts"] - 1,
                    },
                )
            question = None
            if run["question_message_id"]:
                question = (
                    await conn.execute(
                        text("SELECT content FROM messages WHERE id = :m"),
                        {"m": run["question_message_id"]},
                    )
                ).scalar()
        ctx = RunContext.from_run(run, question, recovering)
        active_s = float((ctx.limits or {}).get("run_active_seconds", 120))
        elapsed = (self.now() - ctx.started_at).total_seconds() if ctx.started_at else 0.0
        handle = RunHandle(self.db, ctx, self.runs, self.now)
        handle.deadline = time.monotonic() + max(0.0, active_s - elapsed)
        if run["status"] == "cancel_requested":
            handle.observe_cancel()
        return handle

    async def _execute(
        self, job: dict[str, Any], handle: RunHandle
    ) -> tuple[RunResult | None, dict[str, Any]]:
        exec_task = asyncio.create_task(self.executor.execute(handle))
        stop = asyncio.Event()
        hb = asyncio.create_task(self._heartbeat(job, handle, exec_task, stop))
        watch = asyncio.create_task(self._watch_cancel(job, handle, exec_task, stop))
        latencies: dict[str, Any] = {}
        result: RunResult | None
        try:
            result = await asyncio.wait_for(asyncio.shield(exec_task), timeout=handle.remaining_s())
        except TimeoutError:
            exec_task.cancel()
            with contextlib.suppress(BaseException):
                await exec_task
            result = RunResult("timed_out", "active_deadline_exceeded", None)
        except asyncio.CancelledError:
            if not exec_task.cancelled():
                exec_task.cancel()  # this worker is stopping: abandon the workflow too
                raise
            result = None
        except (RunCancelled, DeadlineExceeded) as exc:
            result = RunResult(
                "timed_out" if isinstance(exc, DeadlineExceeded) else "failed",
                "active_deadline_exceeded" if isinstance(exc, DeadlineExceeded) else None,
            )
        except LostAuthority:
            result = None
        finally:
            stop.set()
            for t in (hb, watch):
                t.cancel()
            await asyncio.gather(hb, watch, return_exceptions=True)
        if handle.lost_event.is_set():
            return None, latencies
        if handle.cancelled:
            async with self.db.transaction(DbContext(account_id=job["account_id"])) as conn:
                run = await self.runs.get_run(conn, job["run_id"])
            requested = run["cancel_requested_at"] if run else None
            latencies = {
                "cancel_stop_calls_ms": _ms_between(requested, handle.cancel_seen_at),
                "cancel_stop_branches_ms": _ms_between(requested, self.now()),
            }
            return result or RunResult("failed"), latencies
        if result is None:
            # The workflow task was cancelled without a cancel request (e.g. shutdown).
            return None, latencies
        return result, latencies

    async def _heartbeat(
        self,
        job: dict[str, Any],
        handle: RunHandle,
        exec_task: asyncio.Task[RunResult],
        stop: asyncio.Event,
    ) -> None:
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self.heartbeat_s)
            if stop.is_set():
                return
            try:
                async with self.db.transaction(DbContext.for_system()) as conn:
                    ok = await self.runs.heartbeat(conn, job["id"], job["generation"])
            except Exception:
                ok = False
            if not ok:
                log.warning("lease_lost", run_id=str(job["run_id"]))
                handle.lost_event.set()
                exec_task.cancel()
                return

    async def _watch_cancel(
        self,
        job: dict[str, Any],
        handle: RunHandle,
        exec_task: asyncio.Task[RunResult],
        stop: asyncio.Event,
    ) -> None:
        sub = (
            self.listener.subscribe(CANCEL_CHANNEL, str(job["run_id"]), maxsize=10)
            if self.listener
            else None
        )
        try:
            while not stop.is_set() and not handle.cancelled:
                if sub is not None:
                    await sub.get(self.cancel_poll_s)
                else:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), self.cancel_poll_s)
                async with self.db.transaction(DbContext(account_id=job["account_id"])) as conn:
                    flagged = (
                        await conn.execute(
                            text("SELECT cancel_requested_at FROM research_runs WHERE id = :r"),
                            {"r": job["run_id"]},
                        )
                    ).scalar()
                if flagged is not None:
                    handle.observe_cancel()  # from here, check() refuses new calls
                    exec_task.cancel()  # cancels sibling branch tasks and open streams
                    return
        finally:
            if sub is not None:
                sub.close()
