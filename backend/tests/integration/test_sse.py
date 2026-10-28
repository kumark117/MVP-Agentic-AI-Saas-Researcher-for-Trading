"""SSE: replay order, cursors (400/410), reconnect, overflow reset, session revocation,
per-member stream limit (build prompt sections 14, 18; D084-D090)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, text

from alloyiq.adapters.pg_listen import PgListener
from alloyiq.api.schemas.events import EventEnvelope
from alloyiq.application.streams import StreamService
from alloyiq.domain.errors import DomainError
from alloyiq.repositories.db import Database, DbContext
from alloyiq.repositories.events import (
    ACCOUNT_CHANNEL,
    RUN_CHANNEL,
    append_account_event,
)

from .apiclient import ApiClient
from .conftest import API_URL, make_tenant
from .fake_graph import FakeWorkflow
from .test_runs import _admitted, _member, _process

pytestmark = pytest.mark.integration


def _parse(body: str) -> list[dict[str, Any]]:
    out = []
    for block in body.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if line.startswith(":") or ": " not in line:
                continue
            k, v = line.split(": ", 1)
            fields[k] = v
        if "data" in fields:
            out.append(
                {
                    "id": fields.get("id"),
                    "event": fields["event"],
                    "data": json.loads(fields["data"]),
                }
            )
    return out


async def _finished_run(
    app: FastAPI, migrator: Engine, worker_db: Database, gold: uuid.UUID
) -> tuple[ApiClient, str]:
    c = await _member(app, migrator)
    run_id = await _admitted(c, gold, migrator)
    await _process(worker_db, run_id, FakeWorkflow(nodes=["validate_request", "price_branch"]))
    return c, run_id


async def test_terminal_run_replays_in_order_then_closes(
    app: FastAPI, migrator: Engine, worker_db: Database, enabled_gold: uuid.UUID
) -> None:
    c, run_id = await _finished_run(app, migrator, worker_db, enabled_gold)
    async with c:
        r = await c.get(f"/research/runs/{run_id}/events")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"] == "no-cache, no-transform"
        assert r.headers["x-accel-buffering"] == "no"
        events = _parse(r.text)
        seqs = [e["data"]["sequence"] for e in events]
        assert seqs == list(range(1, len(seqs) + 1))
        assert events[0]["event"] == "run.queued" and events[-1]["event"] == "run.finished"
        for e in events:
            EventEnvelope.model_validate(e["data"])
            assert e["id"] == f"{run_id}:{e['data']['sequence']}" == e["data"]["event_id"]
        # Reconnect: Last-Event-ID wins over ?after=.
        r2 = await c.get(
            f"/research/runs/{run_id}/events?after={run_id}:1",
            headers={"Last-Event-ID": f"{run_id}:3"},
        )
        assert _parse(r2.text)[0]["data"]["sequence"] == 4
        r3 = await c.get(f"/research/runs/{run_id}/events?after={run_id}:2")
        assert _parse(r3.text)[0]["data"]["sequence"] == 3


async def test_bad_and_expired_cursors(
    app: FastAPI, migrator: Engine, worker_db: Database, enabled_gold: uuid.UUID
) -> None:
    c, run_id = await _finished_run(app, migrator, worker_db, enabled_gold)
    async with c:
        for bad in (f"{uuid.uuid4()}:1", "nonsense", f"{run_id}:-1", f"{run_id}:99999"):
            r = await c.get(f"/research/runs/{run_id}/events", headers={"Last-Event-ID": bad})
            assert r.status_code == 400, bad
        with migrator.begin() as conn:  # retention purge of the oldest events
            conn.execute(
                text("DELETE FROM run_events WHERE scope_id = :r AND seq <= 3"), {"r": run_id}
            )
        r = await c.get(f"/research/runs/{run_id}/events", headers={"Last-Event-ID": f"{run_id}:1"})
        assert r.status_code == 410 and r.json()["code"] == "EVENT_CURSOR_EXPIRED"
        ok = await c.get(
            f"/research/runs/{run_id}/events", headers={"Last-Event-ID": f"{run_id}:3"}
        )
        assert ok.status_code == 200


# ------------------------------------------------------------------ live streams
@pytest.fixture
async def live() -> AsyncIterator[tuple[Database, PgListener]]:
    assert API_URL
    db = Database(API_URL, pool_size=4, max_overflow=0)
    listener = PgListener(API_URL, (RUN_CHANNEL, ACCOUNT_CHANNEL))
    await listener.start()
    yield db, listener
    await listener.stop()
    await db.dispose()


async def _append(db: Database, account_id: uuid.UUID, n: int) -> None:
    for i in range(n):
        async with db.transaction(DbContext(account_id=account_id)) as conn:
            await append_account_event(
                conn,
                account_id,
                "security.session_revoked",
                {"session_id": uuid.UUID(int=i + 1), "reason": "test"},
            )


async def _collect(gen: AsyncIterator[str], n: int, wait_s: float = 10) -> list[str]:
    out: list[str] = []

    async def run() -> None:
        async for chunk in gen:
            if chunk.startswith("id: "):
                out.append(chunk)
                if len(out) >= n:
                    return

    await asyncio.wait_for(run(), wait_s)
    return out


async def test_two_clients_see_the_same_live_order(
    live: tuple[Database, PgListener], migrator: Engine
) -> None:
    db, listener = live
    t = make_tenant(migrator)

    async def get() -> PgListener:
        return listener

    svc = StreamService(db, get, poll_s=5.0)

    async def yes() -> bool:
        return True

    plans = [
        await svc.prepare(
            scope="account", scope_id=t.account_id, account_id=t.account_id, cursor=None
        )
        for _ in range(2)
    ]
    a, b = svc.stream(plans[0], yes), svc.stream(plans[1], yes)
    await anext(a), await anext(b)  # connected (subscribed before any event)
    producer = asyncio.create_task(_append(db, t.account_id, 20))
    got_a, got_b = await asyncio.gather(_collect(a, 20), _collect(b, 20))
    await producer
    assert got_a == got_b and len(got_a) == 20
    seqs = [int(f.split("\n")[0].rsplit(":", 1)[1]) for f in got_a]
    assert seqs == sorted(seqs)
    await a.aclose()
    await b.aclose()


async def test_slow_client_gets_reset_required(
    live: tuple[Database, PgListener], migrator: Engine
) -> None:
    db, listener = live
    t = make_tenant(migrator)

    async def get() -> PgListener:
        return listener

    async def yes() -> bool:
        return True

    svc = StreamService(db, get, poll_s=5.0)
    plan = await svc.prepare(
        scope="account", scope_id=t.account_id, account_id=t.account_id, cursor=None
    )
    gen = svc.stream(plan, yes)
    await anext(gen)  # connected; now the client stops reading
    await _append(db, t.account_id, 130)
    await asyncio.sleep(0.5)  # notifications pile up past the 100-event cap
    chunks = [c async for c in gen]
    assert chunks and chunks[-1].startswith("event: stream.reset_required")
    assert '"buffer_overflow"' in chunks[-1]


async def test_revoked_session_closes_stream_and_limit_is_enforced(
    live: tuple[Database, PgListener], migrator: Engine
) -> None:
    db, listener = live
    t = make_tenant(migrator)

    async def get() -> PgListener:
        return listener

    svc = StreamService(db, get, session_check_s=0.2, poll_s=0.1, heartbeat_s=60)
    revoked = asyncio.Event()

    async def active() -> bool:
        return not revoked.is_set()

    gens = []
    for _ in range(3):
        plan = await svc.prepare(
            scope="account", scope_id=t.account_id, account_id=t.account_id, cursor=None
        )
        g = svc.stream(plan, active)
        await anext(g)
        gens.append(g)
    with pytest.raises(DomainError) as e:  # 4th concurrent stream for the member
        await svc.prepare(
            scope="account", scope_id=t.account_id, account_id=t.account_id, cursor=None
        )
    assert e.value.status == 429
    revoked.set()

    async def drain(g: AsyncIterator[str]) -> None:
        async for _ in g:
            pass

    await asyncio.wait_for(asyncio.gather(*(drain(g) for g in gens)), 5)  # well within 15 s
    # Closed streams free their slots.
    await svc.prepare(scope="account", scope_id=t.account_id, account_id=t.account_id, cursor=None)
