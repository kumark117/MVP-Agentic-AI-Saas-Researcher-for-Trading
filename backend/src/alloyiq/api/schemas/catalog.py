"""Catalogue boundary models (Design section 13; R014-R017, D019)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from alloyiq.api.schemas.common import Horizon, Money, Out

DataMode = Literal["exchange_live", "exchange_delayed", "reference"]
InstrumentType = Literal["spot", "futures_contract", "futures_continuous", "reference_series"]


class FreshnessPolicy(Out):
    max_quote_age_s: dict[str, int]  # per horizon
    expected_delay: str


class Instrument(Out):
    instrument_id: UUID
    canonical_id: str
    display_name: str
    metal: str
    venue: str
    data_mode: DataMode
    data_mode_label: str
    instrument_type: InstrumentType
    contract_expiry: date | None
    quote_currency: str
    unit: str
    timezone: str
    supported_horizons: list[Horizon]
    freshness_policy: FreshnessPolicy
    enabled: bool
    unavailable_reason: str | None


class UnavailableCombination(Out):
    metal: str
    venue: str
    reason: str


class Catalog(Out):
    instruments: list[Instrument]
    unavailable: list[UnavailableCombination]


class ModelProfile(Out):
    model_profile_id: UUID
    slug: str
    display_name: str
    provider: Literal["anthropic"]
    primary_model_id: str
    approved_fallback_model_ids: list[str]
    fallback_policy: str
    branch_effort: str
    signal_effort: str
    context_limit_tokens: int
    run_input_token_budget: int
    run_output_token_budget: int
    max_tokens: dict[str, int]
    limits: RunLimits
    rate_card_version: str | None
    is_default: bool


class RunLimits(Out):
    run_model_calls: int
    run_tool_calls: int
    run_active_seconds: int
    branch_model_calls: int
    branch_tool_calls: int
    structured_output_repairs: int


class RateItem(Out):
    model_id: str
    token_category: Literal["input", "output", "cache_read", "cache_write"]
    provider_usd_per_mtok: Money | None  # null: not yet verified
    customer_inr_per_mtok_simulated: Money | None
    verified: bool


class RateCard(Out):
    version: str
    status: Literal["draft", "active", "retired"]
    effective_from: datetime | None
    effective_to: datetime | None
    demo_fx_usd_inr: str
    demo_markup: str
    fx_rule: str
    note: str
    items: list[RateItem]


class Plan(Out):
    plan_id: UUID
    slug: str
    display_name: str
    active_runs: int
    queued_runs: int
    sse_streams: int
    daily_topup_limit: Money
    fixed_fee: Money
    note: str


ModelProfile.model_rebuild()
