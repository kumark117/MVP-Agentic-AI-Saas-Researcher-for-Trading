"""Metrics and report boundary models (Design section 19, D106; R044-R048)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from alloyiq.api.schemas.common import CostStatus, Money, Out, SimulationFields


class LatencyDistribution(Out):
    sample_count: int
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    population: str
    method: Literal["exact"] = "exact"
    approximate: Literal[False] = False
    low_sample: bool


class Counts(Out):
    submitted: int
    queued: int
    running: int
    succeeded: int
    incomplete: int
    failed: int
    cancelled: int
    timed_out: int
    queue_expired: int
    pending_billing: int


class SuccessRate(Out):
    numerator: int
    denominator: int
    value: float | None
    definition: str = "succeeded / (succeeded + incomplete + failed + timed_out)"


class Latency(Out):
    success_end_to_end: LatencyDistribution
    all_terminal_end_to_end: LatencyDistribution
    queue: LatencyDistribution
    execution: LatencyDistribution


class Usage(Out):
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    unknown_call_count: int
    note: str = "Thinking tokens are included in output_tokens; categories do not overlap."


class ProviderCost(Out):
    amount: Money
    cost_status: CostStatus
    unknown_call_count: int


class LatestRun(Out):
    run_id: UUID
    status: str
    submitted_at: datetime
    end_to_end_ms: int | None
    billing_status: str
    model_ids: list[str]


class WindowBasis(Out):
    counts: Literal["submitted_at"] = "submitted_at"
    outcomes: Literal["terminal_at"] = "terminal_at"
    financial: Literal["posted_at"] = "posted_at"


class Summary(SimulationFields):
    interval: dict[str, datetime]
    timezone: Literal["UTC"] = "UTC"
    data_as_of: datetime
    filters: dict[str, str | None]
    window_basis: WindowBasis
    counts: Counts
    success_rate: SuccessRate
    latency: Latency
    usage: Usage
    provider_cost: ProviderCost
    customer_charge: Money
    retries: int
    tool_errors: int
    validation_failures: int
    security_blocks: int
    cache_hits: int
    latest_run: LatestRun | None
    daily: list[DailyPoint]
    warnings: list[str]


class DailyPoint(Out):
    day: str
    submitted: int
    succeeded: int
    provider_cost_usd: str
    customer_charge_inr: str


class ModelRow(Out):
    model_id: str
    calls: int
    usage: Usage
    provider_cost: ProviderCost
    customer_charge: Money
    success_rate: SuccessRate
    latency: LatencyDistribution


class ModelsReport(SimulationFields):
    interval: dict[str, datetime]
    data_as_of: datetime
    models: list[ModelRow]
    warnings: list[str]


Summary.model_rebuild()
