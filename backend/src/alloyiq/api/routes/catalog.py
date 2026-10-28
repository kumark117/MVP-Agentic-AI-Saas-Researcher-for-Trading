"""Catalogue routes (Design section 13; R014-R017, D019)."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Query, Request

from alloyiq.api.deps import Auth, Svc
from alloyiq.api.envelope import Envelope, ok
from alloyiq.api.schemas.catalog import (
    Catalog,
    FreshnessPolicy,
    Instrument,
    ModelProfile,
    Plan,
    RateCard,
    RateItem,
    RunLimits,
    UnavailableCombination,
)
from alloyiq.api.schemas.common import Money
from alloyiq.application.catalog import RUN_LIMITS, CatalogService, customer_rate, data_mode_label

router = APIRouter(prefix="/catalog", tags=["catalog"])


def _catalog(svc: Svc) -> CatalogService:
    service: CatalogService = svc.extra["catalog"]
    return service


@router.get("/instruments", response_model=Envelope[Catalog])
async def instruments(
    request: Request,
    svc: Svc,
    _: Auth,
    metal: str | None = None,
    exchange: str | None = None,
    available_only: bool = Query(False),
) -> Envelope[Catalog]:
    items, unavailable = await _catalog(svc).instruments()
    out = []
    for i in items:
        if (
            (metal and i.metal != metal)
            or (exchange and i.venue != exchange)
            or (available_only and not i.enabled)
        ):
            continue
        out.append(
            Instrument(
                instrument_id=i.id,
                canonical_id=i.canonical_id,
                display_name=i.display_name,
                metal=i.metal,
                venue=i.venue,
                data_mode=i.data_mode,  # type: ignore[arg-type]
                data_mode_label=data_mode_label(i.data_mode),
                instrument_type=i.instrument_type,  # type: ignore[arg-type]
                contract_expiry=i.contract_expiry,
                quote_currency=i.quote_currency,
                unit=i.unit,
                timezone=i.timezone,
                supported_horizons=i.supported_horizons,  # type: ignore[arg-type]
                freshness_policy=FreshnessPolicy(**i.freshness_policy),
                enabled=i.enabled,
                unavailable_reason=i.unavailable_reason,
            )
        )
    combos = [
        UnavailableCombination(metal=u.metal, venue=u.venue, reason=u.reason)
        for u in unavailable
        if (not metal or u.metal == metal) and (not exchange or u.venue == exchange)
    ]
    return ok(request, Catalog(instruments=out, unavailable=[] if available_only else combos))


@router.get("/models", response_model=Envelope[list[ModelProfile]])
async def models(request: Request, svc: Svc, _: Auth) -> Envelope[list[ModelProfile]]:
    profiles, card = await _catalog(svc).model_profiles()
    out = [
        ModelProfile(
            model_profile_id=p["id"],
            slug=p["slug"],
            display_name=p["display_name"],
            provider="anthropic",
            primary_model_id=p["primary_model_id"],
            approved_fallback_model_ids=p["approved_fallback_model_ids"],
            fallback_policy=(
                "On refusal or overload, retry only on: "
                + ", ".join(p["approved_fallback_model_ids"])
                if p["approved_fallback_model_ids"]
                else "No fallback model; the run fails "
                "explicitly if the primary model is unavailable"
            ),
            branch_effort=p["branch_effort"],
            signal_effort=p["signal_effort"],
            context_limit_tokens=p["context_limit_tokens"],
            run_input_token_budget=p["run_input_token_budget"],
            run_output_token_budget=p["run_output_token_budget"],
            max_tokens={
                "branch": p["max_tokens_branch"],
                "signal": p["max_tokens_signal"],
                "repair": p["max_tokens_repair"],
            },
            limits=RunLimits(**RUN_LIMITS),
            rate_card_version=card["version"] if card else None,
            is_default=p["is_default"],
        )
        for p in profiles
    ]
    return ok(request, out)


@router.get("/prices", response_model=Envelope[RateCard | None])
async def prices(request: Request, svc: Svc, _: Auth) -> Envelope[RateCard | None]:
    card = await _catalog(svc).rate_card()
    if card is None:
        return ok(request, None)
    fx, markup = Decimal(card["demo_fx_usd_inr"]), Decimal(card["demo_markup"])
    items = [
        RateItem(
            model_id=i["model_id"],
            token_category=i["token_category"],
            provider_usd_per_mtok=Money.usd(i["provider_usd_per_mtok"])
            if i["provider_usd_per_mtok"] is not None
            else None,
            customer_inr_per_mtok_simulated=Money.inr(c)
            if (c := customer_rate(i["provider_usd_per_mtok"], fx, markup)) is not None
            else None,
            verified=i["verified"],
        )
        for i in card["items"]
    ]
    note = (
        "Simulated customer rates = provider USD rate x demo markup x demo FX assumption. "
        "Not a market FX rate; no real money is charged."
    )
    if card["status"] == "draft":
        note = "DRAFT rate card awaiting owner verification (OD-3). " + note
    return ok(
        request,
        RateCard(
            version=card["version"],
            status=card["status"],
            effective_from=card["effective_from"],
            effective_to=card["effective_to"],
            demo_fx_usd_inr=format(fx, "f"),
            demo_markup=format(markup, "f"),
            fx_rule=card["fx_rule"],
            note=note,
            items=items,
        ),
    )


@router.get("/plans", response_model=Envelope[list[Plan]])
async def plans(request: Request, svc: Svc) -> Envelope[list[Plan]]:
    rows = await _catalog(svc).plans()
    return ok(
        request,
        [
            Plan(
                plan_id=p["id"],
                slug=p["slug"],
                display_name=p["display_name"],
                active_runs=p["active_runs"],
                queued_runs=p["queued_runs"],
                sse_streams=p["sse_streams"],
                daily_topup_limit=Money.inr(p["daily_topup_limit_inr"]),
                fixed_fee=Money.inr("0.00"),
                note="Simulated demo plan. No subscription is purchased; usage is not unlimited.",
            )
            for p in rows
        ],
    )
