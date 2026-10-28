# ruff: noqa: E501, S608 - readable single-line SQL; fragments are constants, values are bound
"""ReportService: personal summary and per-model report (R044-R048; D106).

- Percentiles are exact: PostgreSQL percentile_cont over raw durations, never averages
  of daily percentiles. An empty population is null; fewer than 100 samples warns.
- Each metric states its population and window basis: submissions by submitted_at,
  outcomes and success-rate denominators by terminal_at, money by posted_at. Totals may
  therefore differ between sections, and the response says so.
- Success rate = succeeded / (succeeded + incomplete + failed + timed_out); cancellations
  and queue expiries are reported separately.
- The synchronous range is at most 90 days; results are cached for 30 s per account.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from alloyiq.api.schemas.common import Money
from alloyiq.api.schemas.reports import (
    Counts,
    DailyPoint,
    Latency,
    LatencyDistribution,
    LatestRun,
    ModelRow,
    ModelsReport,
    ProviderCost,
    SuccessRate,
    Summary,
    Usage,
    WindowBasis,
)
from alloyiq.domain.errors import DomainError, ErrorCode
from alloyiq.domain.identity import AccountContext
from alloyiq.repositories.db import Database, DbContext
from alloyiq.stats.core import LOW_SAMPLE, success_rate

MAX_RANGE = timedelta(days=90)
CACHE_TTL_S = 30.0
TERMINAL = "('succeeded','incomplete','failed','cancelled','timed_out')"

_FILTER = (
    " AND (CAST(:ins AS uuid) IS NULL OR r.instrument_id = :ins)"
    " AND (CAST(:mp AS uuid) IS NULL OR r.model_profile_id = :mp)"
)


def _dist(row: Any, population: str) -> LatencyDistribution:
    n = int(row.n or 0)
    p = row.p or [None, None, None]
    return LatencyDistribution(
        sample_count=n,
        p50_ms=float(p[0]) if n and p[0] is not None else None,
        p95_ms=float(p[1]) if n and p[1] is not None else None,
        p99_ms=float(p[2]) if n and p[2] is not None else None,
        population=population,
        low_sample=n < LOW_SAMPLE,
    )


async def _percentiles(conn: AsyncConnection, expr: str, where: str, params: dict[str, Any]) -> Any:
    return (
        await conn.execute(
            text(
                f"SELECT count(*) AS n, percentile_cont(ARRAY[0.5, 0.95, 0.99])"
                f" WITHIN GROUP (ORDER BY {expr}) AS p FROM research_runs r WHERE {where}"
            ),
            params,
        )
    ).one()


def _ms(a: str, b: str) -> str:
    return f"(extract(epoch FROM ({b} - {a})) * 1000)"


class ReportService:
    def __init__(
        self, db: Database, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self.db = db
        self.now = clock
        self._cache: dict[tuple[Any, ...], tuple[float, Any]] = {}

    def _check_range(self, start: datetime, end: datetime) -> None:
        if end <= start:
            raise DomainError(
                422,
                ErrorCode.VALIDATION_FAILED,
                "Invalid range",
                "'to' must be after 'from'.",
                field_errors=[("to", "before from")],
            )
        if end - start > MAX_RANGE:
            raise DomainError(
                422,
                ErrorCode.VALIDATION_FAILED,
                "Range too long",
                "Reports cover at most 90 days. Use an export for longer ranges.",
                field_errors=[("to", "range exceeds 90 days")],
            )

    def _cached(self, key: tuple[Any, ...]) -> Any | None:
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
            return hit[1]
        return None

    async def summary(
        self,
        ctx: AccountContext,
        *,
        start: datetime,
        end: datetime,
        instrument_id: UUID | None = None,
        model_profile_id: UUID | None = None,
    ) -> Summary:
        self._check_range(start, end)
        key = ("summary", ctx.account_id, start, end, instrument_id, model_profile_id)
        if (hit := self._cached(key)) is not None:
            return hit  # type: ignore[no-any-return]
        p = {
            "w": f"wallet:{ctx.account_id}",
            "a": ctx.account_id,
            "s": start,
            "e": end,
            "ins": instrument_id,
            "mp": model_profile_id,
        }
        async with self.db.transaction(
            DbContext(account_id=ctx.account_id, user_id=ctx.user_id)
        ) as conn:
            result = await self._summary(conn, p)
        self._cache[key] = (time.monotonic(), result)
        return result

    async def _summary(self, conn: AsyncConnection, p: dict[str, Any]) -> Summary:
        sub = "r.account_id = :a AND r.submitted_at >= :s AND r.submitted_at < :e" + _FILTER
        term = "r.account_id = :a AND r.terminal_at >= :s AND r.terminal_at < :e" + _FILTER
        c = (
            await conn.execute(
                text(
                    "SELECT"
                    f" (SELECT count(*) FROM research_runs r WHERE {sub}) AS submitted,"
                    f" (SELECT count(*) FROM research_runs r WHERE r.account_id = :a AND r.status = 'queued'{_FILTER}) AS queued,"
                    f" (SELECT count(*) FROM research_runs r WHERE r.account_id = :a AND r.status IN ('running','cancel_requested'){_FILTER}) AS running,"
                    f" count(*) FILTER (WHERE r.status = 'succeeded') AS succeeded,"
                    f" count(*) FILTER (WHERE r.status = 'incomplete') AS incomplete,"
                    f" count(*) FILTER (WHERE r.status = 'failed') AS failed,"
                    f" count(*) FILTER (WHERE r.status = 'cancelled') AS cancelled,"
                    " count(*) FILTER (WHERE r.status = 'timed_out' AND r.outcome_reason IS DISTINCT FROM 'queue_expired') AS timed_out,"
                    " count(*) FILTER (WHERE r.status = 'timed_out' AND r.outcome_reason = 'queue_expired') AS queue_expired,"
                    " count(*) FILTER (WHERE r.billing_status = 'pending_settlement') AS pending_billing"
                    f" FROM research_runs r WHERE {term}"
                ),
                p,
            )
        ).one()
        counts = Counts(**{k: int(v) for k, v in c._mapping.items()})
        rate = success_rate(counts.succeeded, counts.incomplete, counts.failed, counts.timed_out)

        lat = Latency(
            success_end_to_end=_dist(
                await _percentiles(
                    conn,
                    _ms("r.submitted_at", "r.terminal_at"),
                    term + " AND r.status = 'succeeded'",
                    p,
                ),
                "succeeded runs, terminal_at in window: submitted -> terminal",
            ),
            all_terminal_end_to_end=_dist(
                await _percentiles(conn, _ms("r.submitted_at", "r.terminal_at"), term, p),
                "all terminal runs, terminal_at in window: submitted -> terminal",
            ),
            queue=_dist(
                await _percentiles(
                    conn,
                    _ms("r.submitted_at", "r.started_at"),
                    sub + " AND r.started_at IS NOT NULL",
                    p,
                ),
                "started runs, submitted_at in window: submitted -> started",
            ),
            execution=_dist(
                await _percentiles(
                    conn,
                    _ms("r.started_at", "r.terminal_at"),
                    term + " AND r.started_at IS NOT NULL",
                    p,
                ),
                "terminal runs that started, terminal_at in window: started -> terminal",
            ),
        )

        u = (
            await conn.execute(
                text(
                    "SELECT coalesce(sum(u.input_tokens), 0) AS i, coalesce(sum(u.output_tokens), 0) AS o,"
                    " coalesce(sum(u.cache_read_input_tokens), 0) AS cr,"
                    " coalesce(sum(u.cache_creation_input_tokens), 0) AS cw,"
                    " count(*) FILTER (WHERE u.usage_status = 'unknown') AS unknown,"
                    " coalesce(sum(u.provider_cost_usd), 0) AS usd,"
                    " count(*) FILTER (WHERE u.cost_status = 'unknown') AS cost_unknown,"
                    " count(*) FILTER (WHERE coalesce(u.cache_read_input_tokens, 0) > 0) AS cache_hits"
                    " FROM usage_records u JOIN research_runs r ON r.id = u.run_id"
                    f" WHERE {term}"
                ),
                p,
            )
        ).one()
        usage = Usage(
            input_tokens=int(u.i),
            output_tokens=int(u.o),
            cached_input_tokens=int(u.cr),
            cache_write_tokens=int(u.cw),
            unknown_call_count=int(u.unknown),
        )
        cost = ProviderCost(
            amount=Money.usd(Decimal(u.usd)),
            cost_status="unknown" if u.cost_unknown else "calculated",
            unknown_call_count=int(u.cost_unknown),
        )
        charge = (
            await conn.execute(
                text(
                    "SELECT coalesce(-sum(pp.amount), 0) FROM ledger_transactions t"
                    " JOIN ledger_postings pp ON pp.transaction_id = t.id"
                    "  AND pp.ledger_account = CAST(:w AS text)"
                    " JOIN research_runs r ON r.id = t.run_id"
                    " WHERE t.account_id = :a AND t.kind = 'run_debit'"
                    f"  AND t.posted_at >= :s AND t.posted_at < :e{_FILTER}"
                ),
                p,
            )
        ).scalar_one()
        ops = (
            await conn.execute(
                text(
                    "SELECT"
                    " (SELECT count(*) FROM job_attempts ja JOIN jobs j ON j.id = ja.job_id"
                    "   JOIN research_runs r ON r.id = j.run_id"
                    f"   WHERE ja.generation > 1 AND {term}) AS retries,"
                    " (SELECT count(*) FROM tool_calls tc JOIN research_runs r ON r.id = tc.run_id"
                    f"   WHERE tc.status = 'failed' AND {term}) AS tool_errors,"
                    " (SELECT count(*) FROM validation_results v JOIN research_runs r ON r.id = v.run_id"
                    f"   WHERE v.verdict = 'fail' AND {term}) AS validation_failures,"
                    " (SELECT count(*) FROM security_events se JOIN research_runs r ON r.id = se.run_id"
                    "   WHERE se.kind IN ('injection.quarantined','tool.not_allowed','llm.refusal')"
                    f"   AND {term}) AS security_blocks"
                ),
                p,
            )
        ).one()
        latest = (
            await conn.execute(
                text(
                    "SELECT r.id, r.status, r.submitted_at, r.terminal_at, r.billing_status,"
                    " coalesce(array_agg(DISTINCT c.model_used) FILTER (WHERE c.model_used IS NOT NULL),"
                    " '{}') AS models FROM research_runs r LEFT JOIN llm_calls c ON c.run_id = r.id"
                    f" WHERE r.account_id = :a{_FILTER} GROUP BY r.id"
                    " ORDER BY r.submitted_at DESC LIMIT 1"
                ),
                p,
            )
        ).first()
        daily_rows = await conn.execute(
            text(
                "SELECT to_char(d, 'YYYY-MM-DD') AS day,"
                " (SELECT count(*) FROM research_runs r WHERE r.account_id = :a"
                f"   AND r.submitted_at >= d AND r.submitted_at < d + interval '1 day'{_FILTER}) AS submitted,"
                " (SELECT count(*) FROM research_runs r WHERE r.account_id = :a AND r.status = 'succeeded'"
                f"   AND r.terminal_at >= d AND r.terminal_at < d + interval '1 day'{_FILTER}) AS succeeded,"
                " (SELECT coalesce(sum(u.provider_cost_usd), 0) FROM usage_records u"
                "   JOIN research_runs r ON r.id = u.run_id WHERE r.account_id = :a"
                f"   AND r.terminal_at >= d AND r.terminal_at < d + interval '1 day'{_FILTER}) AS usd,"
                " (SELECT coalesce(-sum(pp.amount), 0) FROM ledger_transactions t"
                "   JOIN ledger_postings pp ON pp.transaction_id = t.id"
                "    AND pp.ledger_account = CAST(:w AS text)"
                "   JOIN research_runs r ON r.id = t.run_id"
                "   WHERE t.account_id = :a AND t.kind = 'run_debit'"
                f"   AND t.posted_at >= d AND t.posted_at < d + interval '1 day'{_FILTER}) AS inr"
                " FROM generate_series(date_trunc('day', CAST(:s AS timestamptz) AT TIME ZONE 'UTC'),"
                "  date_trunc('day', (CAST(:e AS timestamptz) - interval '1 microsecond') AT TIME ZONE 'UTC'),"
                "  interval '1 day') AS g(day_utc), LATERAL (SELECT g.day_utc AT TIME ZONE 'UTC' AS d) x"
            ),
            p,
        )
        daily = [
            DailyPoint(
                day=r.day,
                submitted=int(r.submitted),
                succeeded=int(r.succeeded),
                provider_cost_usd=str(Decimal(r.usd)),
                customer_charge_inr=str(Decimal(r.inr)),
            )
            for r in daily_rows
        ]
        warnings = [
            f"{name.replace('_', ' ')}: only {d['sample_count']} samples (< {LOW_SAMPLE}); percentiles are unstable."
            for name, d in lat.model_dump().items()
            if d["low_sample"] and d["sample_count"]
        ]
        warnings.append(
            "Provider cost is calculated from measured tokens and the pinned rate card; it is not"
            " reconciled with provider invoices (DV-07)."
        )
        if usage.unknown_call_count:
            warnings.append(
                f"{usage.unknown_call_count} call(s) have unknown usage; cost totals are incomplete."
            )
        return Summary(
            interval={"from": p["s"], "to": p["e"]},
            data_as_of=self.now(),
            filters={
                "instrument_id": str(p["ins"]) if p["ins"] else None,
                "model_profile_id": str(p["mp"]) if p["mp"] else None,
            },
            window_basis=WindowBasis(),
            counts=counts,
            success_rate=SuccessRate(
                numerator=rate.numerator, denominator=rate.denominator, value=rate.value
            ),
            latency=lat,
            usage=usage,
            provider_cost=cost,
            customer_charge=Money.inr(Decimal(charge)),
            retries=int(ops.retries),
            tool_errors=int(ops.tool_errors),
            validation_failures=int(ops.validation_failures),
            security_blocks=int(ops.security_blocks),
            cache_hits=int(u.cache_hits),
            latest_run=LatestRun(
                run_id=latest.id,
                status=latest.status,
                submitted_at=latest.submitted_at,
                end_to_end_ms=int((latest.terminal_at - latest.submitted_at).total_seconds() * 1000)
                if latest.terminal_at
                else None,
                billing_status=latest.billing_status,
                model_ids=list(latest.models),
            )
            if latest
            else None,
            daily=daily,
            warnings=warnings,
        )

    async def models(self, ctx: AccountContext, *, start: datetime, end: datetime) -> ModelsReport:
        self._check_range(start, end)
        key = ("models", ctx.account_id, start, end)
        if (hit := self._cached(key)) is not None:
            return hit  # type: ignore[no-any-return]
        p = {"a": ctx.account_id, "s": start, "e": end}
        async with self.db.transaction(
            DbContext(account_id=ctx.account_id, user_id=ctx.user_id)
        ) as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT c.model_used AS model, count(*) AS calls,"
                        " coalesce(sum(u.input_tokens), 0) AS i, coalesce(sum(u.output_tokens), 0) AS o,"
                        " coalesce(sum(u.cache_read_input_tokens), 0) AS cr,"
                        " coalesce(sum(u.cache_creation_input_tokens), 0) AS cw,"
                        " count(*) FILTER (WHERE u.usage_status = 'unknown' OR u.id IS NULL) AS unknown,"
                        " coalesce(sum(u.provider_cost_usd), 0) AS usd,"
                        " count(*) FILTER (WHERE u.cost_status = 'unknown') AS cost_unknown,"
                        " coalesce(sum(u.customer_charge_inr_simulated) FILTER (WHERE u.billable), 0) AS inr,"
                        " count(c.duration_ms) AS n,"
                        " percentile_cont(ARRAY[0.5, 0.95, 0.99]) WITHIN GROUP (ORDER BY c.duration_ms) AS p"
                        " FROM llm_calls c LEFT JOIN usage_records u ON u.llm_call_id = c.id"
                        " WHERE c.account_id = :a AND c.model_used IS NOT NULL"
                        " AND c.started_at >= :s AND c.started_at < :e GROUP BY c.model_used ORDER BY 1"
                    ),
                    p,
                )
            ).all()
            outcomes = {
                r.model: r
                for r in (
                    await conn.execute(
                        text(
                            "SELECT m.model, count(*) FILTER (WHERE r.status = 'succeeded') AS s,"
                            " count(*) FILTER (WHERE r.status = 'incomplete') AS i,"
                            " count(*) FILTER (WHERE r.status = 'failed') AS f,"
                            " count(*) FILTER (WHERE r.status = 'timed_out'"
                            "  AND r.outcome_reason IS DISTINCT FROM 'queue_expired') AS timed_out"
                            " FROM research_runs r JOIN LATERAL (SELECT DISTINCT c.model_used AS model"
                            "  FROM llm_calls c WHERE c.run_id = r.id AND c.model_used IS NOT NULL) m ON true"
                            " WHERE r.account_id = :a AND r.terminal_at >= :s AND r.terminal_at < :e"
                            " GROUP BY m.model"
                        ),
                        p,
                    )
                ).all()
            }
        models: list[ModelRow] = []
        for r in rows:
            o = outcomes.get(r.model)
            rate = (
                success_rate(int(o.s), int(o.i), int(o.f), int(o.timed_out))
                if o
                else success_rate(0, 0, 0, 0)
            )
            models.append(
                ModelRow(
                    model_id=r.model,
                    calls=int(r.calls),
                    usage=Usage(
                        input_tokens=int(r.i),
                        output_tokens=int(r.o),
                        cached_input_tokens=int(r.cr),
                        cache_write_tokens=int(r.cw),
                        unknown_call_count=int(r.unknown),
                    ),
                    provider_cost=ProviderCost(
                        amount=Money.usd(Decimal(r.usd)),
                        cost_status="unknown" if r.cost_unknown else "calculated",
                        unknown_call_count=int(r.cost_unknown),
                    ),
                    customer_charge=Money.inr(Decimal(r.inr)),
                    success_rate=SuccessRate(
                        numerator=rate.numerator, denominator=rate.denominator, value=rate.value
                    ),
                    latency=_dist(r, f"{r.model} calls, started_at in window: call duration"),
                )
            )
        result = ModelsReport(
            interval={"from": start, "to": end},
            data_as_of=self.now(),
            models=models,
            warnings=[
                "Run outcomes are attributed to every model that served a call in the run.",
                "Customer charges here are per-call simulated amounts before per-run rounding;"
                " the ledger (summary) is authoritative for posted demo debits.",
            ],
        )
        self._cache[key] = (time.monotonic(), result)
        return result
