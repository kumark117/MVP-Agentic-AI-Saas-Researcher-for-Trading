"""RunLifecycle: the one place a run reaches a terminal state (D048-D050, D053).

finish() does the guarded CAS (status set + optional fencing generation), resolves a
cancel-vs-completion race in favour of whichever committed first, settles billing,
closes the job, and emits run.finished plus the account-level run.updated, all in the
caller's transaction.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from alloyiq.api.schemas.events import RunFinished, RunUpdated
from alloyiq.application.settlement import SettlementService
from alloyiq.repositories.audit import audit
from alloyiq.repositories.events import append_account_event, append_run_event
from alloyiq.repositories.runs import RunRepository


class RunLifecycle:
    def __init__(self, settlement: SettlementService, runs: RunRepository | None = None) -> None:
        self.settlement = settlement
        self.runs = runs or RunRepository()

    async def emit_run_updated(
        self, conn: AsyncConnection, run: dict[str, Any], request_id: str | None = None
    ) -> None:
        await append_account_event(
            conn,
            run["account_id"],
            "run.updated",
            RunUpdated(
                run_id=run["id"], status=run["status"], billing_status=run["billing_status"]
            ),
            request_id=request_id,
        )

    async def finish(
        self,
        conn: AsyncConnection,
        run_id: UUID,
        target: str,
        *,
        generation: int | None = None,
        outcome_reason: str | None = None,
        error_code: str | None = None,
        sets: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> str | None:
        """Returns the terminal status now in force, or None if this caller lost authority."""
        extra = {"outcome_reason": outcome_reason, "error_code": error_code, **(sets or {})}
        moved = await self.runs.transition(conn, run_id, target, generation=generation, sets=extra)
        if not moved:
            current = await self.runs.get_run(conn, run_id, lock=True)
            if current is None:
                return None
            if generation is not None and current["generation"] != generation:
                return None  # a newer worker owns the run: no side effects (fencing)
            if current["status"] == "cancel_requested" and target != "cancelled":
                # Cancellation committed first: execution can never overwrite it.
                extra["outcome_reason"] = "cancelled_by_user"
                moved = await self.runs.transition(
                    conn, run_id, "cancelled", generation=generation, sets=extra
                )
                target = "cancelled"
            if not moved:
                return str(current["status"])  # already terminal: nothing more to do
        run = await self.runs.get_run(conn, run_id, lock=True)
        assert run is not None  # noqa: S101 - the CAS just updated it
        await self.settlement.settle(conn, run, request_id=request_id)
        await self.runs.finish_job(conn, run_id, generation=generation)
        run = await self.runs.get_run(conn, run_id)
        assert run is not None  # noqa: S101
        await append_run_event(
            conn,
            run["account_id"],
            run_id,
            "run.finished",
            RunFinished(
                status=run["status"],
                terminal_at=run["terminal_at"],
                report_available=await self.runs.report_exists(conn, run_id),
                billing_status=run["billing_status"],
                outcome_reason=run["outcome_reason"],
            ),
            request_id=request_id,
        )
        await self.emit_run_updated(conn, run, request_id)
        await audit(
            conn,
            action="run.finished",
            outcome=target,
            account_id=run["account_id"],
            actor_kind="system",
            run_id=run_id,
            request_id=request_id,
            metadata={"outcome_reason": run["outcome_reason"], "error_code": run["error_code"]},
        )
        return target
