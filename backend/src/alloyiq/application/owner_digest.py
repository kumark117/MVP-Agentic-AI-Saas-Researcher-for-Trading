"""Hourly owner digest: new sign-ups and finished research runs, emailed to the owner.

Enabled by OWNER_ALERT_EMAIL. The worker checks every few minutes; once a full hour has
passed since the last digest it covers [last end, latest full hour) so an outage never
skips activity. Each window is recorded in audit_events (`owner_digest.sent` or
`owner_digest.empty`), which is also the watermark, and an advisory lock keeps two workers
from sending the same window. An empty window sends nothing.

The owner chose to see members' email addresses (2026-10-30). Questions, prompts and report
text are never included. The email goes through email_outbox like every other message, so
it is retried on failure and its payload is cleared once sent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

from alloyiq.repositories.audit import audit
from alloyiq.repositories.db import Database, DbContext

TEMPLATE = "owner_digest"
_LOCK_KEY = 7_204_311  # pg_advisory_xact_lock key for the digest
WINDOW = timedelta(hours=1)


@dataclass(frozen=True)
class DigestResult:
    window_start: datetime | None = None
    window_end: datetime | None = None
    signups: int = 0
    runs: int = 0
    queued: bool = False


def _floor_hour(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0)


class OwnerDigestService:
    def __init__(self, db: Database, owner_email: str, timezone: str = "Asia/Kolkata") -> None:
        self.db, self.owner_email, self.tz = db, owner_email, ZoneInfo(timezone)

    def _local(self, t: datetime) -> str:
        return t.astimezone(self.tz).strftime("%d %b %Y %H:%M")

    async def run_once(self, now: datetime | None = None) -> DigestResult:
        end = _floor_hour(now or datetime.now(UTC))
        async with self.db.transaction(DbContext.for_system()) as conn:
            await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
            last = (
                await conn.execute(
                    text(
                        "SELECT metadata->>'window_end' FROM audit_events"
                        " WHERE action IN ('owner_digest.sent', 'owner_digest.empty')"
                        " ORDER BY occurred_at DESC, id DESC LIMIT 1"
                    )
                )
            ).scalar()
            start = datetime.fromisoformat(last) if last else end - WINDOW
            if end - start < WINDOW:
                return DigestResult()
            window = {"s": start, "e": end}
            signups = (
                await conn.execute(
                    text(
                        "SELECT email, email_verified_at IS NOT NULL AS verified, created_at"
                        " FROM users WHERE created_at >= :s AND created_at < :e"
                        " ORDER BY created_at"
                    ),
                    window,
                )
            ).all()
            runs = (
                await conn.execute(
                    text(
                        "SELECT u.email, r.instrument_snapshot->>'canonical_id' AS instrument,"
                        " r.horizon, r.status, r.outcome_reason, r.submitted_at, r.terminal_at,"
                        " rep.signal,"
                        " (SELECT coalesce(sum(provider_cost_usd), 0) FROM usage_records ur"
                        "   WHERE ur.run_id = r.id) AS cost_usd"
                        " FROM research_runs r JOIN users u ON u.id = r.user_id"
                        " LEFT JOIN reports rep ON rep.run_id = r.id"
                        " WHERE r.terminal_at >= :s AND r.terminal_at < :e"
                        " ORDER BY r.terminal_at"
                    ),
                    window,
                )
            ).all()
            meta = {
                "window_start": start.isoformat(),
                "window_end": end.isoformat(),
                "signups": len(signups),
                "runs": len(runs),
            }
            queued = bool(signups or runs)
            if queued:
                payload = {
                    "window": f"{self._local(start)} to {self._local(end)} ({self.tz.key})",
                    "signups": [
                        {"email": s.email, "verified": s.verified, "at": self._local(s.created_at)}
                        for s in signups
                    ],
                    "runs": [
                        {
                            "email": r.email,
                            "instrument": r.instrument,
                            "horizon": r.horizon,
                            "status": r.status,
                            "signal": r.signal,
                            "outcome": r.outcome_reason,
                            "cost_usd": str(Decimal(r.cost_usd).quantize(Decimal("0.001"))),
                            "seconds": int((r.terminal_at - r.submitted_at).total_seconds()),
                            "at": self._local(r.terminal_at),
                        }
                        for r in runs
                    ],
                }
                await conn.execute(
                    text(
                        "INSERT INTO email_outbox (to_email, template, payload)"
                        " VALUES (:to, :t, CAST(:p AS jsonb))"
                    ),
                    {"to": self.owner_email, "t": TEMPLATE, "p": json.dumps(payload)},
                )
            await audit(
                conn,
                action="owner_digest.sent" if queued else "owner_digest.empty",
                outcome="queued" if queued else "nothing_to_report",
                account_id=None,
                actor_kind="system",
                target_type="owner_digest",
                target_id=end.isoformat(),
                metadata=meta,
            )
        return DigestResult(start, end, len(signups), len(runs), queued)


def render_digest(payload: dict[str, Any]) -> tuple[str, str]:
    signups, runs = payload.get("signups", []), payload.get("runs", [])
    parts = []
    if signups:
        parts.append(f"{len(signups)} sign-up{'s' * (len(signups) != 1)}")
    if runs:
        parts.append(f"{len(runs)} research run{'s' * (len(runs) != 1)}")
    subject = "AlloyIQ: " + ", ".join(parts)
    lines = [f"AlloyIQ activity, {payload['window']}.", ""]
    if signups:
        lines.append(f"New sign-ups ({len(signups)}):")
        for s in signups:
            state = "verified" if s["verified"] else "not verified yet"
            lines.append(f"  - {s['at']}  {s['email']} ({state})")
        lines.append("")
    if runs:
        total = sum(Decimal(r["cost_usd"]) for r in runs)
        lines.append(
            f"Research runs finished ({len(runs)}, real Claude and data cost USD {total}):"
        )
        for r in runs:
            result = r["signal"] or r["outcome"] or r["status"]
            lines.append(
                f"  - {r['at']}  {r['email']}: {r['instrument']} {r['horizon']} -> {result}"
                f" [{r['status']}], USD {r['cost_usd']}, {r['seconds']} s"
            )
        lines.append("")
    lines.append("Questions and report text are never included. Details: alloyiq-admin runs.")
    return subject, "\n".join(lines)
