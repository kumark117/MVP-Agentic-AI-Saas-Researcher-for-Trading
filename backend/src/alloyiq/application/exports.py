# ruff: noqa: E501 - SQL kept on readable single lines
"""ExportService: asynchronous CSV/JSON exports of the member's own data (R034, R035).

- POST /exports (Idempotency-Key) queues a job; the worker builds the file in the
  member's RLS context, stores it in the ArtifactStore and emits export.ready.
- Downloads need the session cookie of the same account *and* a signed link that expires
  within 15 minutes. A pending account deletion invalidates every link.
- CSV cells are neutralised against formula injection. Exports never include other
  accounts' data, secrets, prompts or hidden model reasoning.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text

from alloyiq.adapters.artifacts import ArtifactStore
from alloyiq.api.schemas.events import ExportReady
from alloyiq.api.schemas.history import ExportJob
from alloyiq.application.idempotent import once
from alloyiq.domain.errors import DomainError, ErrorCode, not_found
from alloyiq.domain.identity import AccountContext
from alloyiq.observability.logging import get_logger
from alloyiq.repositories.audit import audit
from alloyiq.repositories.db import Database, DbContext
from alloyiq.repositories.events import append_account_event
from alloyiq.security.csv_safe import to_csv

LINK_TTL = timedelta(minutes=15)
MAX_RANGE = timedelta(days=366)
MAX_ROWS = 50_000
log = get_logger(__name__)

QUERIES: dict[str, tuple[list[str], str]] = {
    "runs": (
        [
            "run_id",
            "submitted_at",
            "terminal_at",
            "status",
            "billing_status",
            "instrument",
            "horizon",
            "signal",
            "settled_charge_inr_simulated",
            "outcome_reason",
        ],
        "SELECT r.id, r.submitted_at, r.terminal_at, r.status, r.billing_status,"
        " r.instrument_snapshot->>'canonical_id', r.horizon, rep.signal, r.settled_charge,"
        " r.outcome_reason FROM research_runs r LEFT JOIN reports rep ON rep.run_id = r.id"
        " WHERE r.account_id = :a AND r.submitted_at >= :s AND r.submitted_at < :e"
        " ORDER BY r.submitted_at",
    ),
    "usage": (
        [
            "call_id",
            "run_id",
            "started_at",
            "node_id",
            "purpose",
            "model_requested",
            "model_used",
            "usage_status",
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "provider_cost_usd",
            "cost_status",
            "customer_charge_inr_simulated",
            "billable",
        ],
        "SELECT c.id, c.run_id, c.started_at, c.node_id, c.purpose, c.model_requested,"
        " c.model_used, coalesce(u.usage_status, 'unknown'), u.input_tokens, u.output_tokens,"
        " u.cache_read_input_tokens, u.cache_creation_input_tokens, u.provider_cost_usd,"
        " coalesce(u.cost_status, 'unknown'), u.customer_charge_inr_simulated, c.billable"
        " FROM llm_calls c LEFT JOIN usage_records u ON u.llm_call_id = c.id"
        " WHERE c.account_id = :a AND c.started_at >= :s AND c.started_at < :e ORDER BY c.started_at",
    ),
    "account_data": (
        ["section", "id", "at", "kind", "detail", "amount_inr_simulated"],
        "SELECT 'ledger', t.id, t.posted_at, t.kind, t.description, p.amount"
        " FROM ledger_transactions t JOIN ledger_postings p ON p.transaction_id = t.id"
        "  AND p.ledger_account = CAST(:w AS text)"
        " WHERE t.account_id = :a AND t.posted_at >= :s AND t.posted_at < :e"
        " UNION ALL SELECT 'message', m.id, m.created_at, m.role, m.content, NULL FROM messages m"
        " WHERE m.account_id = :a AND m.created_at >= :s AND m.created_at < :e"
        " UNION ALL SELECT 'feedback', f.id, f.updated_at, f.rating, f.comment, NULL FROM feedback f"
        " WHERE f.account_id = :a AND f.updated_at >= :s AND f.updated_at < :e"
        " ORDER BY 3",
    ),
}


class ExportService:
    def __init__(
        self,
        db: Database,
        artifacts: ArtifactStore,
        *,
        secret: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.db = db
        self.artifacts = artifacts
        self.secret = secret.encode()
        self.now = clock

    # ------------------------------------------------------------------ signed links
    def _sig(self, export_id: UUID, account_id: UUID, expires: int) -> str:
        msg = f"export:{export_id}:{account_id}:{expires}".encode()
        return hmac.new(self.secret, msg, hashlib.sha256).hexdigest()

    def _link(self, export_id: UUID, account_id: UUID) -> tuple[str, datetime]:
        exp = self.now() + LINK_TTL
        ts = int(exp.timestamp())
        return (
            f"/api/v1/exports/{export_id}/download?expires={ts}&sig={self._sig(export_id, account_id, ts)}",
            exp,
        )

    def _view(self, row: Any) -> ExportJob:
        url, exp = (None, None)
        if row.status == "ready":
            url, exp = self._link(row.id, row.account_id)
        return ExportJob(
            export_id=row.id,
            kind=row.kind,
            format=row.format,
            status=row.status,
            status_url=f"/api/v1/exports/{row.id}",
            download_url=url,
            download_expires_at=exp,
            row_count=row.row_count,
            created_at=row.created_at,
        )

    # ------------------------------------------------------------------ API
    async def create(
        self,
        ctx: AccountContext,
        *,
        kind: str,
        fmt: str,
        start: datetime,
        end: datetime,
        key: UUID,
        request_id: str | None,
    ) -> tuple[int, ExportJob]:
        if end <= start or end - start > MAX_RANGE:
            raise DomainError(
                422,
                ErrorCode.VALIDATION_FAILED,
                "Invalid range",
                "Choose a range of at most 366 days with 'to' after 'from'.",
                field_errors=[("to", "invalid range")],
            )
        body = {"kind": kind, "format": fmt, "from": start.isoformat(), "to": end.isoformat()}
        async with self.db.transaction(
            DbContext(account_id=ctx.account_id, user_id=ctx.user_id)
        ) as conn:

            async def make() -> ExportJob:
                row = (
                    await conn.execute(
                        text(
                            "INSERT INTO export_jobs (account_id, user_id, kind, format, from_ts, to_ts)"
                            " VALUES (:a, :u, :k, :f, :s, :e) RETURNING *"
                        ),
                        {
                            "a": ctx.account_id,
                            "u": ctx.user_id,
                            "k": kind,
                            "f": fmt,
                            "s": start,
                            "e": end,
                        },
                    )
                ).one()
                await conn.execute(text("SELECT pg_notify('exports', :i)"), {"i": str(row.id)})
                await audit(
                    conn,
                    action="export.requested",
                    outcome="queued",
                    account_id=ctx.account_id,
                    actor_user_id=ctx.user_id,
                    target_type="export",
                    target_id=str(row.id),
                    request_id=request_id,
                    metadata={"kind": kind, "format": fmt},
                )
                return self._view(row)

            return await once(
                conn,
                account_id=ctx.account_id,
                method="POST",
                route="/exports",
                path="/exports",
                key=key,
                request=body,
                model=ExportJob,
                status=202,
                action=make,
                resource_id=lambda j: j.export_id,
            )

    async def get(self, ctx: AccountContext, export_id: UUID) -> ExportJob:
        async with self.db.transaction(DbContext(account_id=ctx.account_id)) as conn:
            row = (
                await conn.execute(
                    text("SELECT * FROM export_jobs WHERE id = :i"), {"i": export_id}
                )
            ).first()
        if row is None:
            raise not_found("Export")
        return self._view(row)

    async def download(
        self, ctx: AccountContext, export_id: UUID, expires: int, sig: str
    ) -> tuple[bytes, str, str]:
        gone = DomainError(
            410,
            ErrorCode.RESOURCE_NOT_FOUND,
            "Link expired",
            "This download link has expired. Open the export again for a new link.",
        )
        if expires < int(self.now().timestamp()):
            raise gone
        if not hmac.compare_digest(sig, self._sig(export_id, ctx.account_id, expires)):
            raise not_found("Export")  # a link for another account is indistinguishable
        async with self.db.transaction(DbContext(account_id=ctx.account_id)) as conn:
            row = (
                await conn.execute(
                    text("SELECT * FROM export_jobs WHERE id = :i"), {"i": export_id}
                )
            ).first()
            if row is None:
                raise not_found("Export")
            deleting = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM deletion_requests WHERE account_id = :a AND status = 'scheduled'"
                    ),
                    {"a": ctx.account_id},
                )
            ).first()
            if deleting or row.status != "ready" or not row.artifact_key:
                raise gone
            data = await self.artifacts.get(conn, row.artifact_key)
            await audit(
                conn,
                action="export.downloaded",
                outcome="ok",
                account_id=ctx.account_id,
                actor_user_id=ctx.user_id,
                target_type="export",
                target_id=str(export_id),
            )
        if data is None:
            raise gone
        ctype = "text/csv; charset=utf-8" if row.format == "csv" else "application/json"
        name = f"alloyiq-{row.kind}-{row.created_at:%Y%m%d}.{row.format}"
        return data, ctype, name

    # ------------------------------------------------------------------ worker
    async def process_pending(self, batch: int = 5) -> int:
        async with self.db.transaction(DbContext.for_system()) as conn:
            jobs = (
                await conn.execute(
                    text(
                        "UPDATE export_jobs SET status = 'running' WHERE id IN (SELECT id FROM export_jobs"
                        " WHERE status = 'queued' ORDER BY created_at LIMIT :n FOR UPDATE SKIP LOCKED)"
                        " RETURNING id, account_id"
                    ),
                    {"n": batch},
                )
            ).all()
        for job in jobs:
            try:
                await self._build(job.id, job.account_id)
            except Exception as exc:
                log.exception("export_failed", export_id=str(job.id))
                async with self.db.transaction(DbContext(account_id=job.account_id)) as conn:
                    await conn.execute(
                        text(
                            "UPDATE export_jobs SET status = 'failed', error = :e, completed_at = now()"
                            " WHERE id = :i"
                        ),
                        {"i": job.id, "e": type(exc).__name__},
                    )
                    await append_account_event(
                        conn,
                        job.account_id,
                        "export.ready",
                        ExportReady(export_id=job.id, status="failed"),
                    )
        return len(jobs)

    async def _build(self, export_id: UUID, account_id: UUID) -> None:
        async with self.db.transaction(DbContext(account_id=account_id)) as conn:
            job = (
                await conn.execute(
                    text("SELECT * FROM export_jobs WHERE id = :i"), {"i": export_id}
                )
            ).one()
            header, sql = QUERIES[job.kind]
            rows = (
                await conn.execute(
                    text(sql + f" LIMIT {MAX_ROWS}"),
                    {
                        "a": account_id,
                        "w": f"wallet:{account_id}",
                        "s": job.from_ts,
                        "e": job.to_ts,
                    },
                )
            ).all()
            data = _encode(job.format, header, list(rows))
            key = f"export:{export_id}"
            await self.artifacts.put(
                conn,
                account_id=account_id,
                key=key,
                data=data,
                content_type="text/csv" if job.format == "csv" else "application/json",
                retention_class="ephemeral_7d",
            )
            await conn.execute(
                text(
                    "UPDATE export_jobs SET status = 'ready', artifact_key = :k, artifact_sha256 = :h,"
                    " row_count = :n, completed_at = now(), expires_at = now() + interval '7 days'"
                    " WHERE id = :i"
                ),
                {"i": export_id, "k": key, "h": hashlib.sha256(data).hexdigest(), "n": len(rows)},
            )
            await append_account_event(
                conn, account_id, "export.ready", ExportReady(export_id=export_id, status="ready")
            )


def _encode(fmt: str, header: list[str], rows: list[Any]) -> bytes:
    if fmt == "csv":
        return to_csv(header, [tuple(r) for r in rows])
    return json.dumps(
        {
            "columns": header,
            "rows": [dict(zip(header, r, strict=True)) for r in rows],
            "note": "Demo amounts are simulated; provider costs are real USD (calculated).",
        },
        default=str,
        indent=1,
    ).encode()
