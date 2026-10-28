"""Rate-card pricing (D056, D057; build prompt sections 8.2, 9.3). Pure: no I/O.

Token categories follow the Anthropic usage object, whose fields do not overlap:
input_tokens (uncached input), cache_read_input_tokens, cache_creation_input_tokens and
output_tokens (thinking tokens are already inside output). Nothing is double-counted.

A category with tokens but no verified price is never priced at zero: that portion is
`unknown`, the whole call's provider cost becomes cost_status=unknown with a null total,
and the member is not charged for the unpriced portion (the platform absorbs it, R043).
Prompt caching is only enabled for a model whose cache-write price is on the pinned card
(ADR-014), so worst-case estimates need only the input and output rates.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from alloyiq.domain.money import ZERO, ceil_paise, micro, per_mtok

TokenCategory = Literal["input", "output", "cache_read", "cache_write"]
CATEGORIES: tuple[TokenCategory, ...] = ("input", "output", "cache_read", "cache_write")
CostStatus = Literal["calculated", "reconciled", "unknown"]


@dataclass(frozen=True, slots=True)
class RateCard:
    version: str
    status: str
    demo_fx_usd_inr: Decimal
    demo_markup: Decimal
    rates: dict[tuple[str, str], Decimal | None]  # (model_id, category) -> USD per MTok

    @classmethod
    def from_row(cls, card: dict[str, Any]) -> RateCard:
        return cls(
            version=card["version"],
            status=card["status"],
            demo_fx_usd_inr=Decimal(card["demo_fx_usd_inr"]),
            demo_markup=Decimal(card["demo_markup"]),
            rates={
                (i["model_id"], i["token_category"]): (
                    Decimal(i["provider_usd_per_mtok"])
                    if i["provider_usd_per_mtok"] is not None
                    else None
                )
                for i in card["items"]
            },
        )

    def rate(self, model_id: str, category: TokenCategory) -> Decimal | None:
        return self.rates.get((model_id, category))

    def knows(self, model_id: str) -> bool:
        return any(m == model_id for m, _ in self.rates)

    def caching_priced(self, model_id: str) -> bool:
        return (
            self.rate(model_id, "cache_write") is not None
            and self.rate(model_id, "cache_read") is not None
        )

    def customer_rate(self, provider_usd_per_mtok: Decimal) -> Decimal:
        """Simulated INR per MTok: provider rate x demo markup x demo FX (demo assumptions)."""
        return micro(provider_usd_per_mtok * self.demo_markup * self.demo_fx_usd_inr)


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Measured usage of one call. None means the provider did not report it (unknown)."""

    input_tokens: int | None
    output_tokens: int | None
    cache_read_input_tokens: int | None = 0
    cache_creation_input_tokens: int | None = 0

    def by_category(self) -> dict[TokenCategory, int | None]:
        return {
            "input": self.input_tokens,
            "output": self.output_tokens,
            "cache_read": self.cache_read_input_tokens,
            "cache_write": self.cache_creation_input_tokens,
        }


@dataclass(frozen=True, slots=True)
class CostLine:
    category: TokenCategory
    tokens: int | None
    provider_usd: Decimal | None  # None: unpriced or unknown tokens
    customer_inr: Decimal | None


@dataclass(frozen=True, slots=True)
class CallCost:
    lines: tuple[CostLine, ...]
    provider_cost_usd: Decimal | None  # null unless every non-zero category was priced
    cost_status: CostStatus
    customer_charge_inr: Decimal  # simulated; priced categories of billable calls only
    unpriced: tuple[TokenCategory, ...]


def price_usage(
    card: RateCard, model_id: str, usage: TokenUsage | None, *, billable: bool
) -> CallCost:
    """Provider cost (real USD, calculated) and simulated customer charge (INR) of one call.

    `usage=None` (an interrupted stream, an ambiguous call) is unknown: no tokens are
    invented, cost is unknown and nothing is charged to the member.
    """
    if usage is None:
        return CallCost((), None, "unknown", ZERO, CATEGORIES)
    lines: list[CostLine] = []
    unpriced: list[TokenCategory] = []
    known_usd = ZERO
    charge = ZERO
    for cat, tokens in usage.by_category().items():
        rate = card.rate(model_id, cat)
        if tokens is None:
            lines.append(CostLine(cat, None, None, None))
            unpriced.append(cat)
            continue
        if tokens == 0:
            lines.append(CostLine(cat, 0, ZERO, ZERO))
            continue
        if rate is None:
            lines.append(CostLine(cat, tokens, None, None))
            unpriced.append(cat)
            continue
        usd = per_mtok(tokens, rate)
        inr = per_mtok(tokens, card.customer_rate(rate))
        known_usd += usd
        charge += inr
        lines.append(CostLine(cat, tokens, usd, inr))
    status: CostStatus = "unknown" if unpriced else "calculated"
    return CallCost(
        lines=tuple(lines),
        provider_cost_usd=None if unpriced else known_usd,
        cost_status=status,
        customer_charge_inr=charge if billable else ZERO,
        unpriced=tuple(unpriced),
    )


class UnpricedModelError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WorstCase:
    real_usd: Decimal  # worst-case real provider spend, micro precision
    customer_max_inr: Decimal  # simulated ceiling, rounded up to the paisa
    customer_estimate_inr: Decimal  # typical case (labelled an estimate)
    customer_min_inr: Decimal
    pricing_model: str  # the approved model that sets the worst case


def worst_case(
    card: RateCard,
    approved_models: list[str],
    *,
    input_budget: int,
    output_budget: int,
    typical_fraction: Decimal = Decimal("0.40"),
) -> WorstCase:
    """Worst case over every approved model (a fallback may be the expensive one)."""
    if not approved_models:
        raise UnpricedModelError("no approved models")
    best: tuple[Decimal, Decimal, str] | None = None
    for model in approved_models:
        rin, rout = card.rate(model, "input"), card.rate(model, "output")
        if rin is None or rout is None:
            raise UnpricedModelError(f"{model} has no input/output price on {card.version}")
        usd = per_mtok(input_budget, rin) + per_mtok(output_budget, rout)
        inr = per_mtok(input_budget, card.customer_rate(rin)) + per_mtok(
            output_budget, card.customer_rate(rout)
        )
        if best is None or usd > best[0]:
            best = (usd, inr, model)
    assert best is not None  # noqa: S101 - approved_models is non-empty
    usd, inr, model = best
    max_inr = ceil_paise(inr)
    est = min(ceil_paise(inr * typical_fraction), max_inr)
    return WorstCase(usd, max_inr, est, min(ceil_paise(ZERO), est), model)
