"""TraceSink (build prompt section 17; R058, DV-09).

With LANGSMITH_API_KEY set, each run is exported as one LangSmith tree: the run, its graph
nodes and its Claude calls with token usage. Inputs and outputs are redacted to
identifiers, statuses and counts; no prompts, article text, emails or secrets leave the
platform. Export never blocks research: failures are buffered in trace_outbox and the run
records observability_gap. Without a key the sink logs locally and records the gap.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import text

from alloyiq.observability.logging import get_logger
from alloyiq.repositories.db import Database, DbContext

log = get_logger(__name__)
EXPORT_TIMEOUT_S = 5.0
_ALLOWED_KEYS = {
    "node_id",
    "status",
    "model",
    "model_used",
    "stop_reason",
    "usage_status",
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "duration_ms",
    "call_key",
    "purpose",
    "tool_name",
    "verdict",
    "outcome",
    "error_code",
    "provider_request_id",
}


def redact(meta: dict[str, Any]) -> dict[str, Any]:
    """Keep only allowlisted, non-personal fields; truncate strings."""
    return {
        k: (v[:120] if isinstance(v, str) else v)
        for k, v in meta.items()
        if k in _ALLOWED_KEYS and (v is None or isinstance(v, (str, int, float, bool)))
    }


@dataclass
class Span:
    name: str
    kind: str  # chain | llm | tool
    start: datetime
    end: datetime | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class TraceSink(Protocol):
    def span(self, name: str, kind: str, **meta: Any) -> Span: ...

    def finish(self, span: Span, **meta: Any) -> None: ...

    async def flush(self, outcome: str) -> None: ...


class _Collector:
    def __init__(self, db: Database, account_id: uuid.UUID, run_id: uuid.UUID) -> None:
        self.db, self.account_id, self.run_id = db, account_id, run_id
        self.spans: list[Span] = []
        self.started = datetime.now(UTC)

    def span(self, name: str, kind: str, **meta: Any) -> Span:
        s = Span(name, kind, datetime.now(UTC), meta=redact(meta))
        self.spans.append(s)
        return s

    def finish(self, span: Span, **meta: Any) -> None:
        span.end = datetime.now(UTC)
        span.meta.update(redact(meta))

    async def _mark_gap(self, reason: str, payload: dict[str, Any] | None) -> None:
        async with self.db.transaction(DbContext(account_id=self.account_id)) as conn:
            await conn.execute(
                text("UPDATE research_runs SET observability_gap = true WHERE id = :r"),
                {"r": self.run_id},
            )
            if payload is not None:
                await conn.execute(
                    text(
                        "INSERT INTO trace_outbox (account_id, run_id, payload, last_error)"
                        " VALUES (:a, :r, CAST(:p AS jsonb), :e)"
                    ),
                    {
                        "a": self.account_id,
                        "r": self.run_id,
                        "p": json.dumps(payload, default=str),
                        "e": reason[:200],
                    },
                )

    def payload(self, outcome: str) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "outcome": outcome,
            "started": self.started.isoformat(),
            "spans": [
                {
                    "name": s.name,
                    "kind": s.kind,
                    "start": s.start.isoformat(),
                    "end": (s.end or s.start).isoformat(),
                    "meta": s.meta,
                }
                for s in self.spans
            ],
        }


class LocalTraceSink(_Collector):
    async def flush(self, outcome: str) -> None:
        log.info("trace_local", run_id=str(self.run_id), spans=len(self.spans), outcome=outcome)
        await self._mark_gap("LANGSMITH_API_KEY not configured", None)


class LangSmithTraceSink(_Collector):
    def __init__(
        self, db: Database, account_id: uuid.UUID, run_id: uuid.UUID, *, api_key: str, project: str
    ) -> None:
        super().__init__(db, account_id, run_id)
        self.api_key, self.project = api_key, project

    def _export(self, outcome: str) -> None:
        from langsmith import Client
        from langsmith.run_trees import RunTree

        client = Client(api_key=self.api_key)
        root = RunTree(
            name="research_run",
            run_type="chain",
            project_name=self.project,
            ls_client=client,
            inputs={"run_id": str(self.run_id)},
            start_time=self.started,
            extra={"metadata": {"run_id": str(self.run_id)}},
        )
        for s in self.spans:
            child = root.create_child(
                name=s.name,
                run_type="llm" if s.kind == "llm" else "tool" if s.kind == "tool" else "chain",
                inputs={"node_id": s.meta.get("node_id")},
                start_time=s.start,
            )
            child.end(outputs=s.meta, end_time=s.end or s.start)
        root.end(outputs={"outcome": outcome})
        root.post(exclude_child_runs=False)

    async def flush(self, outcome: str) -> None:
        try:
            await asyncio.wait_for(asyncio.to_thread(self._export, outcome), EXPORT_TIMEOUT_S)
        except Exception as exc:  # never block or fail research on tracing
            log.warning(
                "trace_export_failed", run_id=str(self.run_id), error_type=type(exc).__name__
            )
            await self._mark_gap(type(exc).__name__, self.payload(outcome))


def make_sink(
    db: Database, account_id: uuid.UUID, run_id: uuid.UUID, *, api_key: str | None, project: str
) -> TraceSink:
    if api_key:
        return LangSmithTraceSink(db, account_id, run_id, api_key=api_key, project=project)
    return LocalTraceSink(db, account_id, run_id)
