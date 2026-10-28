"""Token-bucket rate limiting (build prompt section 8.1).

RedisRateLimiter is the normal path. If Redis errors, it falls back to an in-process
limiter with half the capacity (conservative), so losing Redis never disables limits.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Protocol

from alloyiq.observability.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Limit:
    capacity: int  # burst size
    per_seconds: float  # time to refill the whole bucket


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    retry_after_s: int


class RateLimiter(Protocol):
    async def hit(self, key: str, limit: Limit) -> Decision: ...


class InMemoryRateLimiter:
    def __init__(self, scale: float = 1.0, clock: object = time.monotonic) -> None:
        self._buckets: dict[str, tuple[float, float]] = {}
        self._scale = scale
        self._clock = clock

    async def hit(self, key: str, limit: Limit) -> Decision:
        now: float = self._clock()  # type: ignore[operator]
        cap = max(1.0, limit.capacity * self._scale)
        rate = cap / limit.per_seconds
        tokens, last = self._buckets.get(key, (cap, now))
        tokens = min(cap, tokens + (now - last) * rate)
        if tokens >= 1:
            self._buckets[key] = (tokens - 1, now)
            return Decision(True, 0)
        self._buckets[key] = (tokens, now)
        return Decision(False, max(1, math.ceil((1 - tokens) / rate)))


_LUA = """
local key = KEYS[1]
local cap = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local b = redis.call('HMGET', key, 't', 'ts')
local tokens = tonumber(b[1]) or cap
local ts = tonumber(b[2]) or now
tokens = math.min(cap, tokens + (now - ts) * rate)
local allowed = 0
if tokens >= 1 then tokens = tokens - 1; allowed = 1 end
redis.call('HSET', key, 't', tokens, 'ts', now)
redis.call('EXPIRE', key, math.ceil(cap / rate) + 1)
return {allowed, tostring(tokens)}
"""


class RedisRateLimiter:
    def __init__(self, redis_url: str) -> None:
        import redis.asyncio as aioredis

        self._redis = aioredis.from_url(redis_url, socket_timeout=0.5, socket_connect_timeout=0.5)
        self._script = self._redis.register_script(_LUA)
        self._fallback = InMemoryRateLimiter(scale=0.5)

    async def hit(self, key: str, limit: Limit) -> Decision:
        rate = limit.capacity / limit.per_seconds
        try:
            allowed, tokens = await self._script(
                keys=[f"rl:{key}"], args=[limit.capacity, rate, time.time()]
            )
        except Exception as exc:  # Redis down: never fail open
            log.warning("rate_limit_fallback", error_type=type(exc).__name__)
            return await self._fallback.hit(key, limit)
        if int(allowed):
            return Decision(True, 0)
        return Decision(False, max(1, math.ceil((1 - float(tokens)) / rate)))


# Named limits used by the API (per IP and per subject).
LIMITS: dict[str, Limit] = {
    "login:ip": Limit(20, 60),
    "login:email": Limit(5, 60),
    "register:ip": Limit(5, 60),
    "recover:ip": Limit(10, 60),
    "recover:email": Limit(3, 300),
    "verify:ip": Limit(20, 60),
    "reauth:account": Limit(5, 60),
    "checkout:account": Limit(10, 60),
    "estimate:account": Limit(30, 60),
}
