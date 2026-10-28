"""Research, run, report, usage and activity boundary models (Design sections 14, 19; D105-D108)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from alloyiq.api.schemas.catalog import RunLimits
from alloyiq.api.schemas.common import (
    BillingStatus,
    CostStatus,
    Horizon,
    Money,
    Out,
    PositiveMoney,
    RunStatus,
    Signal,
    SimulationFields,
    Strict,
    UsageStatus,
)


class EstimateRequest(Strict):
    instrument_id: UUID
    horizon: Horizon
    model_profile_id: UUID
    question: str | None = Field(default=None, max_length=4000)
    conversation_id: UUID | None = None


class Estimate(SimulationFields):
    estimate_id: UUID
    request_fingerprint: str
    normalized_request: dict[str, object]
    model_profile_id: UUID
    primary_model_id: str
    approved_models: list[str]
    limits: RunLimits
    token_budget: dict[str, int]
    price_version: str
    min_charge: Money
    estimated_charge: Money
    max_charge: Money
    estimate_basis: str
    max_real_cost: Money  # real USD ceiling for this run, shown for transparency
    expires_at: datetime


class StartRunRequest(Strict):
    estimate_id: UUID
    max_charge: PositiveMoney


class RunAccepted(SimulationFields):
    run_id: UUID
    status: RunStatus
    billing_status: BillingStatus
    reserved_amount: Money
    status_url: str
    events_url: str


class Progress(Out):
    completed_nodes: int
    total_nodes: int
    active_nodes: list[str]


class Versions(Out):
    graph: str
    prompt: str
    policy: str
    rate_card: str
    application: str


class RunSnapshot(SimulationFields):
    run_id: UUID
    conversation_id: UUID | None
    instrument_id: UUID
    instrument_snapshot: dict[str, object]
    horizon: Horizon
    model_profile_id: UUID
    status: RunStatus
    billing_status: BillingStatus
    outcome_reason: str | None
    error_code: str | None
    submitted_at: datetime
    started_at: datetime | None
    cancel_requested_at: datetime | None
    terminal_at: datetime | None
    queue_ms: int | None
    execution_ms: int | None
    end_to_end_ms: int | None
    time_to_first_activity_ms: int | None
    time_to_first_model_token_ms: int | None
    latest_event_id: str | None
    report_available: bool
    progress: Progress
    authorized_max_charge: Money
    reserved_amount: Money
    settled_charge: Money | None
    pending_amount: Money
    released_amount: Money
    versions: Versions
    observability_gap: bool


class RunSummary(Out):
    run_id: UUID
    instrument_id: UUID
    instrument_display_name: str
    horizon: Horizon
    model_profile_id: UUID
    status: RunStatus
    billing_status: BillingStatus
    signal: Signal | None
    submitted_at: datetime
    terminal_at: datetime | None
    end_to_end_ms: int | None
    settled_charge: Money | None


class CancelRequest(Strict):
    reason: str | None = Field(default=None, max_length=500)


# ------------------------------------------------------------------ report (D021, section 14)
class Confidence(Out):
    score: float | None
    label: Literal["low", "medium", "high"] | None
    calibration_status: Literal["uncalibrated"] = "uncalibrated"


class Claim(Out):
    claim: str
    evidence_ids: list[UUID]


class Citation(Out):
    evidence_id: UUID
    source_title: str
    source_url: str | None
    observed_at: datetime
    content_hash: str


class ValidationCheck(Out):
    name: str
    passed: bool
    detail: str | None = None


class ValidationSummary(Out):
    checks: list[ValidationCheck]
    passed: bool
    repaired: bool


class Report(Out):
    report_id: UUID
    run_id: UUID
    instrument_snapshot: dict[str, object]
    horizon: Horizon
    signal: Signal
    complete: bool
    confidence: Confidence
    summary: str
    supporting_evidence: list[Claim]
    opposing_evidence: list[Claim]
    assumptions: list[str]
    risks: list[str]
    limitations: list[str]
    missing_evidence: list[str]
    citations: list[Citation]
    data_as_of: datetime | None
    freshness_state: str
    validation_summary: ValidationSummary
    research_only_disclosure: str
    created_at: datetime


# ------------------------------------------------------------------ usage (D107, D113)
class TokenCategories(Out):
    input_tokens: int | None
    output_tokens: int | None
    cache_read_input_tokens: int | None
    cache_creation_input_tokens: int | None


class UsageCall(Out):
    call_id: UUID
    attempt: int
    node_id: str
    purpose: str
    provider: Literal["anthropic"]
    model_requested: str
    model_id: str | None
    fallback_used: bool
    provider_request_id: str | None
    started_at: datetime
    duration_ms: int | None
    usage_status: UsageStatus
    token_categories: TokenCategories
    billable: bool
    nonbillable_reason: str | None
    provider_cost: Money | None  # real USD
    cost_status: CostStatus
    computed_charge: Money | None  # simulated INR customer charge
    reconciliation_status: str


class RunUsage(SimulationFields):
    run_id: UUID
    billing_status: BillingStatus
    price_version: str
    authorized_max_charge: Money
    settled_charge: Money | None
    pending_amount: Money
    released_amount: Money
    provider_cost_total: Money | None
    provider_cost_status: CostStatus
    unknown_call_count: int
    calls: list[UsageCall]


class Activity(Out):
    event_id: str
    sequence: int
    occurred_at: datetime
    agent: str | None
    action: str
    tool_name: str | None
    summary: str
    elapsed_ms: int | None
    retry: int
    validation_status: str | None


class FeedbackRequest(Strict):
    rating: Literal["positive", "negative"]
    comment: str | None = Field(default=None, max_length=2000)


class Feedback(Out):
    feedback_id: UUID
    run_id: UUID
    rating: Literal["positive", "negative"]
    comment: str | None
    updated_at: datetime
