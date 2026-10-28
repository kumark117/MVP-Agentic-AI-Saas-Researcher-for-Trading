"""Committed contracts must equal what the code generates (D110)."""

from __future__ import annotations

import json
from pathlib import Path

from alloyiq.api.contracts import build_event_schemas, render
from alloyiq.api.schemas.events import ACCOUNT_EVENTS, RUN_EVENTS, validate_payload

CONTRACTS = Path(__file__).resolve().parents[3] / "docs" / "contracts"


def test_committed_contracts_match_generated() -> None:
    files = render(CONTRACTS)
    stale = [
        str(p) for p, txt in files.items() if not p.exists() or p.read_text(encoding="utf-8") != txt
    ]
    assert stale == [], "run scripts/gen-contracts.ps1"


def test_every_route_documents_its_response_model() -> None:
    doc = json.loads((CONTRACTS / "openapi.json").read_text(encoding="utf-8"))
    for path, ops in doc["paths"].items():
        for method, op in ops.items():
            ok = (
                op["responses"].get("200")
                or op["responses"].get("201")
                or op["responses"].get("202")
            )
            assert ok, f"{method} {path} has no success response"
            content = ok.get("content", {})
            if "text/event-stream" in content:
                continue
            schema = content.get("application/json", {}).get("schema", {})
            assert schema.get("$ref") or schema.get("title"), f"{method} {path} untyped"


def test_one_schema_per_event_type() -> None:
    names = set(build_event_schemas())
    assert len(names) == len(RUN_EVENTS) + len(ACCOUNT_EVENTS)


def test_event_payload_validation_rejects_unknown_fields_and_types() -> None:
    import pytest

    with pytest.raises(ValueError):
        validate_payload("run", "no.such.event", {})
    with pytest.raises(ValueError):
        validate_payload("run", "node.started", {"node_id": "price", "attempt": 1, "x": 1})
    assert validate_payload("run", "node.started", {"node_id": "price", "attempt": 1}) == {
        "node_id": "price",
        "attempt": 1,
    }
