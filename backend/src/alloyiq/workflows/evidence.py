"""EvidenceValidator, evidence normalisation, handoff checks and the evidence-minimum gate
(R021-R022, R049-R052; D039-D047, D108). Pure functions over tool envelopes: no I/O.

Verdicts are typed with reasons, never a bare boolean. Numbers in evidence come only from
validated tool envelopes; the model explains them and never supplies them.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from alloyiq.domain.market import history_plan
from alloyiq.security import injection
from alloyiq.tools.schemas import ToolEnvelope

VALIDATOR_VERSION = "evidence-validator-1"
MAX_FUTURE_SKEW = timedelta(minutes=2)
Verdict = Literal["pass", "fail", "quarantine"]
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    verdict: Verdict
    reasons: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"


def _hash(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def validate_envelope(
    env: ToolEnvelope,
    *,
    tool: str,
    instrument: dict[str, Any],
    now: datetime,
) -> ValidationOutcome:
    """D040: HTTP 200 or parseable JSON is not success; check the envelope semantics."""
    reasons: list[str] = []
    if env.schema_version != "1.0":
        reasons.append("schema_version")
    if env.tool != tool:
        reasons.append("tool_mismatch")
    if env.status != "ok" or env.error is not None or env.data is None:
        reasons.append(f"tool_error:{env.error.code if env.error else 'no_data'}")
        return ValidationOutcome("fail", tuple(reasons))
    if not env.provenance_hash or not _HEX64.match(env.provenance_hash):
        reasons.append("provenance_hash")
    if env.observed_at is not None and env.observed_at > now + MAX_FUTURE_SKEW:
        reasons.append("observed_in_future")
    if tool != "get_fx_rate" and str(env.instrument_id) != str(instrument["instrument_id"]):
        reasons.append("instrument_mismatch")
    if tool in ("get_quote", "get_history", "get_technical_indicators"):
        if env.currency != instrument["quote_currency"]:
            reasons.append("currency_mismatch")
        if env.unit != instrument["unit"]:
            reasons.append("unit_mismatch")
    d = env.data
    try:
        if tool == "get_quote":
            if float(d["price"]) <= 0:
                reasons.append("price_not_positive")
            if env.freshness_state == "stale":
                reasons.append("stale_quote")
        elif tool == "get_fx_rate":
            if float(d["rate"]) <= 0:
                reasons.append("rate_not_positive")
        elif tool == "get_history":
            if not d.get("bars"):
                reasons.append("no_bars")
        elif tool == "get_technical_indicators":
            if not d.get("values"):
                reasons.append("no_indicators")
        elif tool == "search_news":
            if not isinstance(d.get("articles"), list):
                reasons.append("articles_missing")
    except (KeyError, TypeError, ValueError):
        reasons.append("data_shape")
    return ValidationOutcome("fail" if reasons else "pass", tuple(reasons))


# ------------------------------------------------------------------ normalised evidence
@dataclass
class EvidenceItem:
    evidence_id: str
    kind: Literal["quote", "bars", "indicator", "fx", "news"]
    source_id: str
    observed_at: str
    fetched_at: str
    content_hash: str
    trust_class: Literal["untrusted_external", "validated_numeric", "application_generated"]
    payload: dict[str, Any]
    unit: str | None = None
    currency: str | None = None
    source_uri: str | None = None
    source_title: str | None = None
    freshness_state: str = "unknown"
    taint_flags: list[str] = field(default_factory=list)
    quarantined: bool = False
    tool_call_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EvidenceItem:
        return cls(**d)


def _iso(ts: datetime | None, fallback: datetime) -> str:
    return (ts or fallback).astimezone(UTC).isoformat()


def normalise(env: ToolEnvelope, tool: str, now: datetime) -> list[EvidenceItem]:
    """Turn a validated envelope into evidence items with server-assigned IDs."""
    assert env.data is not None  # noqa: S101 - validated before
    d = env.data
    base: dict[str, Any] = {
        "source_id": f"{tool}:{env.source or 'unknown'}",
        "fetched_at": _iso(env.fetched_at, now),
        "unit": env.unit,
        "currency": env.currency,
        "freshness_state": env.freshness_state,
        "tool_call_id": str(env.tool_call_id),
    }
    observed = _iso(env.observed_at, now)
    if tool == "get_quote":
        payload = {
            "price": d["price"],
            "currency": d.get("currency"),
            "unit": d.get("unit"),
            "observed_at": d.get("observed_at"),
            "market_state": env.market_state,
            "data_mode": env.data_mode,
            "delay_note": d.get("delay_note"),
        }
        return [
            EvidenceItem(
                str(uuid.uuid4()),
                "quote",
                observed_at=observed,
                content_hash=_hash(payload),
                trust_class="validated_numeric",
                payload=payload,
                **base,
            )
        ]
    if tool == "get_fx_rate":
        payload = {k: d[k] for k in ("base", "quote", "rate", "observed_at")}
        return [
            EvidenceItem(
                str(uuid.uuid4()),
                "fx",
                observed_at=observed,
                content_hash=_hash(payload),
                trust_class="validated_numeric",
                payload=payload,
                **{**base, "unit": None, "currency": d["quote"]},
            )
        ]
    if tool == "get_history":
        bars = d["bars"]
        payload = {
            "interval": d["interval"],
            "count": d["count"],
            "total": d.get("total", d["count"]),
            "first_ts": bars[0]["ts"],
            "last_ts": bars[-1]["ts"],
            "last_close": bars[-1]["close"],
            "min_close": min(bars, key=lambda b: float(b["close"]))["close"],
            "max_close": max(bars, key=lambda b: float(b["close"]))["close"],
        }
        return [
            EvidenceItem(
                str(uuid.uuid4()),
                "bars",
                observed_at=observed,
                content_hash=_hash(d),
                trust_class="validated_numeric",
                payload=payload,
                **base,
            )
        ]
    if tool == "get_technical_indicators":
        items = []
        for name, value in sorted(d["values"].items()):
            payload = {
                "indicator": name,
                "value": value,
                "interval": d["interval"],
                "bar_count": d["bar_count"],
                "as_of": d["as_of"],
                "method": d.get("method"),
            }
            items.append(
                EvidenceItem(
                    str(uuid.uuid4()),
                    "indicator",
                    observed_at=observed,
                    content_hash=_hash(payload),
                    trust_class="validated_numeric",
                    payload=payload,
                    **base,
                )
            )
        if d.get("last_close") is not None:
            payload = {
                "indicator": "LAST_CLOSE",
                "value": d["last_close"],
                "interval": d["interval"],
                "bar_count": d["bar_count"],
                "as_of": d["as_of"],
                "method": "last bar close",
            }
            items.append(
                EvidenceItem(
                    str(uuid.uuid4()),
                    "indicator",
                    observed_at=observed,
                    content_hash=_hash(payload),
                    trust_class="validated_numeric",
                    payload=payload,
                    **base,
                )
            )
        return items
    if tool == "search_news":
        items = []
        for art in d["articles"][:8]:
            title = str(art.get("title") or "")[:300]
            excerpt = str(art.get("excerpt") or "")[:600]  # short excerpts only (section 11)
            result = injection.scan(title, excerpt)
            payload = {
                "title": title,
                "excerpt": excerpt,
                "published_at": art.get("published_at"),
                "source_domain": art.get("source_domain"),
                "score": art.get("score"),
            }
            items.append(
                EvidenceItem(
                    str(uuid.uuid4()),
                    "news",
                    observed_at=art.get("published_at") or observed,
                    content_hash=_hash(payload),
                    trust_class="untrusted_external",
                    payload=payload,
                    source_uri=art.get("url"),
                    source_title=title,
                    taint_flags=list(result.flags),
                    quarantined=result.quarantined,
                    **{**base, "unit": None, "currency": None},
                )
            )
        return items
    return []


def model_view(item: EvidenceItem) -> str:
    """How an evidence item is shown to a model. External text is wrapped as data; a
    quarantined item is replaced by a redacted record (D041-D043)."""
    if item.quarantined:
        return injection.redacted(item.evidence_id, tuple(item.taint_flags))
    body = json.dumps(item.payload, sort_keys=True, default=str)
    if item.trust_class == "untrusted_external":
        return (
            f'<untrusted_evidence id="{item.evidence_id}"'
            f' source="{item.source_uri or item.source_id}">'
            f"{body}</untrusted_evidence>"
        )
    return (
        f'<evidence id="{item.evidence_id}" kind="{item.kind}" unit="{item.unit}" '
        f'currency="{item.currency}" observed_at="{item.observed_at}">{body}</evidence>'
    )


# ------------------------------------------------------------------ handoff and gate
def handoff_checks(items: list[EvidenceItem], instrument: dict[str, Any]) -> ValidationOutcome:
    """D043: stale quotes and mixed units or currencies before synthesis.

    Units can never be reconciled here (no unit-conversion evidence exists), so any numeric
    item in a unit other than the instrument's blocks synthesis. A second currency is only
    allowed when an FX evidence item links it.
    """
    reasons: list[str] = []
    numeric = [i for i in items if i.kind in ("quote", "bars", "indicator")]
    units = sorted({str(i.unit) for i in numeric})
    if any(i.unit != instrument["unit"] for i in numeric):
        reasons.append("mixed_units:" + "/".join(units))
    foreign = sorted(
        {str(i.currency) for i in numeric if i.currency != instrument["quote_currency"]}
    )
    if foreign and not any(i.kind == "fx" for i in items):
        reasons.append("mixed_currencies_without_fx:" + "/".join(foreign))
    if any(i.kind == "quote" and i.freshness_state == "stale" for i in items):
        reasons.append("stale_quote")
    return ValidationOutcome("fail" if reasons else "pass", tuple(reasons))


@dataclass(frozen=True, slots=True)
class GateResult:
    sufficient: bool
    missing: tuple[str, ...]


def evidence_minimum(
    items: list[EvidenceItem],
    *,
    horizon: str,
    instrument_type: str,
    relevant_news_ids: set[str],
) -> GateResult:
    """R022/D047: a valid matching quote, enough bars for the horizon's indicator set, and
    at least one relevant cited news item. Anything less ends as NO_SIGNAL."""
    missing: list[str] = []
    if not any(i.kind == "quote" and i.freshness_state != "stale" for i in items):
        missing.append("No valid, fresh quote for the instrument.")
    wanted = set(history_plan(horizon, instrument_type).indicators)  # type: ignore[arg-type]
    if instrument_type == "reference_series":
        wanted.discard("ATR14")  # reference series have no high/low
    have = {i.payload["indicator"] for i in items if i.kind == "indicator"}
    if not wanted <= have:
        missing.append(
            "Not enough price history for the indicator set: missing "
            + ", ".join(sorted(wanted - have))
            + "."
        )
    news_ok = {i.evidence_id for i in items if i.kind == "news" and not i.quarantined}
    if not (news_ok & relevant_news_ids):
        missing.append("No relevant, safe news article was found and cited.")
    return GateResult(not missing, tuple(missing))
