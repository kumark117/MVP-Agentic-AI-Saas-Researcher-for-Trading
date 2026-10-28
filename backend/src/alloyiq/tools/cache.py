"""Short-lived cache for public market data (optional Redis; never private data).

Keys include instrument, provider, interval and a freshness bucket (ToolService), so an
entry can never be served beyond its freshness policy. Redis failure degrades to a miss.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from alloyiq.observability.logging import get_logger

log = get_logger(__name__)


class Cache(Protocol):
    async def get(self, key: str) -> dict[str, Any] | None: ...

    async def set(self, key: str, value: dict[str, Any], ttl_s: int) -> None: ...


class NullCache:
    async def get(self, key: str) -> dict[str, Any] | None:
        return None

    async def set(self, key: str, value: dict[str, Any], ttl_s: int) -> None:
        return None


class MemoryCache:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}

    async def get(self, key: str) -> dict[str, Any] | None:
        return self.data.get(key)

    async def set(self, key: str, value: dict[str, Any], ttl_s: int) -> None:
        self.data[key] = value


class RedisCache:
    def __init__(self, url: str) -> None:
        import redis.asyncio as aioredis

        self._r = aioredis.from_url(url, socket_timeout=0.3, socket_connect_timeout=0.3)

    async def get(self, key: str) -> dict[str, Any] | None:
        try:
            raw = await self._r.get(key)
        except Exception as exc:
            log.warning("cache_get_failed", error_type=type(exc).__name__)
            return None
        return json.loads(raw) if raw else None

    async def set(self, key: str, value: dict[str, Any], ttl_s: int) -> None:
        try:
            await self._r.set(key, json.dumps(value, default=str), ex=ttl_s)
        except Exception as exc:
            log.warning("cache_set_failed", error_type=type(exc).__name__)
