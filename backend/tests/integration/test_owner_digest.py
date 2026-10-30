"""Hourly owner digest: sign-ups and finished runs, watermarked in audit_events."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, text

from alloyiq.application.email_dispatch import render
from alloyiq.application.owner_digest import OwnerDigestService
from alloyiq.repositories.db import Database

from .conftest import make_run, make_tenant

OWNER = "owner-digest-test@example.com"
# A fixed historical hour, so rows from other tests never fall inside the window.
T0 = datetime(2001, 1, 1, 10, 0, tzinfo=UTC)


def _watermark(migrator: Engine, end: datetime) -> None:
    with migrator.begin() as c:
        c.execute(
            text(
                "INSERT INTO audit_events (actor_kind, action, outcome, target_type, metadata)"
                " VALUES ('system', 'owner_digest.empty', 'nothing_to_report', 'owner_digest',"
                " CAST(:m AS jsonb))"
            ),
            {"m": json.dumps({"window_end": end.isoformat()})},
        )


def _queued(migrator: Engine) -> list[dict]:
    with migrator.connect() as c:
        rows = c.execute(
            text(
                "SELECT payload FROM email_outbox WHERE to_email = :o AND template = 'owner_digest'"
                " AND payload->>'window' LIKE '01 Jan 2001%' ORDER BY created_at"
            ),
            {"o": OWNER},
        ).all()
    return [r.payload for r in rows]


async def test_digest_reports_signups_and_runs_once_per_window(
    migrator: Engine, worker_db: Database
) -> None:
    with migrator.begin() as c:
        c.execute(
            text(
                "DELETE FROM email_outbox WHERE to_email = :o AND payload->>'window' LIKE"
                " '01 Jan 2001%'"
            ),
            {"o": OWNER},
        )
    t = make_tenant(migrator)
    run = make_run(migrator, t)
    with migrator.begin() as c:
        c.execute(
            text("UPDATE users SET created_at = :at WHERE id = :u"),
            {"at": T0 + timedelta(minutes=10), "u": t.user_id},
        )
        c.execute(
            text(
                "UPDATE research_runs SET status = 'timed_out', submitted_at = :s,"
                " terminal_at = :e WHERE id = :r"
            ),
            {"s": T0 + timedelta(minutes=20), "e": T0 + timedelta(minutes=21), "r": run},
        )
    _watermark(migrator, T0)
    svc = OwnerDigestService(worker_db, OWNER)

    res = await svc.run_once(now=T0 + timedelta(minutes=65))
    assert (res.window_start, res.window_end) == (T0, T0 + timedelta(hours=1))
    assert res.queued and res.signups >= 1 and res.runs >= 1
    [payload] = _queued(migrator)
    assert t.email in [s["email"] for s in payload["signups"]]
    assert t.email in [r["email"] for r in payload["runs"]]
    assert "15:30" in payload["window"]  # 10:00 UTC shown in IST
    subject, body = render("owner_digest", payload)
    assert subject.startswith("AlloyIQ: ") and "sign-up" in subject and "research run" in subject
    assert t.email in body and "->" in body

    again = await svc.run_once(now=T0 + timedelta(minutes=70))  # same hour: nothing new
    assert not again.queued and again.window_end is None
    assert len(_queued(migrator)) == 1


async def test_empty_window_sends_nothing_but_advances(
    migrator: Engine, worker_db: Database
) -> None:
    quiet = datetime(2001, 1, 2, 3, 0, tzinfo=UTC)
    _watermark(migrator, quiet)
    res = await OwnerDigestService(worker_db, OWNER).run_once(now=quiet + timedelta(hours=1))
    assert res.window_end == quiet + timedelta(hours=1)
    assert not res.queued and res.signups == 0 and res.runs == 0
    with migrator.connect() as c:
        latest = c.execute(
            text(
                "SELECT action, metadata->>'window_end' FROM audit_events"
                " WHERE action LIKE 'owner_digest.%' ORDER BY occurred_at DESC, id DESC LIMIT 1"
            )
        ).one()
    assert latest[0] == "owner_digest.empty"
    assert datetime.fromisoformat(latest[1]) == quiet + timedelta(hours=1)
