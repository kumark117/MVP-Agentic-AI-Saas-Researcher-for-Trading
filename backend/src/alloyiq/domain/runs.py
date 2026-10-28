"""Run lifecycle state machine (R024-R026, D048-D050). Pure: no I/O.

queued           -> running | cancel_requested | timed_out
running          -> cancel_requested | succeeded | incomplete | failed | timed_out
cancel_requested -> cancelled, or a terminal state that was committed first (the guarded
                    UPDATE decides: execution can never overwrite a committed cancel)
Terminal: succeeded, incomplete, failed, cancelled, timed_out. Billing status is separate.
"""

from __future__ import annotations

from typing import Literal

RunStatus = Literal[
    "queued",
    "running",
    "cancel_requested",
    "succeeded",
    "incomplete",
    "failed",
    "cancelled",
    "timed_out",
]
BillingStatus = Literal["reserved", "pending_settlement", "settled", "released", "disputed"]

TERMINAL: frozenset[str] = frozenset(
    {"succeeded", "incomplete", "failed", "cancelled", "timed_out"}
)
ACTIVE: frozenset[str] = frozenset({"running", "cancel_requested"})

TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancel_requested", "timed_out"}),
    "running": frozenset({"cancel_requested", "succeeded", "incomplete", "failed", "timed_out"}),
    "cancel_requested": frozenset({"cancelled"}),
}


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


def sources_for(target: str) -> tuple[str, ...]:
    """Statuses from which `target` is reachable: the WHERE status IN (...) of the CAS."""
    return tuple(sorted(s for s, ts in TRANSITIONS.items() if target in ts))


def is_terminal(status: str) -> bool:
    return status in TERMINAL


def finish_status(outcome: str, *, cancel_seen: bool) -> str:
    """The terminal state a worker asks for. Once cancellation was observed, the run
    resolves to cancelled whatever the graph produced."""
    return "cancelled" if cancel_seen else outcome


# Graph nodes reported in RunSnapshot.progress (D032). Kept here so P5's fake graph and
# P6's LangGraph agree on the total.
GRAPH_NODES: tuple[str, ...] = (
    "validate_request",
    "policy_check",
    "price_branch",
    "news_branch",
    "technical_branch",
    "merge_evidence",
    "handoff_validation",
    "evidence_minimum_gate",
    "signal_synthesis",
    "final_validation",
    "persist_report",
    "settle",
)
