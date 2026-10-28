"""Audit and security event writes (R053, D068). Metadata must already be redacted."""

from __future__ import annotations

import json
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

ActorKind = Literal["user", "system", "admin_cli"]

_AUDIT = text(
    "INSERT INTO audit_events (account_id, actor_user_id, actor_kind, action, target_type,"
    " target_id, request_id, run_id, outcome, metadata)"
    " VALUES (:account_id, :actor, :kind, :action, :target_type, :target_id, :request_id,"
    " :run_id, :outcome, CAST(:metadata AS jsonb))"
)
_SECURITY = text(
    "INSERT INTO security_events (account_id, kind, severity, request_id, run_id, subject_id,"
    " metadata) VALUES (:account_id, :kind, :severity, :request_id, :run_id, :subject_id,"
    " CAST(:metadata AS jsonb))"
)


async def audit(
    conn: AsyncConnection,
    *,
    action: str,
    outcome: str,
    account_id: UUID | None,
    actor_user_id: UUID | None = None,
    actor_kind: ActorKind = "user",
    target_type: str | None = None,
    target_id: str | None = None,
    request_id: str | None = None,
    run_id: UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        _AUDIT,
        {
            "account_id": account_id,
            "actor": actor_user_id,
            "kind": actor_kind,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "request_id": request_id,
            "run_id": run_id,
            "outcome": outcome,
            "metadata": json.dumps(metadata or {}),
        },
    )


async def security_event(
    conn: AsyncConnection,
    *,
    kind: str,
    severity: Literal["info", "low", "medium", "high", "critical"],
    account_id: UUID | None = None,
    request_id: str | None = None,
    run_id: UUID | None = None,
    subject_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        _SECURITY,
        {
            "account_id": account_id,
            "kind": kind,
            "severity": severity,
            "request_id": request_id,
            "run_id": run_id,
            "subject_id": subject_id,
            "metadata": json.dumps(metadata or {}),
        },
    )
