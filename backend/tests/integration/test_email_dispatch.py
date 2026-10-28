"""Queued emails are delivered once and their one-time links are wiped afterwards."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, text

from alloyiq.adapters.email import FakeEmailSender
from alloyiq.application.email_dispatch import EmailDispatcher
from alloyiq.repositories.db import Database

from .apiclient import PASSWORD, ApiClient, new_email

pytestmark = pytest.mark.integration


async def test_dispatch_sends_and_clears_payload(
    app: FastAPI, migrator: Engine, worker_db: Database
) -> None:
    email = new_email()
    async with ApiClient(app) as c:
        await c.post(
            "/auth/register",
            {
                "email": email,
                "password": PASSWORD,
                "display_name": "X",
                "terms_version": "t1",
                "privacy_version": "p1",
            },
        )
    sender = FakeEmailSender()
    dispatcher = EmailDispatcher(worker_db, sender, batch=500)
    while await dispatcher.run_once():
        pass
    mine = [m for m in sender.sent if m.to == email]
    assert len(mine) == 1 and "#token=" in mine[0].body
    with migrator.connect() as conn:
        row = conn.execute(
            text("SELECT sent_at, payload FROM email_outbox WHERE to_email = :e"), {"e": email}
        ).one()
    assert row.sent_at is not None and row.payload == {}
    # Nothing is sent twice.
    before = len(sender.sent)
    await dispatcher.run_once()
    assert [m for m in sender.sent[before:] if m.to == email] == []
