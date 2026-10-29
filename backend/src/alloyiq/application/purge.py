# ruff: noqa: E501 - SQL kept on readable single lines
"""PurgeService: retention classes and account deletion (R054, R055; D078, D079).

Runs in the worker, in system scope, hourly. Retention (ADR-011):
- sse_30d: run/account events past expires_at (30 days).
- ephemeral_7d: idempotency records past expiry, published outbox rows, ephemeral
  artifacts (exports) older than 7 days.
- auth_short: consumed or expired auth tokens, sessions ended more than 30 days ago.
- activity_90d: tool calls, validation verdicts, evidence, run nodes, artifacts > 90 days.
Account deletion (scheduled 7 days after the request, completed within the 30-day window):
sessions revoked, queued work cancelled, conversations, messages, feedback, reports,
evidence, activity and exports deleted, the user pseudonymised, and a tombstone written
per subject so a restore can re-apply the deletion. Demo ledger, usage and provider-cost
records stay under their own retention (product_12m / financial_policy) with only
pseudonymous references (D079).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from alloyiq.application.lifecycle import RunLifecycle
from alloyiq.observability.logging import get_logger
from alloyiq.repositories.audit import audit
from alloyiq.repositories.db import Database, DbContext

log = get_logger(__name__)

RETENTION_SQL: list[tuple[str, str]] = [
    ("run_events", "DELETE FROM run_events WHERE expires_at < now()"),
    ("account_events", "DELETE FROM account_events WHERE expires_at < now()"),
    ("idempotency_records", "DELETE FROM idempotency_records WHERE expires_at < now()"),
    ("outbox", "DELETE FROM outbox WHERE created_at < now() - interval '7 days'"),
    (
        "auth_tokens",
        "DELETE FROM auth_tokens WHERE expires_at < now() - interval '1 day'"
        " OR consumed_at < now() - interval '1 day'",
    ),
    (
        "sessions",
        "DELETE FROM sessions WHERE absolute_expires_at < now() - interval '30 days'"
        " OR revoked_at < now() - interval '30 days'",
    ),
    (
        "tool_calls",
        "DELETE FROM tool_calls WHERE started_at < now() - interval '90 days'",
    ),
    (
        "validation_results",
        "DELETE FROM validation_results WHERE created_at < now() - interval '90 days'",
    ),
    ("run_nodes", "DELETE FROM run_nodes WHERE started_at < now() - interval '90 days'"),
]


@dataclass
class PurgeReport:
    deleted: dict[str, int] = field(default_factory=dict)
    accounts_deleted: int = 0
    artifacts_removed: int = 0


class PurgeService:
    def __init__(self, db: Database, lifecycle: RunLifecycle, artifacts: Any) -> None:
        self.db = db
        self.lifecycle = lifecycle
        self.artifacts = artifacts

    async def run_once(self) -> PurgeReport:
        report = PurgeReport()
        async with self.db.transaction(DbContext.for_system()) as conn:
            for name, sql in RETENTION_SQL:
                report.deleted[name] = (await conn.execute(text(sql))).rowcount
            # Evidence older than 90 days: drop citations first (reports keep their copy).
            await conn.execute(
                text(
                    "DELETE FROM report_citations WHERE evidence_id IN (SELECT id FROM evidence_items"
                    " WHERE created_at < now() - interval '90 days')"
                )
            )
            report.deleted["evidence_items"] = (
                await conn.execute(
                    text("DELETE FROM evidence_items WHERE created_at < now() - interval '90 days'")
                )
            ).rowcount
            report.artifacts_removed = await self._expire_artifacts(conn)
        async with self.db.transaction(DbContext.for_system()) as conn:
            due = (
                await conn.execute(
                    text(
                        "UPDATE deletion_requests SET status = 'processing' WHERE id IN ("
                        " SELECT id FROM deletion_requests WHERE status = 'scheduled'"
                        " AND scheduled_at <= now() ORDER BY scheduled_at LIMIT 5"
                        " FOR UPDATE SKIP LOCKED) RETURNING id, account_id, user_id"
                    )
                )
            ).all()
        for req in due:
            await self.delete_account(req.id, req.account_id, req.user_id)
            report.accounts_deleted += 1
        if any(report.deleted.values()) or report.accounts_deleted or report.artifacts_removed:
            log.info(
                "purge",
                **report.deleted,
                accounts_deleted=report.accounts_deleted,
                artifacts_removed=report.artifacts_removed,
            )
        return report

    async def _expire_artifacts(self, conn: AsyncConnection) -> int:
        rows = (
            await conn.execute(
                text(
                    "UPDATE artifacts SET deleted_at = now(), content = NULL WHERE deleted_at IS NULL AND ("
                    " (retention_class = 'ephemeral_7d' AND created_at < now() - interval '7 days')"
                    " OR (retention_class = 'activity_90d' AND created_at < now() - interval '90 days'))"
                    " RETURNING sha256"
                )
            )
        ).all()
        await conn.execute(
            text(
                "UPDATE export_jobs SET status = 'expired' WHERE status = 'ready'"
                " AND expires_at < now()"
            )
        )
        removed = 0
        for r in rows:
            still_used = (
                await conn.execute(
                    text("SELECT 1 FROM artifacts WHERE sha256 = :s AND deleted_at IS NULL"),
                    {"s": r.sha256},
                )
            ).first()
            if not still_used and hasattr(self.artifacts, "remove"):
                removed += await self.artifacts.remove(r.sha256)
        return removed

    async def delete_account(self, request_id: UUID, account_id: UUID, user_id: UUID) -> None:
        async with self.db.transaction(DbContext.for_system()) as conn:
            # Stop future work first: queued runs are cancelled with no model work.
            queued = (
                (
                    await conn.execute(
                        text(
                            "SELECT id FROM research_runs WHERE account_id = :a AND status = 'queued'"
                        ),
                        {"a": account_id},
                    )
                )
                .scalars()
                .all()
            )
            for run_id in queued:
                await conn.execute(
                    text(
                        "UPDATE research_runs SET status = 'cancel_requested',"
                        " cancel_requested_at = now(), cancel_reason = 'account_deleted'"
                        " WHERE id = :r AND status = 'queued'"
                    ),
                    {"r": run_id},
                )
                await self.lifecycle.runs.finish_job(conn, run_id)
                await self.lifecycle.finish(
                    conn, run_id, "cancelled", outcome_reason="account_deleted"
                )
            await conn.execute(
                text(
                    "UPDATE sessions SET revoked_at = now(), revoked_reason = 'account_deleted'"
                    " WHERE user_id = :u AND revoked_at IS NULL"
                ),
                {"u": user_id},
            )
            counts: dict[str, int] = {}
            for table, sql in [
                ("report_citations", "DELETE FROM report_citations WHERE account_id = :a"),
                ("reports", "DELETE FROM reports WHERE account_id = :a"),
                ("evidence_items", "DELETE FROM evidence_items WHERE account_id = :a"),
                ("feedback", "DELETE FROM feedback WHERE account_id = :a"),
                ("messages", "DELETE FROM messages WHERE account_id = :a"),
                (
                    "research_runs.conversation",
                    "UPDATE research_runs SET conversation_id = NULL,"
                    " question_message_id = NULL WHERE account_id = :a",
                ),
                ("conversations", "DELETE FROM conversations WHERE account_id = :a"),
                ("tool_calls", "DELETE FROM tool_calls WHERE account_id = :a"),
                ("validation_results", "DELETE FROM validation_results WHERE account_id = :a"),
                ("run_nodes", "DELETE FROM run_nodes WHERE account_id = :a"),
                ("run_events", "DELETE FROM run_events WHERE account_id = :a"),
                ("account_events", "DELETE FROM account_events WHERE account_id = :a"),
                ("export_jobs", "UPDATE export_jobs SET status = 'expired' WHERE account_id = :a"),
                (
                    "artifacts",
                    "UPDATE artifacts SET deleted_at = now(), content = NULL WHERE account_id = :a"
                    " AND deleted_at IS NULL",
                ),
                ("email_outbox", "DELETE FROM email_outbox WHERE account_id = :a"),
            ]:
                counts[table] = (await conn.execute(text(sql), {"a": account_id})).rowcount
            pseudonym = hashlib.sha256(f"{user_id}".encode()).hexdigest()[:16]
            await conn.execute(
                text(
                    "UPDATE users SET email = :e, password_hash = NULL, display_name = 'Deleted user',"
                    " status = 'deleted', deleted_at = now() WHERE id = :u"
                ),
                {"u": user_id, "e": f"deleted+{pseudonym}@deleted.invalid"},
            )
            await conn.execute(
                text("UPDATE accounts SET status = 'deleted' WHERE id = :a"), {"a": account_id}
            )
            for table, n in counts.items():
                if n:
                    await conn.execute(
                        text(
                            "INSERT INTO tombstones (account_id, subject_table, subject_id, reason)"
                            " VALUES (:a, :t, NULL, :r)"
                        ),
                        {"a": account_id, "t": table, "r": f"account_deletion:{request_id}"},
                    )
            await conn.execute(
                text(
                    "INSERT INTO tombstones (account_id, subject_table, subject_id, reason)"
                    " VALUES (:a, 'users', :u, :r)"
                ),
                {"a": account_id, "u": user_id, "r": f"account_deletion:{request_id}"},
            )
            await conn.execute(
                text(
                    "UPDATE deletion_requests SET status = 'completed', completed_at = now()"
                    " WHERE id = :i"
                ),
                {"i": request_id},
            )
            await audit(
                conn,
                action="account.deleted",
                outcome="completed",
                account_id=account_id,
                actor_kind="system",
                target_type="account",
                target_id=str(account_id),
                metadata={
                    "request_id": str(request_id),
                    "deleted": {k: v for k, v in counts.items() if v},
                    "trace_note": "third-party trace deletion is deferred (DV-10)",
                },
            )
