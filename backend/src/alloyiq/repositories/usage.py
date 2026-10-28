"""Call intent protocol for model calls (D038, D061; build prompt section 8.5).

An intent row (unique call_key) is written before the provider call. After the call the
outcome and measured usage are persisted. On recovery, an intent with no completion is
ambiguous: it is marked so, with usage_status=unknown (the platform absorbs the cost,
R043), and is never blindly retried under the same key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from alloyiq.billing.rates import CallCost, TokenUsage


@dataclass(frozen=True, slots=True)
class CallRecord:
    id: UUID
    status: str
    created: bool


class UsageRepository:
    async def intent(self, conn: AsyncConnection, values: dict[str, Any]) -> CallRecord:
        row = (
            await conn.execute(
                text(
                    "INSERT INTO llm_calls (account_id, run_id, node_id, attempt, generation,"
                    " call_key, purpose, request_fingerprint, model_requested, effort, max_tokens,"
                    " billable, nonbillable_reason) VALUES (:account_id, :run_id, :node_id,"
                    " :attempt, :generation, :call_key, :purpose, :request_fingerprint,"
                    " :model_requested, :effort, :max_tokens, :billable, :nonbillable_reason)"
                    " ON CONFLICT (call_key) DO NOTHING RETURNING id, status"
                ),
                values,
            )
        ).first()
        if row is not None:
            return CallRecord(row.id, row.status, True)
        existing = (
            await conn.execute(
                text("SELECT id, status FROM llm_calls WHERE call_key = :k"),
                {"k": values["call_key"]},
            )
        ).one()
        return CallRecord(existing.id, existing.status, False)

    async def mark_in_flight(self, conn: AsyncConnection, call_id: UUID) -> None:
        await conn.execute(
            text("UPDATE llm_calls SET status = 'in_flight' WHERE id = :i AND status = 'intent'"),
            {"i": call_id},
        )

    async def complete(
        self,
        conn: AsyncConnection,
        call_id: UUID,
        *,
        account_id: UUID,
        run_id: UUID,
        status: str,
        model_used: str | None,
        provider_request_id: str | None,
        provider_message_id: str | None,
        stop_reason: str | None,
        refusal_category: str | None,
        error_code: str | None,
        duration_ms: int | None,
        time_to_first_token_ms: int | None,
        usage: TokenUsage | None,
        cost: CallCost,
        rate_card_version: str,
        billable: bool,
    ) -> None:
        await conn.execute(
            text(
                "UPDATE llm_calls SET status = :s, model_used = :m, provider_request_id = :pr,"
                " provider_message_id = :pm, stop_reason = :sr, refusal_category = :rc,"
                " error_code = :e, ended_at = now(), duration_ms = :d,"
                " time_to_first_token_ms = :ttft WHERE id = :i"
            ),
            {
                "i": call_id,
                "s": status,
                "m": model_used,
                "pr": provider_request_id,
                "pm": provider_message_id,
                "sr": stop_reason,
                "rc": refusal_category,
                "e": error_code,
                "d": duration_ms,
                "ttft": time_to_first_token_ms,
            },
        )
        u = usage
        await conn.execute(
            text(
                "INSERT INTO usage_records (account_id, run_id, llm_call_id, usage_status,"
                " input_tokens, output_tokens, cache_read_input_tokens,"
                " cache_creation_input_tokens, rate_card_version, provider_cost_usd, cost_status,"
                " customer_charge_inr_simulated, billable) VALUES (:a, :r, :c, :us, :it, :ot,"
                " :cr, :cw, :rv, :usd, :cs, :inr, :b) ON CONFLICT (llm_call_id) DO NOTHING"
            ),
            {
                "a": account_id,
                "r": run_id,
                "c": call_id,
                "us": "actual" if u is not None else "unknown",
                "it": u.input_tokens if u else None,
                "ot": u.output_tokens if u else None,
                "cr": u.cache_read_input_tokens if u else None,
                "cw": u.cache_creation_input_tokens if u else None,
                "rv": rate_card_version,
                "usd": cost.provider_cost_usd,
                "cs": cost.cost_status if u is not None else "unknown",
                "inr": cost.customer_charge_inr if billable else None,
                "b": billable,
            },
        )

    async def mark_ambiguous(
        self, conn: AsyncConnection, run_id: UUID, rate_card_version: str
    ) -> int:
        """Recovery: intents with no completion become ambiguous with unknown usage."""
        rows = (
            await conn.execute(
                text(
                    "UPDATE llm_calls SET status = 'ambiguous', ended_at = now(),"
                    " billable = false, nonbillable_reason = 'ambiguous_after_recovery'"
                    " WHERE run_id = :r AND status IN ('intent','in_flight')"
                    " RETURNING id, account_id"
                ),
                {"r": run_id},
            )
        ).all()
        for row in rows:
            await conn.execute(
                text(
                    "INSERT INTO usage_records (account_id, run_id, llm_call_id, usage_status,"
                    " rate_card_version, cost_status, billable) VALUES (:a, :r, :c, 'unknown',"
                    " :rv, 'unknown', false) ON CONFLICT (llm_call_id) DO NOTHING"
                ),
                {"a": row.account_id, "r": run_id, "c": row.id, "rv": rate_card_version},
            )
        return len(rows)

    async def completed_result(self, conn: AsyncConnection, call_key: str) -> dict[str, Any] | None:
        row = (
            await conn.execute(
                text(
                    "SELECT c.id, c.status, c.model_used, c.stop_reason FROM llm_calls c"
                    " WHERE c.call_key = :k AND c.status = 'completed'"
                ),
                {"k": call_key},
            )
        ).first()
        return dict(row._mapping) if row else None

    async def counts(self, conn: AsyncConnection, run_id: UUID) -> dict[str, int]:
        row = (
            await conn.execute(
                text(
                    "SELECT count(*) AS calls,"
                    " coalesce(sum(u.input_tokens + coalesce(u.cache_read_input_tokens, 0)"
                    "  + coalesce(u.cache_creation_input_tokens, 0)), 0) AS input_tokens,"
                    " coalesce(sum(u.output_tokens), 0) AS output_tokens"
                    " FROM llm_calls c LEFT JOIN usage_records u ON u.llm_call_id = c.id"
                    " WHERE c.run_id = :r"
                ),
                {"r": run_id},
            )
        ).one()
        return {k: int(v) for k, v in row._mapping.items()}
