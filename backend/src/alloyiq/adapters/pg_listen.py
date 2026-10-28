"""PostgreSQL LISTEN/NOTIFY fan-out (build prompt sections 4, 8.4, 14).

One dedicated asyncpg connection per process listens on a few channels and fans each
notification out to in-process subscribers. Notifications are only wake-ups: subscribers
re-read the durable rows, so a lost notification (reconnect, restart) costs latency, not
data, and polling backstops every consumer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

import asyncpg

from alloyiq.observability.logging import get_logger

log = get_logger(__name__)


def asyncpg_dsn(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://", 1).replace(
        "postgresql+psycopg://", "postgresql://", 1
    )


class Subscription:
    """A bounded inbox. Overflow sets `overflowed` instead of blocking the listener."""

    def __init__(self, hub: PgListener, channel: str, key: str | None, maxsize: int) -> None:
        self.hub, self.channel, self.key = hub, channel, key
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self.overflowed = False

    def offer(self, payload: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            self.overflowed = True

    async def get(self, wait_s: float) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(self.queue.get(), wait_s)
        except TimeoutError:
            return None

    def close(self) -> None:
        self.hub.unsubscribe(self)

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class PgListener:
    def __init__(self, dsn: str, channels: Iterable[str]) -> None:
        self.dsn = asyncpg_dsn(dsn)
        self.channels = tuple(channels)
        self._subs: dict[str, set[Subscription]] = defaultdict(set)
        self._conn: asyncpg.Connection | None = None
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._stopping = False

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="pg-listener")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._ready.wait(), 5)

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()

    @property
    def connected(self) -> bool:
        return self._conn is not None and not self._conn.is_closed()

    def subscribe(self, channel: str, key: str | None = None, maxsize: int = 100) -> Subscription:
        sub = Subscription(self, channel, key, maxsize)
        self._subs[channel].add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subs[sub.channel].discard(sub)

    def _on_notify(self, _conn: Any, _pid: int, channel: str, payload: str) -> None:
        try:
            data = json.loads(payload) if payload.startswith("{") else {"value": payload}
        except ValueError:
            data = {"value": payload}
        key = data.get("scope_id") or data.get("value")
        for sub in list(self._subs.get(channel, ())):
            if sub.key is None or sub.key == key:
                sub.offer(data)

    async def _run(self) -> None:
        delay = 0.5
        while not self._stopping:
            try:
                self._conn = await asyncpg.connect(self.dsn)
                for ch in self.channels:
                    await self._conn.add_listener(ch, self._on_notify)
                self._ready.set()
                delay = 0.5
                # Wake everyone once after (re)connecting: they re-read durable state.
                for ch in self.channels:
                    for sub in list(self._subs.get(ch, ())):
                        sub.offer({"reconnected": True})
                closed = asyncio.Event()
                self._conn.add_termination_listener(lambda _c, ev=closed: ev.set())
                await closed.wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("pg_listener_disconnected", error_type=type(exc).__name__)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10)
