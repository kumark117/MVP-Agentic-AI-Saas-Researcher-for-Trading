"""ResearchService: estimate, atomic admission, snapshots, listing, cancellation and the
usage/activity/report reads (R003, R004, R018-R027; D048-D050, D059-D060, D105-D107).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from alloyiq.api.schemas.catalog import RunLimits
from alloyiq.api.schemas.common import CostStatus, Money, UsageStatus
from alloyiq.api.schemas.events import RunCancelRequested, RunQueued
from alloyiq.api.schemas.research import (
    Activity,
    Estimate,
    Progress,
    Report,
    RunAccepted,
    RunSnapshot,
    RunSummary,
    RunUsage,
    TokenCategories,
    UsageCall,
    Versions,
)
from alloyiq.application.budget import BudgetService
from alloyiq.application.catalog import POLICY_VERSION, RUN_LIMITS
from alloyiq.application.idempotent import once
from alloyiq.application.lifecycle import RunLifecycle
from alloyiq.application.wallet import WalletService
from alloyiq.billing.rates import RateCard, UnpricedModelError, worst_case
from alloyiq.domain.errors import DomainError, ErrorCode, not_found, rate_limited
from alloyiq.domain.identity import AccountContext
from alloyiq.domain.runs import GRAPH_NODES, TERMINAL
from alloyiq.repositories.audit import audit
from alloyiq.repositories.catalog import CatalogRepository
from alloyiq.repositories.db import Database, DbContext
from alloyiq.repositories.events import append_run_event, event_id
from alloyiq.repositories.paging import decode_cursor, encode_cursor
from alloyiq.repositories.runs import CANCEL_CHANNEL, RunRepository
from alloyiq.security.ratelimit import LIMITS, RateLimiter
from alloyiq.workflows.versions import run_versions

ESTIMATE_TTL = timedelta(minutes=5)
TYPICAL_FRACTION = Decimal("0.40")
ESTIMATE_BASIS = (
    "Max charge = worst case over the approved models (run input and output token budgets x "
    "provider rate) x demo markup x demo FX assumption, rounded up to the paisa. The estimated "
    "charge is 40% of that worst case and is only an estimate. Simulated INR: no money is "
    "collected. Real provider cost is capped separately."
)
CANCEL_NOTICE = "Provider work already started may still incur real platform cost."


def _ctx(ctx: AccountContext) -> DbContext:
    return DbContext(account_id=ctx.account_id, user_id=ctx.user_id)


def fingerprint(obj: dict[str, Any]) -> str:
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _ms(a: datetime | None, b: datetime | None) -> int | None:
    if a is None or b is None:
        return None
    return max(0, int((b - a).total_seconds() * 1000))


def _invalid(
    field: str, title: str, detail: str, code: ErrorCode = ErrorCode.VALIDATION_FAILED
) -> DomainError:
    return DomainError(422, code, title, detail, field_errors=[(field, detail)])


class ResearchService:
    def __init__(
        self,
        db: Database,
        *,
        wallet: WalletService,
        budget: BudgetService,
        lifecycle: RunLifecycle,
        limiter: RateLimiter,
        catalog: CatalogRepository | None = None,
        runs: RunRepository | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.db = db
        self.wallet = wallet
        self.budget = budget
        self.lifecycle = lifecycle
        self.limiter = limiter
        self.catalog = catalog or CatalogRepository()
        self.runs = runs or RunRepository()
        self.now = clock

    # ------------------------------------------------------------------ estimate (8.2)
    async def estimate(
        self,
        ctx: AccountContext,
        *,
        instrument_id: UUID,
        horizon: str,
        model_profile_id: UUID,
        question: str | None,
        conversation_id: UUID | None,
    ) -> Estimate:
        decision = await self.limiter.hit(
            f"estimate:account:{ctx.account_id}", LIMITS["estimate:account"]
        )
        if not decision.allowed:
            raise rate_limited(decision.retry_after_s)
        question = (question or "").strip() or None
        async with self.db.transaction(_ctx(ctx)) as conn:
            ins = await self.catalog.get_instrument(conn, instrument_id)
            if ins is None or not ins.enabled:
                raise _invalid(
                    "instrument_id",
                    "Instrument unavailable",
                    ins.unavailable_reason
                    if ins and ins.unavailable_reason
                    else "Choose an enabled instrument.",
                    ErrorCode.UNSUPPORTED_INSTRUMENT,
                )
            if horizon not in ins.supported_horizons:
                raise _invalid(
                    "horizon",
                    "Horizon not supported",
                    f"{ins.display_name} supports: {', '.join(ins.supported_horizons)}.",
                    ErrorCode.UNSUPPORTED_INSTRUMENT,
                )
            profile = await self.catalog.get_model_profile(conn, model_profile_id)
            if profile is None or not profile["enabled"]:
                raise _invalid(
                    "model_profile_id", "Unknown model profile", "Choose an enabled model profile."
                )
            cutoff = None
            if conversation_id is not None:
                if not await self.runs.conversation_exists(conn, conversation_id):
                    raise not_found("Conversation")
                cutoff = await self.runs.latest_message_id(conn, conversation_id)
            card_row = await self.catalog.current_rate_card(conn)
            if card_row is None:
                raise DomainError(
                    503,
                    ErrorCode.PROVIDER_UNAVAILABLE,
                    "Pricing unavailable",
                    "No rate card is configured.",
                )
            card = RateCard.from_row(card_row)
            approved = [profile["primary_model_id"], *profile["approved_fallback_model_ids"]]
            try:
                wc = worst_case(
                    card,
                    approved,
                    input_budget=int(profile["run_input_token_budget"]),
                    output_budget=int(profile["run_output_token_budget"]),
                    typical_fraction=TYPICAL_FRACTION,
                )
            except UnpricedModelError as exc:
                raise DomainError(
                    503,
                    ErrorCode.PROVIDER_UNAVAILABLE,
                    "Pricing unavailable",
                    "This model profile has no verified price yet.",
                ) from exc
            self.budget.check_per_run(wc.real_usd)
            token_budget = {
                "run_input_tokens": int(profile["run_input_token_budget"]),
                "run_output_tokens": int(profile["run_output_token_budget"]),
                "max_tokens_branch": int(profile["max_tokens_branch"]),
                "max_tokens_signal": int(profile["max_tokens_signal"]),
                "max_tokens_repair": int(profile["max_tokens_repair"]),
                "context_limit_tokens": int(profile["context_limit_tokens"]),
            }
            limits = {**RUN_LIMITS, "token_budget": token_budget, "policy_version": POLICY_VERSION}
            normalized = {
                "instrument_id": str(ins.id),
                "canonical_id": ins.canonical_id,
                "horizon": horizon,
                "model_profile_id": str(model_profile_id),
                "question": question,
                "conversation_id": str(conversation_id) if conversation_id else None,
                "message_cutoff_id": str(cutoff) if cutoff else None,
            }
            row = await self.runs.insert_estimate(
                conn,
                {
                    "account_id": ctx.account_id,
                    "user_id": ctx.user_id,
                    "normalized_request": {
                        **normalized,
                        "instrument_snapshot": ins.snapshot(),
                        "profile": {
                            "slug": profile["slug"],
                            "primary_model_id": profile["primary_model_id"],
                            "branch_effort": profile["branch_effort"],
                            "signal_effort": profile["signal_effort"],
                        },
                        "pricing_model": wc.pricing_model,
                    },
                    "request_fingerprint": fingerprint(normalized),
                    "instrument_id": ins.id,
                    "horizon": horizon,
                    "model_profile_id": model_profile_id,
                    "approved_models": approved,
                    "limits": limits,
                    "rate_card_version": card.version,
                    "min_charge": wc.customer_min_inr,
                    "estimated_charge": wc.customer_estimate_inr,
                    "max_charge": wc.customer_max_inr,
                    "max_real_usd": wc.real_usd,
                    "question": question,
                    "conversation_id": conversation_id,
                    "message_cutoff_id": cutoff,
                    "expires_at": self.now() + ESTIMATE_TTL,
                },
            )
        return Estimate(
            estimate_id=row["id"],
            request_fingerprint=row["request_fingerprint"],
            normalized_request=dict(normalized),
            model_profile_id=model_profile_id,
            primary_model_id=profile["primary_model_id"],
            approved_models=approved,
            limits=RunLimits(**RUN_LIMITS),
            token_budget=token_budget,
            price_version=card.version + (" (draft)" if card.status == "draft" else ""),
            min_charge=Money.inr(wc.customer_min_inr),
            estimated_charge=Money.inr(wc.customer_estimate_inr),
            max_charge=Money.inr(wc.customer_max_inr),
            estimate_basis=ESTIMATE_BASIS,
            max_real_cost=Money.usd(wc.real_usd),
            expires_at=row["expires_at"],
        )

    # ------------------------------------------------------------------ admission (8.3)
    async def admit(
        self,
        ctx: AccountContext,
        *,
        estimate_id: UUID,
        max_charge: Decimal,
        key: UUID,
        request_id: str | None,
    ) -> tuple[int, RunAccepted]:
        body = {"estimate_id": str(estimate_id), "max_charge": str(max_charge)}
        async with self.db.transaction(_ctx(ctx)) as conn:
            return await once(
                conn,
                account_id=ctx.account_id,
                method="POST",
                route="/research/runs",
                path="/research/runs",
                key=key,
                request=body,
                model=RunAccepted,
                status=202,
                action=lambda: self._admit(conn, ctx, estimate_id, max_charge, request_id),
                resource_id=lambda r: r.run_id,
            )

    async def _admit(
        self,
        conn: AsyncConnection,
        ctx: AccountContext,
        estimate_id: UUID,
        max_charge: Decimal,
        request_id: str | None,
    ) -> RunAccepted:
        est = await self.runs.get_estimate(conn, estimate_id, lock=True)
        if est is None:
            raise not_found("Estimate")
        if est["expires_at"] <= self.now():
            raise DomainError(
                410, ErrorCode.ESTIMATE_EXPIRED, "Estimate expired", "Get a new estimate."
            )
        if est["consumed_by_run_id"] is not None:
            raise DomainError(
                409,
                ErrorCode.INVALID_RUN_STATE,
                "Estimate already used",
                "This estimate already started a run. Get a new estimate.",
            )
        if max_charge < Decimal(est["max_charge"]):
            raise _invalid(
                "max_charge",
                "Maximum charge too low",
                f"The authorised maximum must be at least INR {est['max_charge']}.",
                ErrorCode.ESTIMATE_MISMATCH,
            )
        # Lock order: wallet -> account_day -> platform_day. The wallet lock also serialises
        # this account's admissions, which makes the plan-limit count below exact.
        await self.wallet.repo.lock_wallet(conn, ctx.account_id)
        plan = await self.runs.plan_limits(conn, ctx.account_id)
        if plan["account_status"] != "active":
            raise DomainError(
                403,
                ErrorCode.ACCOUNT_SUSPENDED,
                "Account unavailable",
                "This account is suspended.",
            )
        active, queued = await self.runs.count_open(conn, ctx.account_id)
        if (
            queued >= plan["queued_runs"]
            or active + queued >= plan["active_runs"] + plan["queued_runs"]
        ):
            raise DomainError(
                429,
                ErrorCode.PLAN_LIMIT_REACHED,
                "Too many runs in progress",
                f"The Demo plan allows {plan['active_runs']} running and {plan['queued_runs']} "
                "queued runs. Wait for one to finish.",
                headers={"Retry-After": "30"},
            )
        ins = await self.catalog.get_instrument(conn, est["instrument_id"])
        if ins is None or not ins.enabled:
            raise _invalid(
                "estimate_id",
                "Instrument unavailable",
                "The instrument is no longer available.",
                ErrorCode.UNSUPPORTED_INSTRUMENT,
            )
        run_id = uuid4()
        reserve = Decimal(est["max_charge"])  # reserve the estimate maximum, never more (D059)
        nr = est["normalized_request"]
        conversation_id = est["conversation_id"]
        if conversation_id is None and est["question"]:
            conversation_id = await self.runs.create_conversation(
                conn, ctx.account_id, ctx.user_id, est["question"][:80]
            )
        versions = run_versions(POLICY_VERSION, est["rate_card_version"])
        await self.runs.insert_run(
            conn,
            {
                "id": run_id,
                "account_id": ctx.account_id,
                "user_id": ctx.user_id,
                "conversation_id": conversation_id,
                "estimate_id": estimate_id,
                "instrument_id": est["instrument_id"],
                "instrument_snapshot": nr.get("instrument_snapshot") or ins.snapshot(),
                "horizon": est["horizon"],
                "model_profile_id": est["model_profile_id"],
                "question_message_id": None,
                "versions": versions,
                "limits": {
                    **est["limits"],
                    "approved_models": list(est["approved_models"]),
                    "max_real_usd": str(est["max_real_usd"]),
                },
                "authorized_max_charge": max_charge,
                "reserved_amount": reserve,
            },
        )
        await self.wallet.reserve(conn, ctx.account_id, run_id, reserve)
        await self.budget.reserve(conn, ctx.account_id, run_id, Decimal(est["max_real_usd"]))
        if est["question"] and conversation_id is not None:
            mid = await self.runs.insert_message(
                conn,
                account_id=ctx.account_id,
                conversation_id=conversation_id,
                user_id=ctx.user_id,
                role="user",
                content=est["question"],
                run_id=run_id,
            )
            await self.runs.update_run(conn, run_id, {"question_message_id": mid})
        if not await self.runs.consume_estimate(conn, estimate_id, run_id):  # pragma: no cover
            raise DomainError(
                409, ErrorCode.INVALID_RUN_STATE, "Estimate already used", "Get a new estimate."
            )
        await self.runs.insert_job(conn, ctx.account_id, run_id)
        await self.runs.outbox(conn, ctx.account_id, "run.queued", {"run_id": str(run_id)})
        await append_run_event(
            conn,
            ctx.account_id,
            run_id,
            "run.queued",
            RunQueued(billing_status="reserved", reserved_amount=Money.inr(reserve)),
            request_id=request_id,
        )
        run = await self.runs.get_run(conn, run_id)
        assert run is not None  # noqa: S101
        await self.lifecycle.emit_run_updated(conn, run, request_id)
        await self.wallet.emit_balance(conn, ctx.account_id, request_id=request_id)
        await audit(
            conn,
            action="run.admitted",
            outcome="queued",
            account_id=ctx.account_id,
            actor_user_id=ctx.user_id,
            run_id=run_id,
            request_id=request_id,
            metadata={
                "reserved_inr_simulated": str(reserve),
                "reserved_real_usd": str(est["max_real_usd"]),
                "rate_card": est["rate_card_version"],
            },
        )
        return RunAccepted(
            run_id=run_id,
            status="queued",
            billing_status="reserved",
            reserved_amount=Money.inr(reserve),
            status_url=f"/api/v1/research/runs/{run_id}",
            events_url=f"/api/v1/research/runs/{run_id}/events",
        )

    # ------------------------------------------------------------------ reads
    async def _owned_run(self, conn: AsyncConnection, run_id: UUID) -> dict[str, Any]:
        run = await self.runs.get_run(conn, run_id)
        if run is None:  # RLS hides other accounts' runs: 404 either way
            raise not_found("Run")
        return run

    async def _snapshot(self, conn: AsyncConnection, run: dict[str, Any]) -> RunSnapshot:
        done, active = await self.runs.progress(conn, run["id"])
        v = run["versions"] or {}
        submitted, started, terminal = run["submitted_at"], run["started_at"], run["terminal_at"]
        return RunSnapshot(
            run_id=run["id"],
            conversation_id=run["conversation_id"],
            instrument_id=run["instrument_id"],
            instrument_snapshot=run["instrument_snapshot"],
            horizon=run["horizon"],
            model_profile_id=run["model_profile_id"],
            status=run["status"],
            billing_status=run["billing_status"],
            outcome_reason=run["outcome_reason"],
            error_code=run["error_code"],
            submitted_at=submitted,
            started_at=started,
            cancel_requested_at=run["cancel_requested_at"],
            terminal_at=terminal,
            queue_ms=_ms(submitted, started),
            execution_ms=_ms(started, terminal),
            end_to_end_ms=_ms(submitted, terminal),
            time_to_first_activity_ms=run["time_to_first_activity_ms"],
            time_to_first_model_token_ms=run["time_to_first_model_token_ms"],
            latest_event_id=(
                event_id(run["id"], run["latest_event_seq"]) if run["latest_event_seq"] else None
            ),
            report_available=await self.runs.report_exists(conn, run["id"]),
            progress=Progress(
                completed_nodes=done, total_nodes=len(GRAPH_NODES), active_nodes=active
            ),
            authorized_max_charge=Money.inr(run["authorized_max_charge"]),
            reserved_amount=Money.inr(run["reserved_amount"]),
            settled_charge=Money.inr(run["settled_charge"])
            if run["settled_charge"] is not None
            else None,
            pending_amount=Money.inr(run["pending_amount"]),
            released_amount=Money.inr(run["released_amount"]),
            versions=Versions(
                graph=v.get("graph", ""),
                prompt=v.get("prompt", ""),
                policy=v.get("policy", ""),
                rate_card=v.get("rate_card", ""),
                application=v.get("application", ""),
            ),
            observability_gap=run["observability_gap"],
        )

    async def snapshot(self, ctx: AccountContext, run_id: UUID) -> RunSnapshot:
        async with self.db.transaction(_ctx(ctx)) as conn:
            return await self._snapshot(conn, await self._owned_run(conn, run_id))

    async def list_runs(
        self,
        ctx: AccountContext,
        *,
        status: str | None = None,
        instrument_id: UUID | None = None,
        model_profile_id: UUID | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> tuple[list[RunSummary], str | None]:
        after = decode_cursor(cursor)
        async with self.db.transaction(_ctx(ctx)) as conn:
            rows = await self.runs.list_runs(
                conn,
                ctx.account_id,
                status=status,
                instrument_id=instrument_id,
                model_profile_id=model_profile_id,
                start=start,
                end=end,
                after=after,
                limit=limit + 1,
            )
        more = len(rows) > limit
        rows = rows[:limit]
        items = [
            RunSummary(
                run_id=r["id"],
                instrument_id=r["instrument_id"],
                instrument_display_name=str(
                    (r["instrument_snapshot"] or {}).get("display_name", "")
                ),
                horizon=r["horizon"],
                model_profile_id=r["model_profile_id"],
                status=r["status"],
                billing_status=r["billing_status"],
                signal=r["signal"],
                submitted_at=r["submitted_at"],
                terminal_at=r["terminal_at"],
                end_to_end_ms=_ms(r["submitted_at"], r["terminal_at"]),
                settled_charge=Money.inr(r["settled_charge"])
                if r["settled_charge"] is not None
                else None,
            )
            for r in rows
        ]
        nxt = encode_cursor(rows[-1]["submitted_at"], rows[-1]["id"]) if more and rows else None
        return items, nxt

    # ------------------------------------------------------------------ cancellation (8.6)
    async def cancel(
        self, ctx: AccountContext, run_id: UUID, reason: str | None, request_id: str | None
    ) -> tuple[int, RunSnapshot]:
        """Durable and idempotent: 202 while pending, 200 once terminal."""
        async with self.db.transaction(_ctx(ctx)) as conn:
            run = await self._owned_run(conn, run_id)
            if run["status"] in TERMINAL:
                return 200, await self._snapshot(conn, run)
            if run["status"] != "cancel_requested":
                now = self.now()
                moved = await self.runs.transition(
                    conn,
                    run_id,
                    "cancel_requested",
                    sets={"cancel_requested_at": now, "cancel_reason": reason},
                )
                if moved:
                    await append_run_event(
                        conn,
                        ctx.account_id,
                        run_id,
                        "run.cancel_requested",
                        RunCancelRequested(cancel_requested_at=now, notice=CANCEL_NOTICE),
                        request_id=request_id,
                    )
                    await conn.execute(
                        text("SELECT pg_notify(:c, :p)"),
                        {"c": CANCEL_CHANNEL, "p": json.dumps({"scope_id": str(run_id)})},
                    )
                    await audit(
                        conn,
                        action="run.cancel_requested",
                        outcome="requested",
                        account_id=ctx.account_id,
                        actor_user_id=ctx.user_id,
                        run_id=run_id,
                        request_id=request_id,
                    )
                    if run["status"] == "queued":
                        # No worker has started it: resolve now, with no model work.
                        claimed = await self.runs.finish_job(conn, run_id)
                        if claimed:
                            await self.lifecycle.finish(
                                conn,
                                run_id,
                                "cancelled",
                                outcome_reason="cancelled_before_start",
                                sets={"cancel_stop_calls_ms": 0, "cancel_stop_branches_ms": 0},
                                request_id=request_id,
                            )
            run = await self._owned_run(conn, run_id)
            status = 200 if run["status"] in TERMINAL else 202
            return status, await self._snapshot(conn, run)

    # ------------------------------------------------------------------ usage (D107)
    async def usage(self, ctx: AccountContext, run_id: UUID) -> RunUsage:
        async with self.db.transaction(_ctx(ctx)) as conn:
            run = await self._owned_run(conn, run_id)
            rows = (
                await conn.execute(
                    text(
                        "SELECT c.id, c.attempt, c.node_id, c.purpose, c.model_requested,"
                        " c.model_used, c.provider_request_id, c.started_at, c.duration_ms,"
                        " c.billable, c.nonbillable_reason, u.usage_status, u.input_tokens,"
                        " u.output_tokens, u.cache_read_input_tokens,"
                        " u.cache_creation_input_tokens,"
                        " u.provider_cost_usd, u.cost_status, u.customer_charge_inr_simulated"
                        " FROM llm_calls c LEFT JOIN usage_records u ON u.llm_call_id = c.id"
                        " WHERE c.run_id = :r ORDER BY c.started_at, c.id"
                    ),
                    {"r": run_id},
                )
            ).all()
        calls: list[UsageCall] = []
        total = Decimal(0)
        any_unknown = False
        for r in rows:
            status: UsageStatus = r.usage_status or "unknown"
            cost_status: CostStatus = r.cost_status or "unknown"
            any_unknown = any_unknown or cost_status == "unknown"
            if r.provider_cost_usd is not None:
                total += Decimal(r.provider_cost_usd)
            calls.append(
                UsageCall(
                    call_id=r.id,
                    attempt=r.attempt,
                    node_id=r.node_id,
                    purpose=r.purpose,
                    provider="anthropic",
                    model_requested=r.model_requested,
                    model_id=r.model_used,
                    fallback_used=r.model_used is not None and r.model_used != r.model_requested,
                    provider_request_id=r.provider_request_id,
                    started_at=r.started_at,
                    duration_ms=r.duration_ms,
                    usage_status=status,
                    token_categories=TokenCategories(
                        input_tokens=r.input_tokens,
                        output_tokens=r.output_tokens,
                        cache_read_input_tokens=r.cache_read_input_tokens,
                        cache_creation_input_tokens=r.cache_creation_input_tokens,
                    ),
                    billable=r.billable,
                    nonbillable_reason=r.nonbillable_reason,
                    provider_cost=Money.usd(r.provider_cost_usd)
                    if r.provider_cost_usd is not None
                    else None,
                    cost_status=cost_status,
                    computed_charge=(
                        Money.inr(r.customer_charge_inr_simulated)
                        if r.customer_charge_inr_simulated is not None
                        else None
                    ),
                    reconciliation_status="not_reconciled (DV-07)",
                )
            )
        return RunUsage(
            run_id=run_id,
            billing_status=run["billing_status"],
            price_version=(run["versions"] or {}).get("rate_card", ""),
            authorized_max_charge=Money.inr(run["authorized_max_charge"]),
            settled_charge=Money.inr(run["settled_charge"])
            if run["settled_charge"] is not None
            else None,
            pending_amount=Money.inr(run["pending_amount"]),
            released_amount=Money.inr(run["released_amount"]),
            provider_cost_total=None if any_unknown else Money.usd(total),
            provider_cost_status="unknown" if any_unknown else "calculated",
            unknown_call_count=sum(1 for c in calls if c.usage_status == "unknown"),
            calls=calls,
        )

    # ------------------------------------------------------------------ activity
    async def activity(
        self, ctx: AccountContext, run_id: UUID, *, after: int, limit: int
    ) -> tuple[list[Activity], int | None]:
        async with self.db.transaction(_ctx(ctx)) as conn:
            await self._owned_run(conn, run_id)
            rows = (
                await conn.execute(
                    text(
                        "SELECT seq, occurred_at, payload FROM run_events WHERE scope_id = :r"
                        " AND event_type = 'activity.appended' AND seq > :a"
                        " ORDER BY seq LIMIT :n"
                    ),
                    {"r": run_id, "a": after, "n": limit + 1},
                )
            ).all()
        more = len(rows) > limit
        rows = rows[:limit]
        items = [
            Activity(
                event_id=event_id(run_id, r.seq),
                sequence=r.seq,
                occurred_at=r.occurred_at,
                agent=r.payload.get("agent"),
                action=r.payload.get("action", ""),
                tool_name=r.payload.get("tool_name"),
                summary=r.payload.get("summary", ""),
                elapsed_ms=r.payload.get("elapsed_ms"),
                retry=r.payload.get("retry", 0),
                validation_status=r.payload.get("validation_status"),
            )
            for r in rows
        ]
        return items, (rows[-1].seq if more and rows else None)

    # ------------------------------------------------------------------ report
    async def report(self, ctx: AccountContext, run_id: UUID) -> Report:
        async with self.db.transaction(_ctx(ctx)) as conn:
            await self._owned_run(conn, run_id)
            row = (
                await conn.execute(
                    text("SELECT report FROM reports WHERE run_id = :r"), {"r": run_id}
                )
            ).first()
        if row is None:
            raise DomainError(
                409,
                ErrorCode.REPORT_NOT_READY,
                "Report not ready",
                "The report is not available yet. Follow the run's events or try again.",
            )
        return Report.model_validate(row.report)
