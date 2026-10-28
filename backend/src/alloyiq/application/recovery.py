"""RecoverySweeper (build prompt section 8.4; D053), run by the worker every 15 s.

- A leased job whose lease expired goes back to runnable with a `recovering` activity
  event; the public status stays running. The next claim bumps the generation, so the
  stale worker's writes are fenced out.
- After MAX_ATTEMPTS claims the job is dead: an incident is written and the run fails.
- A queued run older than 5 minutes times out without any model work; its reservations
  are released.
"""

from __future__ import annotations

from dataclasses import dataclass

from alloyiq.application.lifecycle import RunLifecycle
from alloyiq.observability.logging import get_logger
from alloyiq.repositories.db import Database, DbContext
from alloyiq.repositories.events import append_run_event
from alloyiq.repositories.runs import RunRepository

MAX_ATTEMPTS = 3
QUEUE_EXPIRY_S = 300
log = get_logger(__name__)


@dataclass
class SweepResult:
    requeued: int = 0
    dead: int = 0
    queue_expired: int = 0


class RecoverySweeper:
    def __init__(
        self,
        db: Database,
        lifecycle: RunLifecycle,
        *,
        runs: RunRepository | None = None,
        queue_expiry_s: int = QUEUE_EXPIRY_S,
    ) -> None:
        self.db = db
        self.lifecycle = lifecycle
        self.runs = runs or RunRepository()
        self.queue_expiry_s = queue_expiry_s

    async def sweep(self) -> SweepResult:
        res = SweepResult()
        async with self.db.transaction(DbContext.for_system()) as conn:
            for job in await self.runs.expired_leases(conn):
                if job["attempts"] >= MAX_ATTEMPTS:
                    await self.runs.finish_job(
                        conn, job["run_id"], state="dead", error="lease expired too often"
                    )
                    await self.runs.incident(
                        conn,
                        job["account_id"],
                        "job_dead",
                        job["run_id"],
                        {"attempts": job["attempts"], "generation": job["generation"]},
                    )
                    await self.lifecycle.finish(
                        conn,
                        job["run_id"],
                        "failed",
                        outcome_reason="worker_retries_exhausted",
                        error_code="INTERNAL_ERROR",
                    )
                    res.dead += 1
                elif await self.runs.requeue(conn, job["id"], job["generation"]):
                    await append_run_event(
                        conn,
                        job["account_id"],
                        job["run_id"],
                        "activity.appended",
                        {
                            "agent": None,
                            "action": "recovering",
                            "summary": "The worker running this job stopped responding; "
                            "the run will resume on another worker.",
                            "retry": job["attempts"],
                        },
                    )
                    res.requeued += 1
        async with self.db.transaction(DbContext.for_system()) as conn:
            stale = await self.runs.stale_queued(conn, self.queue_expiry_s)
        for run in stale:
            async with self.db.transaction(DbContext(account_id=run["account_id"])) as conn:
                status = await self.lifecycle.finish(
                    conn, run["id"], "timed_out", outcome_reason="queue_expired"
                )
                if status == "timed_out":
                    res.queue_expired += 1
        if res.requeued or res.dead or res.queue_expired:
            log.info("recovery_sweep", **res.__dict__)
        return res
