"""Run state machine (R024-R026, D048-D049)."""

from __future__ import annotations

import itertools

from hypothesis import given
from hypothesis import strategies as st

from alloyiq.domain.runs import TERMINAL, TRANSITIONS, can_transition, finish_status, sources_for

STATES = [
    "queued",
    "running",
    "cancel_requested",
    "succeeded",
    "incomplete",
    "failed",
    "cancelled",
    "timed_out",
]


def test_documented_transitions() -> None:
    assert can_transition("queued", "running") and can_transition("queued", "timed_out")
    assert can_transition("running", "succeeded") and can_transition(
        "cancel_requested", "cancelled"
    )
    assert not can_transition("cancel_requested", "succeeded")  # a cancel is never overwritten
    assert not can_transition("queued", "succeeded")  # no result without running
    assert sources_for("cancelled") == ("cancel_requested",)
    assert sources_for("cancel_requested") == ("queued", "running")


def test_terminal_states_are_final() -> None:
    for t, target in itertools.product(TERMINAL, STATES):
        assert not can_transition(t, target)
    assert set(TRANSITIONS) | TERMINAL == set(STATES)


@given(st.lists(st.sampled_from(STATES), max_size=10))
def test_any_walk_ends_terminal_at_most_once(targets: list[str]) -> None:
    status, terminal_hits = "queued", 0
    for t in targets:
        if can_transition(status, t):
            status = t
            terminal_hits += t in TERMINAL
    assert terminal_hits <= 1


def test_cancel_seen_always_resolves_cancelled() -> None:
    assert finish_status("succeeded", cancel_seen=True) == "cancelled"
    assert finish_status("incomplete", cancel_seen=False) == "incomplete"
