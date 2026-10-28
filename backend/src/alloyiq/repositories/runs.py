"""RunRepository: estimates, runs, jobs, nodes and the outbox (D026, D048-D053, D059-D060).

State changes are compare-and-swap UPDATEs guarded by the expected status set and, for
worker writes, the fencing generation. Zero rows updated means the caller lost the race
(or its authority) and must not perform side effects.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from alloyiq.domain.runs import sources_for

LEASE_SECONDS = 30
JOBS_CHANNEL = "jobs"
CANCEL_CHANNEL = "run_cancel"


def _d(row: Any) -> dict[str, Any] | None:
    return dict(row._mapping) if row is not None else None


class RunRepository:
    # ------------------------------------------------------------------ estimates
    async def insert_estimate(
        self, conn: AsyncConnection, values: dict[str, Any]
    ) -> dict[str, Any]:
        row = (
            await conn.execute(
                text(
                    "INSERT INTO estimates (account_id, user_id, normalized_request,"
                    " request_fingerprint, instrument_id, horizon, model_profile_id,"
                    " approved_models, limits, rate_card_version, min_charge, estimated_charge,"
                    " max_charge, max_real_usd, question, conversation_id, message_cutoff_id,"
                    " expires_at) VALUES (:account_id, :user_id,"
                    " CAST(:normalized_request AS jsonb),"
                    " :request_fingerprint, :instrument_id, :horizon, :model_profile_id,"
                    " :approved_models, CAST(:limits AS jsonb), :rate_card_version, :min_charge,"
                    " :estimated_charge, :max_charge, :max_real_usd, :question, :conversation_id,"
                    " :message_cutoff_id, :expires_at) RETURNING *"
                ),
                {
                    **values,
                    "normalized_request": json.dumps(values["normalized_request"]),
                    "limits": json.dumps(values["limits"]),
                },
            )
        ).one()
        return dict(row._mapping)

    async def get_estimate(
        self, conn: AsyncConnection, estimate_id: UUID, *, lock: bool = False
    ) -> dict[str, Any] | None:
        row = (
            await conn.execute(
                text("SELECT * FROM estimates WHERE id = :i" + (" FOR UPDATE" if lock else "")),  # noqa: S608
                {"i": estimate_id},
            )
        ).first()
        return _d(row)

    async def consume_estimate(
        self, conn: AsyncConnection, estimate_id: UUID, run_id: UUID
    ) -> bool:
        r = await conn.execute(
            text(
                "UPDATE estimates SET consumed_by_run_id = :r, consumed_at = now()"
                " WHERE id = :i AND consumed_by_run_id IS NULL"
            ),
            {"i": estimate_id, "r": run_id},
        )
        return r.rowcount == 1

    # ------------------------------------------------------------------ plan limits
    async def plan_limits(self, conn: AsyncConnection, account_id: UUID) -> dict[str, Any]:
        row = (
            await conn.execute(
                text(
                    "SELECT a.status AS account_status, p.active_runs, p.queued_runs, p.sse_streams"
                    " FROM accounts a JOIN plans p ON p.id = a.plan_id WHERE a.id = :a"
                ),
                {"a": account_id},
            )
        ).one()
        return dict(row._mapping)

    async def count_open(self, conn: AsyncConnection, account_id: UUID) -> tuple[int, int]:
        row = (
            await conn.execute(
                text(
                    "SELECT count(*) FILTER (WHERE status IN ('running','cancel_requested')),"
                    " count(*) FILTER (WHERE status = 'queued') FROM research_runs"
                    " WHERE account_id = :a AND status IN ('queued','running','cancel_requested')"
                ),
                {"a": account_id},
            )
        ).one()
        return int(row[0]), int(row[1])

    # ------------------------------------------------------------------ runs
    async def insert_run(self, conn: AsyncConnection, values: dict[str, Any]) -> None:
        await conn.execute(
            text(
                "INSERT INTO research_runs (id, account_id, user_id, conversation_id, estimate_id,"
                " instrument_id, instrument_snapshot, horizon, model_profile_id,"
                " question_message_id, versions, limits, authorized_max_charge, reserved_amount)"
                " VALUES (:id, :account_id, :user_id, :conversation_id, :estimate_id,"
                " :instrument_id, CAST(:instrument_snapshot AS jsonb), :horizon,"
                " :model_profile_id, :question_message_id, CAST(:versions AS jsonb),"
                " CAST(:limits AS jsonb), :authorized_max_charge, :reserved_amount)"
            ),
            {
                **values,
                "instrument_snapshot": json.dumps(values["instrument_snapshot"]),
                "versions": json.dumps(values["versions"]),
                "limits": json.dumps(values["limits"]),
            },
        )

    async def get_run(
        self, conn: AsyncConnection, run_id: UUID, *, lock: bool = False
    ) -> dict[str, Any] | None:
        row = (
            await conn.execute(
                text("SELECT * FROM research_runs WHERE id = :i" + (" FOR UPDATE" if lock else "")),  # noqa: S608
                {"i": run_id},
            )
        ).first()
        return _d(row)

    async def list_runs(
        self,
        conn: AsyncConnection,
        account_id: UUID,
        *,
        status: str | None,
        instrument_id: UUID | None,
        model_profile_id: UUID | None,
        start: datetime | None,
        end: datetime | None,
        after: tuple[datetime, UUID] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows = await conn.execute(
            text(
                "SELECT r.*, rep.signal FROM research_runs r"
                " LEFT JOIN reports rep ON rep.run_id = r.id"
                " WHERE r.account_id = :a"
                "  AND (CAST(:st AS text) IS NULL OR r.status = :st)"
                "  AND (CAST(:ins AS uuid) IS NULL OR r.instrument_id = :ins)"
                "  AND (CAST(:mp AS uuid) IS NULL OR r.model_profile_id = :mp)"
                "  AND (CAST(:s AS timestamptz) IS NULL OR r.submitted_at >= :s)"
                "  AND (CAST(:e AS timestamptz) IS NULL OR r.submitted_at < :e)"
                "  AND (CAST(:cts AS timestamptz) IS NULL OR (r.submitted_at, r.id) < (:cts, :cid))"
                " ORDER BY r.submitted_at DESC, r.id DESC LIMIT :n"
            ),
            {
                "a": account_id,
                "st": status,
                "ins": instrument_id,
                "mp": model_profile_id,
                "s": start,
                "e": end,
                "cts": after[0] if after else None,
                "cid": after[1] if after else None,
                "n": limit,
            },
        )
        return [dict(r._mapping) for r in rows]

    async def transition(
        self,
        conn: AsyncConnection,
        run_id: UUID,
        target: str,
        *,
        generation: int | None = None,
        sets: dict[str, Any] | None = None,
    ) -> bool:
        """Guarded CAS: WHERE status IN (legal sources) [AND generation = :g]."""
        sources = sources_for(target)
        if not sources:
            return False
        sets = dict(sets or {})
        terminal = target in ("succeeded", "incomplete", "failed", "cancelled", "timed_out")
        assignments = ["status = :target"]
        if terminal:
            assignments.append("terminal_at = coalesce(terminal_at, now())")
        for col in sets:
            assignments.append(f"{col} = :{col}")
        sql = (
            f"UPDATE research_runs SET {', '.join(assignments)}"  # noqa: S608 - column names are code
            " WHERE id = :id AND status = ANY(:sources)"
            + (" AND generation = :generation" if generation is not None else "")
        )
        r = await conn.execute(
            text(sql),
            {
                **sets,
                "target": target,
                "id": run_id,
                "sources": list(sources),
                "generation": generation,
            },
        )
        return r.rowcount == 1

    async def update_run(self, conn: AsyncConnection, run_id: UUID, sets: dict[str, Any]) -> None:
        assignments = ", ".join(f"{c} = :{c}" for c in sets)
        await conn.execute(
            text(f"UPDATE research_runs SET {assignments} WHERE id = :id"),  # noqa: S608
            {**sets, "id": run_id},
        )

    async def fence(
        self, conn: AsyncConnection, run_id: UUID, generation: int
    ) -> dict[str, Any] | None:
        """Lock the run if this worker still holds authority (generation matches)."""
        row = (
            await conn.execute(
                text("SELECT * FROM research_runs WHERE id = :i AND generation = :g FOR UPDATE"),
                {"i": run_id, "g": generation},
            )
        ).first()
        return _d(row)

    # ------------------------------------------------------------------ conversations
    async def create_conversation(
        self, conn: AsyncConnection, account_id: UUID, user_id: UUID, title: str | None
    ) -> UUID:
        cid: UUID = (
            await conn.execute(
                text(
                    "INSERT INTO conversations (account_id, user_id, title) VALUES (:a, :u, :t)"
                    " RETURNING id"
                ),
                {"a": account_id, "u": user_id, "t": title},
            )
        ).scalar_one()
        return cid

    async def conversation_exists(self, conn: AsyncConnection, conversation_id: UUID) -> bool:
        return (
            await conn.execute(
                text("SELECT 1 FROM conversations WHERE id = :c"), {"c": conversation_id}
            )
        ).first() is not None

    async def latest_message_id(self, conn: AsyncConnection, conversation_id: UUID) -> UUID | None:
        mid: UUID | None = (
            await conn.execute(
                text(
                    "SELECT id FROM messages WHERE conversation_id = :c"
                    " ORDER BY created_at DESC, id DESC LIMIT 1"
                ),
                {"c": conversation_id},
            )
        ).scalar()
        return mid

    async def insert_message(
        self,
        conn: AsyncConnection,
        *,
        account_id: UUID,
        conversation_id: UUID,
        user_id: UUID | None,
        role: str,
        content: str,
        run_id: UUID | None,
        client_message_id: UUID | None = None,
    ) -> UUID:
        mid: UUID = (
            await conn.execute(
                text(
                    "INSERT INTO messages (account_id, conversation_id, user_id, role, content,"
                    " run_id, client_message_id) VALUES (:a, :c, :u, :r, :t, :run, :cm)"
                    " RETURNING id"
                ),
                {
                    "a": account_id,
                    "c": conversation_id,
                    "u": user_id,
                    "r": role,
                    "t": content,
                    "run": run_id,
                    "cm": client_message_id,
                },
            )
        ).scalar_one()
        await conn.execute(
            text("UPDATE conversations SET updated_at = now() WHERE id = :c"),
            {"c": conversation_id},
        )
        return mid

    # ------------------------------------------------------------------ jobs and outbox
    async def insert_job(self, conn: AsyncConnection, account_id: UUID, run_id: UUID) -> None:
        await conn.execute(
            text("INSERT INTO jobs (account_id, run_id) VALUES (:a, :r)"),
            {"a": account_id, "r": run_id},
        )
        await conn.execute(text("SELECT pg_notify(:c, :p)"), {"c": JOBS_CHANNEL, "p": str(run_id)})

    async def outbox(
        self, conn: AsyncConnection, account_id: UUID | None, topic: str, payload: dict[str, Any]
    ) -> None:
        await conn.execute(
            text(
                "INSERT INTO outbox (account_id, topic, payload) VALUES (:a, :t, CAST(:p AS jsonb))"
            ),
            {"a": account_id, "t": topic, "p": json.dumps(payload, default=str)},
        )

    async def claim_job(
        self, conn: AsyncConnection, worker_id: str, *, max_active: int = 2
    ) -> dict[str, Any] | None:
        """System scope. Oldest first, accounts with fewer active runs first, and never more
        than the plan's active-run limit per account (D026)."""
        row = (
            await conn.execute(
                text(
                    "WITH active AS (SELECT account_id, count(*) AS n FROM research_runs"  # noqa: S608
                    "  WHERE status IN ('running','cancel_requested') GROUP BY account_id),"
                    " pick AS (SELECT j.id FROM jobs j LEFT JOIN active a"
                    "  ON a.account_id = j.account_id"
                    "  WHERE j.state = 'runnable' AND j.available_at <= now()"
                    "   AND coalesce(a.n, 0) < :max_active"
                    "  ORDER BY coalesce(a.n, 0), j.priority DESC, j.available_at"
                    "  FOR UPDATE OF j SKIP LOCKED LIMIT 1)"
                    " UPDATE jobs SET state = 'leased', generation = generation + 1,"
                    "  attempts = attempts + 1, lease_owner = :w, updated_at = now(),"
                    f"  lease_expires_at = now() + interval '{LEASE_SECONDS} seconds'"
                    " FROM pick WHERE jobs.id = pick.id"
                    " RETURNING jobs.id, jobs.run_id, jobs.account_id, jobs.generation,"
                    "  jobs.attempts"
                ),
                {"w": worker_id, "max_active": max_active},
            )
        ).first()
        if row is None:
            return None
        job = dict(row._mapping)
        await conn.execute(
            text("UPDATE research_runs SET generation = :g WHERE id = :r"),
            {"g": job["generation"], "r": job["run_id"]},
        )
        await conn.execute(
            text(
                "INSERT INTO job_attempts (account_id, job_id, generation, worker_id)"
                " VALUES (:a, :j, :g, :w)"
            ),
            {"a": job["account_id"], "j": job["id"], "g": job["generation"], "w": worker_id},
        )
        return job

    async def heartbeat(self, conn: AsyncConnection, job_id: UUID, generation: int) -> bool:
        r = await conn.execute(
            text(
                "UPDATE jobs SET updated_at = now(),"  # noqa: S608
                f" lease_expires_at = now() + interval '{LEASE_SECONDS} seconds'"
                " WHERE id = :j AND generation = :g AND state = 'leased'"
            ),
            {"j": job_id, "g": generation},
        )
        return r.rowcount == 1

    async def finish_job(
        self,
        conn: AsyncConnection,
        run_id: UUID,
        *,
        state: str = "done",
        generation: int | None = None,
        error: str | None = None,
    ) -> bool:
        r = await conn.execute(
            text(
                "UPDATE jobs SET state = :s, lease_owner = NULL, lease_expires_at = NULL,"  # noqa: S608
                " last_error = coalesce(:e, last_error), updated_at = now()"
                " WHERE run_id = :r AND state IN ('runnable','leased')"
                + (" AND generation = :g" if generation is not None else "")
            ),
            {"r": run_id, "s": state, "e": error, "g": generation},
        )
        await conn.execute(
            text(
                "UPDATE job_attempts SET ended_at = now(), outcome = :s, error = :e"
                " WHERE job_id = (SELECT id FROM jobs WHERE run_id = :r) AND ended_at IS NULL"
            ),
            {"r": run_id, "s": state, "e": error},
        )
        return r.rowcount == 1

    async def expired_leases(self, conn: AsyncConnection, limit: int = 50) -> list[dict[str, Any]]:
        rows = await conn.execute(
            text(
                "SELECT id, run_id, account_id, generation, attempts FROM jobs"
                " WHERE state = 'leased' AND lease_expires_at < now()"
                " ORDER BY lease_expires_at LIMIT :n FOR UPDATE SKIP LOCKED"
            ),
            {"n": limit},
        )
        return [dict(r._mapping) for r in rows]

    async def requeue(self, conn: AsyncConnection, job_id: UUID, generation: int) -> bool:
        r = await conn.execute(
            text(
                "UPDATE jobs SET state = 'runnable', lease_owner = NULL, lease_expires_at = NULL,"
                " available_at = now(), updated_at = now()"
                " WHERE id = :j AND generation = :g AND state = 'leased'"
            ),
            {"j": job_id, "g": generation},
        )
        await conn.execute(
            text(
                "UPDATE job_attempts SET ended_at = now(), outcome = 'lease_expired'"
                " WHERE job_id = :j AND generation = :g AND ended_at IS NULL"
            ),
            {"j": job_id, "g": generation},
        )
        if r.rowcount == 1:
            await conn.execute(text("SELECT pg_notify('jobs', :p)"), {"p": str(job_id)})
        return r.rowcount == 1

    async def stale_queued(
        self, conn: AsyncConnection, max_age_s: int, limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = await conn.execute(
            text(
                "SELECT r.id, r.account_id FROM research_runs r JOIN jobs j ON j.run_id = r.id"
                " WHERE r.status = 'queued' AND j.state = 'runnable'"
                " AND r.submitted_at < now() - make_interval(secs => :s)"
                " ORDER BY r.submitted_at LIMIT :n"
            ),
            {"s": max_age_s, "n": limit},
        )
        return [dict(r._mapping) for r in rows]

    async def incident(
        self,
        conn: AsyncConnection,
        account_id: UUID | None,
        kind: str,
        run_id: UUID | None,
        details: dict[str, Any],
    ) -> None:
        await conn.execute(
            text(
                "INSERT INTO incidents (account_id, kind, run_id, details)"
                " VALUES (:a, :k, :r, CAST(:d AS jsonb))"
            ),
            {"a": account_id, "k": kind, "r": run_id, "d": json.dumps(details, default=str)},
        )

    # ------------------------------------------------------------------ nodes and progress
    async def node_started(
        self, conn: AsyncConnection, account_id: UUID, run_id: UUID, node_id: str, generation: int
    ) -> int:
        attempt: int = (
            await conn.execute(
                text(
                    "INSERT INTO run_nodes (account_id, run_id, node_id, status, attempt,"
                    " generation)"
                    " VALUES (:a, :r, :n, 'running', (SELECT coalesce(max(attempt), 0) + 1"
                    "  FROM run_nodes WHERE run_id = :r AND node_id = :n), :g) RETURNING attempt"
                ),
                {"a": account_id, "r": run_id, "n": node_id, "g": generation},
            )
        ).scalar_one()
        return attempt

    async def node_finished(
        self,
        conn: AsyncConnection,
        run_id: UUID,
        node_id: str,
        status: str,
        *,
        output_ref: str | None = None,
        error_code: str | None = None,
    ) -> None:
        await conn.execute(
            text(
                "UPDATE run_nodes SET status = :s, ended_at = now(), output_ref = :o,"
                " error_code = :e WHERE run_id = :r AND node_id = :n AND status = 'running'"
            ),
            {"r": run_id, "n": node_id, "s": status, "o": output_ref, "e": error_code},
        )

    async def completed_output(
        self, conn: AsyncConnection, run_id: UUID, node_id: str
    ) -> str | None:
        ref: str | None = (
            await conn.execute(
                text(
                    "SELECT output_ref FROM run_nodes WHERE run_id = :r AND node_id = :n"
                    " AND status = 'completed' ORDER BY attempt DESC LIMIT 1"
                ),
                {"r": run_id, "n": node_id},
            )
        ).scalar()
        return ref

    async def progress(self, conn: AsyncConnection, run_id: UUID) -> tuple[int, list[str]]:
        rows = (
            await conn.execute(
                text(
                    "SELECT DISTINCT ON (node_id) node_id, status FROM run_nodes WHERE run_id = :r"
                    " ORDER BY node_id, attempt DESC"
                ),
                {"r": run_id},
            )
        ).all()
        done = sum(1 for r in rows if r.status in ("completed", "skipped"))
        return done, sorted(r.node_id for r in rows if r.status == "running")

    async def report_exists(self, conn: AsyncConnection, run_id: UUID) -> bool:
        return (
            await conn.execute(text("SELECT 1 FROM reports WHERE run_id = :r"), {"r": run_id})
        ).first() is not None

    async def usage_totals(self, conn: AsyncConnection, run_id: UUID) -> tuple[Decimal, bool]:
        """(sum of simulated charges of billable calls, any unknown usage) for settlement."""
        row = (
            await conn.execute(
                text(
                    "SELECT coalesce(sum(u.customer_charge_inr_simulated)"
                    " FILTER (WHERE u.billable), 0), bool_or(u.usage_status = 'unknown')"
                    " FROM usage_records u"
                    " WHERE u.run_id = :r"
                ),
                {"r": run_id},
            )
        ).one()
        unknown_calls = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM llm_calls c LEFT JOIN usage_records u"
                    " ON u.llm_call_id = c.id WHERE c.run_id = :r AND u.id IS NULL"
                    " AND c.status IN ('intent','in_flight','ambiguous')"
                ),
                {"r": run_id},
            )
        ).scalar_one()
        return Decimal(row[0]), bool(row[1]) or unknown_calls > 0
