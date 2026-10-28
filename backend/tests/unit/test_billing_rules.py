"""Money, posting rules, settlement splits and rate-card pricing (pure; Hypothesis properties)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from alloyiq.billing import posting
from alloyiq.billing.posting import PostingError, split_settlement, wallet_account
from alloyiq.billing.rates import RateCard, TokenUsage, UnpricedModelError, price_usage, worst_case
from alloyiq.domain.money import ceil_paise, is_paise, per_mtok, round_paise
from alloyiq.domain.payments import apply_checkout_event

ACC = uuid.uuid4()
money = st.decimals(min_value=Decimal("0.01"), max_value=Decimal("100000"), places=2)
micro_money = st.decimals(min_value=Decimal("0"), max_value=Decimal("5000"), places=6)


def _card(**over: Decimal | None) -> RateCard:
    rates: dict[tuple[str, str], Decimal | None] = {
        ("claude-opus-5-5", "input"): Decimal("4.00"),
        ("claude-opus-5-5", "output"): Decimal("20.00"),
        ("claude-opus-5-5", "cache_read"): Decimal("0.20"),
        ("claude-opus-5-5", "cache_write"): None,
        ("claude-sonnet-5-5", "input"): Decimal("2.00"),
        ("claude-sonnet-5-5", "output"): Decimal("10.00"),
        ("claude-sonnet-5-5", "cache_read"): Decimal("0.20"),
        ("claude-sonnet-5-5", "cache_write"): None,
    }
    for k, v in over.items():
        model, cat = k.split("__")
        rates[(model.replace("_", "-"), cat)] = v
    return RateCard("v-test", "draft", Decimal("85.00"), Decimal("1.25"), rates)


# ------------------------------------------------------------------ money
def test_rounding_rules() -> None:
    assert round_paise(Decimal("1.005")) == Decimal("1.01")  # half-up, not banker's
    assert round_paise(Decimal("1.004999")) == Decimal("1.00")
    assert ceil_paise(Decimal("1.000001")) == Decimal("1.01")
    assert per_mtok(150_000, Decimal("4.00")) == Decimal("0.600000")
    assert is_paise(Decimal("10.50")) and not is_paise(Decimal("10.505"))


# ------------------------------------------------------------------ posting rules
@given(amount=money)
def test_every_journal_balances(amount: Decimal) -> None:
    for j in (
        posting.opening_grant(ACC, amount),
        posting.topup(ACC, uuid.uuid4(), amount),
        posting.refund(ACC, uuid.uuid4(), amount),
        posting.run_debit(ACC, uuid.uuid4(), amount),
        posting.adjustment(ACC, "x", -amount, "test"),
    ):
        assert sum(p.amount for p in j.postings) == 0
        assert len(j.postings) >= 2 and all(p.currency == "INR" for p in j.postings)
        rev = posting.reversal(j, uuid.uuid4())
        assert sum(p.amount for p in rev.postings) == 0
        assert rev.wallet_delta(ACC) == -j.wallet_delta(ACC)


def test_wallet_signs_and_business_ids() -> None:
    cid = uuid.uuid4()
    t = posting.topup(ACC, cid, Decimal("10"))
    assert t.business_event_id == f"topup:{cid}" and t.wallet_delta(ACC) == Decimal("10")
    assert posting.refund(ACC, cid, Decimal("4")).wallet_delta(ACC) == Decimal("-4")
    assert posting.opening_grant(ACC, Decimal("1")).business_event_id == f"opening_grant:{ACC}"
    assert wallet_account(ACC) == f"wallet:{ACC}"


def test_invalid_journals_rejected() -> None:
    with pytest.raises(PostingError):
        posting.topup(ACC, uuid.uuid4(), Decimal("0"))
    with pytest.raises(PostingError):
        posting.run_debit(ACC, uuid.uuid4(), Decimal("-1"))
    with pytest.raises(PostingError):
        posting.adjustment(ACC, "x", Decimal("0.0000001"), "too precise")


# ------------------------------------------------------------------ settlement
@given(reserved=money, charge=micro_money, unknown=st.booleans())
def test_settlement_conserves_the_reservation(
    reserved: Decimal, charge: Decimal, unknown: bool
) -> None:
    s = split_settlement(reserved, charge, unknown_usage=unknown)
    assert s.debit + s.pending + s.released == reserved
    assert min(s.debit, s.pending, s.released) >= 0
    assert s.debit <= reserved and is_paise(s.debit)
    assert s.capped == (round_paise(charge) > reserved)
    if not s.capped:
        assert s.debit == round_paise(charge)
        assert s.debit - s.exact_charge == s.rounding_adjustment
        assert abs(s.rounding_adjustment) <= Decimal("0.005")
    if not unknown:
        assert s.pending == 0


def test_settlement_examples() -> None:
    s = split_settlement(Decimal("25.00"), Decimal("3.455"), unknown_usage=False)
    assert (s.debit, s.released, s.pending) == (Decimal("3.46"), Decimal("21.54"), 0)
    assert s.rounding_adjustment == Decimal("0.005")
    s = split_settlement(Decimal("25.00"), Decimal("3.00"), unknown_usage=True)
    assert (s.debit, s.released, s.pending) == (Decimal("3.00"), 0, Decimal("22.00"))
    s = split_settlement(Decimal("5.00"), Decimal("9.00"), unknown_usage=False)
    assert s.capped and s.debit == Decimal("5.00") and s.released == 0


# ------------------------------------------------------------------ rate cards
def test_priced_usage_is_calculated_and_charged() -> None:
    cost = price_usage(
        _card(), "claude-opus-5-5", TokenUsage(10_000, 2_000, 5_000, 0), billable=True
    )
    # 10k*4 + 2k*20 + 5k*0.20 per MTok = 0.04 + 0.04 + 0.001 USD
    assert cost.cost_status == "calculated" and cost.provider_cost_usd == Decimal("0.081000")
    # customer = provider x 1.25 x 85 per category, micro precision
    assert cost.customer_charge_inr == Decimal("8.606250")
    nonbillable = price_usage(_card(), "claude-opus-5-5", TokenUsage(10_000, 2_000), billable=False)
    assert nonbillable.customer_charge_inr == 0 and nonbillable.provider_cost_usd is not None


def test_unpriced_category_is_unknown_never_zero() -> None:
    cost = price_usage(_card(), "claude-opus-5-5", TokenUsage(1000, 100, 0, 4000), billable=True)
    assert cost.cost_status == "unknown" and cost.provider_cost_usd is None
    assert cost.unpriced == ("cache_write",)
    line = next(ln for ln in cost.lines if ln.category == "cache_write")
    assert line.tokens == 4000 and line.provider_usd is None
    # The member pays only for priced categories; the platform absorbs the rest.
    assert cost.customer_charge_inr == per_mtok(1000, Decimal("425.00")) + per_mtok(
        100, Decimal("2125.00")
    )


def test_missing_usage_is_unknown() -> None:
    cost = price_usage(_card(), "claude-opus-5-5", None, billable=True)
    assert cost.cost_status == "unknown" and cost.provider_cost_usd is None
    assert cost.customer_charge_inr == 0 and cost.lines == ()


def test_worst_case_uses_most_expensive_approved_model() -> None:
    wc = worst_case(
        _card(),
        ["claude-opus-5-5", "claude-sonnet-5-5"],
        input_budget=150_000,
        output_budget=20_000,
    )
    assert wc.pricing_model == "claude-opus-5-5"
    assert wc.real_usd == Decimal("1.000000")  # 0.6 + 0.4
    assert wc.customer_max_inr == Decimal("106.25")
    assert wc.customer_estimate_inr == Decimal("42.50")
    with pytest.raises(UnpricedModelError):
        worst_case(_card(), ["claude-haiku-5-5"], input_budget=1, output_budget=1)


# ------------------------------------------------------------------ checkout lifecycle
NOW = datetime(2026, 10, 28, tzinfo=UTC)
EXP = NOW + timedelta(minutes=30)
EVENTS = ["checkout.pending", "checkout.paid", "checkout.failed", "checkout.expired"]


@given(st.lists(st.sampled_from(EVENTS), max_size=12))
def test_any_event_order_credits_at_most_once(events: list[str]) -> None:
    status, credits = "created", 0
    for e in events:
        t = apply_checkout_event(status, e, received_at=NOW, expires_at=EXP)  # type: ignore[arg-type]
        status, credits = t.status, credits + t.credit
    assert credits <= 1
    assert credits == 1 if status == "paid" else credits == 0
    first_final = next(
        (e for e in events if e in ("checkout.paid", "checkout.failed", "checkout.expired")), None
    )
    if first_final:
        assert status == first_final.split(".")[1]


def test_success_after_expiry_expires_instead() -> None:
    t = apply_checkout_event(
        "created", "checkout.paid", received_at=EXP + timedelta(seconds=1), expires_at=EXP
    )
    assert t.status == "expired" and not t.credit
