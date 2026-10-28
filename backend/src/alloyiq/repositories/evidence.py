"""Tool calls, evidence items, validation verdicts and reports (D038, D108, D021)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from alloyiq.workflows.evidence import VALIDATOR_VERSION, EvidenceItem


class EvidenceRepository:
    # ------------------------------------------------------------------ tool calls
    async def tool_intent(self, conn: AsyncConnection, values: dict[str, Any]) -> bool:
        row = (
            await conn.execute(
                text(
                    "INSERT INTO tool_calls (id, account_id, run_id, node_id, call_key, generation,"
                    " tool_name, tool_version, args_hash) VALUES (:id, :account_id, :run_id,"
                    " :node_id, :call_key, :generation, :tool_name, :tool_version, :args_hash)"
                    " ON CONFLICT (call_key) DO NOTHING RETURNING id"
                ),
                values,
            )
        ).first()
        return row is not None

    async def tool_finish(
        self,
        conn: AsyncConnection,
        call_id: UUID,
        *,
        status: str,
        duration_ms: int | None,
        result_ref: str | None,
        error_code: str | None,
    ) -> None:
        await conn.execute(
            text(
                "UPDATE tool_calls SET status = :s, duration_ms = :d, result_ref = :r,"
                " error_code = :e WHERE id = :i AND status = 'intent'"
            ),
            {"i": call_id, "s": status, "d": duration_ms, "r": result_ref, "e": error_code},
        )

    async def tool_ambiguous(self, conn: AsyncConnection, run_id: UUID) -> int:
        r = await conn.execute(
            text(
                "UPDATE tool_calls SET status = 'ambiguous' WHERE run_id = :r AND status = 'intent'"
            ),
            {"r": run_id},
        )
        return r.rowcount

    async def count_tool_calls(self, conn: AsyncConnection, run_id: UUID) -> dict[str, int]:
        rows = await conn.execute(
            text("SELECT node_id, count(*) FROM tool_calls WHERE run_id = :r GROUP BY node_id"),
            {"r": run_id},
        )
        return {r[0]: int(r[1]) for r in rows}

    # ------------------------------------------------------------------ evidence
    async def insert_evidence(
        self,
        conn: AsyncConnection,
        *,
        account_id: UUID,
        run_id: UUID,
        instrument_id: UUID | None,
        item: EvidenceItem,
        artifact_key: str | None = None,
    ) -> None:
        await conn.execute(
            text(
                "INSERT INTO evidence_items (id, account_id, run_id, kind, source_id, source_uri,"
                " source_title, observed_at, fetched_at, content_hash, trust_class, taint_flags,"
                " quarantined, instrument_id, unit, currency, payload, artifact_key,"
                " validator_version) VALUES (:id, :a, :r, :k, :sid, :uri, :title, :obs, :fetched,"
                " :hash, :trust, :taint, :q, :ins, :unit, :cur, CAST(:payload AS jsonb), :art, :vv)"
                " ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": UUID(item.evidence_id),
                "a": account_id,
                "r": run_id,
                "k": item.kind,
                "sid": item.source_id,
                "uri": item.source_uri,
                "title": item.source_title,
                "obs": datetime.fromisoformat(item.observed_at),
                "fetched": datetime.fromisoformat(item.fetched_at),
                "hash": item.content_hash,
                "trust": item.trust_class,
                "taint": item.taint_flags,
                "q": item.quarantined,
                "ins": instrument_id,
                "unit": item.unit,
                "cur": item.currency,
                "payload": json.dumps(
                    {
                        **item.payload,
                        "_freshness": item.freshness_state,
                        "_tool_call": item.tool_call_id,
                    },
                    default=str,
                ),
                "art": artifact_key,
                "vv": VALIDATOR_VERSION,
            },
        )

    async def validation(
        self,
        conn: AsyncConnection,
        *,
        account_id: UUID,
        run_id: UUID,
        boundary: str,
        subject_id: str,
        verdict: str,
        reasons: list[str],
        policy_version: str,
    ) -> None:
        await conn.execute(
            text(
                "INSERT INTO validation_results (account_id, run_id, boundary, subject_id, verdict,"
                " reasons, policy_version) VALUES (:a, :r, :b, :s, :v, :reasons, :p)"
            ),
            {
                "a": account_id,
                "r": run_id,
                "b": boundary,
                "s": subject_id[:200],
                "v": verdict,
                "reasons": reasons,
                "p": policy_version,
            },
        )

    # ------------------------------------------------------------------ reports
    async def insert_report(
        self,
        conn: AsyncConnection,
        *,
        account_id: UUID,
        run_id: UUID,
        report: dict[str, Any],
    ) -> bool:
        conf = report["confidence"]
        row = (
            await conn.execute(
                text(
                    "INSERT INTO reports (id, account_id, run_id, report, signal, confidence_score,"
                    " confidence_label, complete, data_as_of, freshness_state) VALUES (:id, :a, :r,"
                    " CAST(:rep AS jsonb), :sig, :score, :label, :complete, :asof, :fresh)"
                    " ON CONFLICT (run_id) DO NOTHING RETURNING id"
                ),
                {
                    "id": UUID(report["report_id"]),
                    "a": account_id,
                    "r": run_id,
                    "rep": json.dumps(report, default=str),
                    "sig": report["signal"],
                    "score": conf["score"],
                    "label": conf["label"],
                    "complete": report["complete"],
                    "asof": datetime.fromisoformat(report["data_as_of"])
                    if report["data_as_of"]
                    else None,
                    "fresh": report["freshness_state"],
                },
            )
        ).first()
        if row is None:
            return False
        for c in report["citations"]:
            await conn.execute(
                text(
                    "INSERT INTO report_citations (account_id, report_id, evidence_id,"
                    " source_title,"
                    " source_url, observed_at, content_hash) VALUES (:a, :rep, :e, :t, :u, :o, :h)"
                    " ON CONFLICT DO NOTHING"
                ),
                {
                    "a": account_id,
                    "rep": UUID(report["report_id"]),
                    "e": UUID(c["evidence_id"]),
                    "t": c["source_title"],
                    "u": c["source_url"],
                    "o": datetime.fromisoformat(c["observed_at"]),
                    "h": c["content_hash"],
                },
            )
        return True
