"""Demo ledger posting rules (D058, D062, D063). Pure functions: no I/O.

Sign convention: a positive posting on `wallet:{account_id}` is demo credit to the member;
every journal's postings sum to zero (the database trigger re-checks this before commit).

| kind          | wallet        | counter-account     |
|---------------|---------------|---------------------|
| opening_grant | +amount       | sim_topup_source -  |
| topup         | +amount       | sim_topup_source -  |
| adjustment    | +/- amount    | sim_adjustments     |
| run_debit     | -charge       | sim_usage_income +  |
| refund        | -amount       | sim_refunds +       |
| reversal      | negation of the reversed journal's postings |

None of these accounts is revenue or cash: demo journals are never aggregated with real
provider expense (D063).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
from uuid import UUID

from alloyiq.domain.money import ZERO, micro, round_paise

LedgerKind = Literal["topup", "run_debit", "refund", "adjustment", "reversal", "opening_grant"]

SIM_TOPUP_SOURCE = "sim_topup_source"
SIM_USAGE_INCOME = "sim_usage_income"
SIM_REFUNDS = "sim_refunds"
SIM_ADJUSTMENTS = "sim_adjustments"


def wallet_account(account_id: UUID) -> str:
    return f"wallet:{account_id}"


@dataclass(frozen=True, slots=True)
class Posting:
    ledger_account: str
    amount: Decimal
    currency: str = "INR"


@dataclass(frozen=True, slots=True)
class Journal:
    kind: LedgerKind
    business_event_id: str
    postings: tuple[Posting, ...]
    description: str
    currency: str = "INR"

    def wallet_delta(self, account_id: UUID) -> Decimal:
        acct = wallet_account(account_id)
        return sum((p.amount for p in self.postings if p.ledger_account == acct), ZERO)


class PostingError(ValueError):
    pass


def _check(journal: Journal) -> Journal:
    if len(journal.postings) < 2:
        raise PostingError("a journal needs at least two postings")
    if any(p.amount == 0 for p in journal.postings):
        raise PostingError("postings must be non-zero")
    if any(p.currency != journal.currency for p in journal.postings):
        raise PostingError("all postings must use the journal currency")
    if sum((p.amount for p in journal.postings), ZERO) != 0:
        raise PostingError("journal does not balance")
    if any(p.amount != micro(p.amount) for p in journal.postings):
        raise PostingError("postings are limited to 6 decimal places")
    return journal


def _positive(amount: Decimal) -> Decimal:
    if amount <= 0:
        raise PostingError("amount must be positive")
    return amount


def opening_grant(account_id: UUID, amount: Decimal) -> Journal:
    a = _positive(amount)
    return _check(
        Journal(
            "opening_grant",
            f"opening_grant:{account_id}",
            (Posting(wallet_account(account_id), a), Posting(SIM_TOPUP_SOURCE, -a)),
            "Opening demo credit (simulated, no cash value)",
        )
    )


def topup(account_id: UUID, checkout_id: UUID, amount: Decimal) -> Journal:
    a = _positive(amount)
    return _check(
        Journal(
            "topup",
            f"topup:{checkout_id}",
            (Posting(wallet_account(account_id), a), Posting(SIM_TOPUP_SOURCE, -a)),
            "Simulated top-up (no money collected)",
        )
    )


def refund(account_id: UUID, refund_id: UUID, amount: Decimal) -> Journal:
    a = _positive(amount)
    return _check(
        Journal(
            "refund",
            f"refund:{refund_id}",
            (Posting(wallet_account(account_id), -a), Posting(SIM_REFUNDS, a)),
            "Simulated refund (reverses demo credit only)",
        )
    )


def adjustment(account_id: UUID, event_id: str, amount: Decimal, reason: str) -> Journal:
    if amount == 0:
        raise PostingError("adjustment must be non-zero")
    return _check(
        Journal(
            "adjustment",
            f"adjustment:{event_id}",
            (Posting(wallet_account(account_id), amount), Posting(SIM_ADJUSTMENTS, -amount)),
            f"Operator adjustment: {reason}"[:300],
        )
    )


def run_debit(account_id: UUID, run_id: UUID, charge: Decimal) -> Journal:
    c = _positive(charge)
    return _check(
        Journal(
            "run_debit",
            f"run_debit:{run_id}",
            (Posting(wallet_account(account_id), -c), Posting(SIM_USAGE_INCOME, c)),
            "Simulated research charge (no money collected)",
        )
    )


def reversal(original: Journal, original_id: UUID) -> Journal:
    """A correction never edits a journal: it posts the exact negation (D063)."""
    return _check(
        Journal(
            "reversal",
            f"reversal:{original_id}",
            tuple(Posting(p.ledger_account, -p.amount, p.currency) for p in original.postings),
            f"Reversal of {original.business_event_id}",
            original.currency,
        )
    )


# ------------------------------------------------------------------ run settlement
@dataclass(frozen=True, slots=True)
class SettlementSplit:
    """How a run's demo reservation is resolved (build prompt section 8.7)."""

    exact_charge: Decimal  # sum of simulated charges of billable calls, micro precision
    debit: Decimal  # posted, rounded once to the paisa (and capped at the reservation)
    rounding_adjustment: Decimal  # debit - exact (before any cap)
    pending: Decimal  # bounded hold kept while some usage is unknown
    released: Decimal  # returned to the member immediately
    capped: bool  # True if the charge exceeded the reservation (platform absorbs the rest)


def split_settlement(
    reserved: Decimal, exact_charge: Decimal, *, unknown_usage: bool
) -> SettlementSplit:
    """Resolve a reservation into debit + pending + release, never exceeding the hold.

    With unknown usage, the remaining reservation stays pending (bounded by what was
    reserved) for up to 24 hours, after which a scheduler releases it (D061).
    """
    if reserved <= 0:
        raise PostingError("reservation must be positive")
    if exact_charge < 0:
        raise PostingError("charge cannot be negative")
    rounded = round_paise(exact_charge)
    capped = rounded > reserved
    debit = min(rounded, reserved)
    remainder = reserved - debit
    pending = remainder if unknown_usage else ZERO
    return SettlementSplit(
        exact_charge=exact_charge,
        debit=debit,
        rounding_adjustment=rounded - exact_charge,
        pending=pending,
        released=remainder - pending,
        capped=capped,
    )
