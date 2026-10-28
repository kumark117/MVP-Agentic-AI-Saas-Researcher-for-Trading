"""Structured outputs for the agents: a strict JSON schema sent to the model (output_config
format) and a Pydantic model that re-validates the result regardless (section 9.1)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Observation(_M):
    statement: str = Field(max_length=600)
    evidence_ids: list[str] = Field(max_length=12)


class NewsAssessment(_M):
    evidence_id: str
    relevance: Literal["high", "medium", "low"]
    stance: Literal["supporting", "opposing", "neutral"]
    excerpt: str = Field(max_length=400)


class BranchFinding(_M):
    summary: str = Field(max_length=800)
    observations: list[Observation] = Field(max_length=12)
    stance: Literal["bullish", "bearish", "neutral", "insufficient"]
    news_assessments: list[NewsAssessment] = Field(max_length=8)


class Claim(_M):
    claim: str = Field(max_length=600)
    evidence_ids: list[str] = Field(min_length=1, max_length=12)


class SignalOutput(_M):
    signal: Literal["BUY", "SELL", "HOLD", "NO_SIGNAL"]
    confidence_score: float | None = Field(ge=0, le=1)
    confidence_label: Literal["low", "medium", "high"] | None
    summary: str = Field(max_length=1500)
    supporting_evidence: list[Claim] = Field(max_length=10)
    opposing_evidence: list[Claim] = Field(max_length=10)
    assumptions: list[str] = Field(max_length=10)
    risks: list[str] = Field(max_length=10)
    limitations: list[str] = Field(max_length=10)


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


_STR = {"type": "string"}
_IDS = {"type": "array", "items": _STR}

BRANCH_FINDING_SCHEMA: dict[str, Any] = _obj(
    {
        "summary": _STR,
        "observations": {
            "type": "array",
            "items": _obj({"statement": _STR, "evidence_ids": _IDS}),
        },
        "stance": {"type": "string", "enum": ["bullish", "bearish", "neutral", "insufficient"]},
        "news_assessments": {
            "type": "array",
            "items": _obj(
                {
                    "evidence_id": _STR,
                    "relevance": {"type": "string", "enum": ["high", "medium", "low"]},
                    "stance": {"type": "string", "enum": ["supporting", "opposing", "neutral"]},
                    "excerpt": _STR,
                }
            ),
        },
    }
)

_CLAIMS = {"type": "array", "items": _obj({"claim": _STR, "evidence_ids": _IDS})}
_LIST = {"type": "array", "items": _STR}

SIGNAL_SCHEMA: dict[str, Any] = _obj(
    {
        "signal": {"type": "string", "enum": ["BUY", "SELL", "HOLD", "NO_SIGNAL"]},
        "confidence_score": {"type": ["number", "null"]},
        "confidence_label": {"type": ["string", "null"], "enum": ["low", "medium", "high", None]},
        "summary": _STR,
        "supporting_evidence": _CLAIMS,
        "opposing_evidence": _CLAIMS,
        "assumptions": _LIST,
        "risks": _LIST,
        "limitations": _LIST,
    }
)


def _tool(name: str, description: str, props: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "description": description, "strict": True, "input_schema": _obj(props)}


# Stable, byte-identical definitions (prompt caching; D035): the model chooses only which tool.
TOOL_DEFS: dict[str, dict[str, Any]] = {
    "get_quote": _tool(
        "get_quote",
        "Latest validated reference quote for this run's instrument (fixed by the server).",
        {},
    ),
    "get_fx_rate": _tool(
        "get_fx_rate", "USD to INR reference rate for an optional converted view.", {}
    ),
    "get_history": _tool(
        "get_history", "Summary of validated price bars for this run's instrument and horizon.", {}
    ),
    "get_technical_indicators": _tool(
        "get_technical_indicators",
        "Deterministic indicator values computed on the server from validated bars.",
        {},
    ),
    "search_news": _tool(
        "search_news",
        "Recent news about this run's metal; the server builds the query.",
        {"focus": {"type": "string", "description": "Optional plain words, or empty."}},
    ),
}
