"""alloyiq-admin accounts: email -> account ID lookup, audited without the search text."""

from __future__ import annotations

import argparse
import json

import pytest
from sqlalchemy import Engine, text

from alloyiq.cli import admin_ops
from alloyiq.repositories.db import Database

from .conftest import make_tenant
from .test_admin_billing import admin_db  # noqa: F401 - fixture

pytestmark = pytest.mark.integration


async def test_accounts_finds_a_member_by_email_and_is_audited(
    admin_db: Database,  # noqa: F811
    migrator: Engine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    t = make_tenant(migrator)
    needle = t.email.split("@")[0].upper()  # case-insensitive part of the address
    ns = argparse.Namespace(email=needle, limit=10)
    assert await admin_ops.accounts(admin_db, ns) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["email"] for r in rows] == [t.email]
    assert rows[0]["account_id"] == str(t.account_id)
    assert rows[0]["verified"] is True and rows[0]["runs"] == 0
    with migrator.connect() as c:
        meta = c.execute(
            text(
                "SELECT metadata FROM audit_events WHERE action = 'admin.accounts_listed'"
                " ORDER BY occurred_at DESC, id DESC LIMIT 1"
            )
        ).scalar()
    assert meta == {"filtered": True, "count": 1}  # the search text itself is not stored


async def test_accounts_without_email_lists_newest_first(
    admin_db: Database,  # noqa: F811
    migrator: Engine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    older, newer = make_tenant(migrator), make_tenant(migrator)
    assert await admin_ops.accounts(admin_db, argparse.Namespace(email=None, limit=500)) == 0
    emails = [r["email"] for r in json.loads(capsys.readouterr().out)]
    assert emails.index(newer.email) < emails.index(older.email)
