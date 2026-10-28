"""Catalogue facts that are code, not configuration (build prompt section 11; R014, D004)."""

from __future__ import annotations

from dataclasses import dataclass

CANDIDATE_METALS = ("gold", "silver", "copper", "zinc", "uranium")
CANDIDATE_VENUES = ("MCX", "COMEX", "LME")

REFERENCE_BADGE = "Reference data: delayed or derived; not a live exchange quote."

DATA_MODE_LABELS = {
    "exchange_live": "Exchange live data",
    "exchange_delayed": "Exchange delayed data",
    "reference": REFERENCE_BADGE,
}


@dataclass(frozen=True, slots=True)
class Unavailable:
    metal: str
    venue: str
    reason: str


def unavailable_combinations(available: set[tuple[str, str]]) -> list[Unavailable]:
    """Every candidate metal x venue pair with no enabled instrument, with a reason (R014).

    Never infer that a metal trades on a venue: a pair is offered only when an instrument
    row for it exists and is enabled.
    """
    out: list[Unavailable] = []
    for venue in CANDIDATE_VENUES:
        for metal in CANDIDATE_METALS:
            if (metal, venue) in available:
                continue
            if venue == "MCX":
                reason = "No licensed MCX feed configured"
            elif venue == "LME":
                reason = "No licensed LME feed configured"
            elif metal in ("zinc", "uranium"):
                reason = "No verified data source"
            else:
                reason = "Instrument not verified by the adapter acceptance test"
            out.append(Unavailable(metal, venue, reason))
    return out
