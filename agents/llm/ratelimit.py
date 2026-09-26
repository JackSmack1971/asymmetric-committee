"""Redis token bucket shared by every worker (§10.2): one request bucket and one token bucket."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

import redis

# KEYS[1]=bucket; ARGV: capacity, refill tokens per ms, cost. Returns ms to wait (0 = granted).
# Time comes from Redis so workers on different hosts agree. Cost above capacity is clamped so a
# single oversized request cannot wait forever.
_BUCKET = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local cap = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local cost = math.min(tonumber(ARGV[3]), cap)
local s = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(s[1])
local ts = tonumber(s[2])
if tokens == nil then tokens = cap; ts = now end
tokens = math.min(cap, tokens + (now - ts) * rate)
local wait = 0
if tokens >= cost then tokens = tokens - cost else wait = math.ceil((cost - tokens) / rate) end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', now)
redis.call('PEXPIRE', KEYS[1], 120000)
return wait
"""


class RateLimiter(Protocol):
    def acquire(self, est_tokens: int) -> None: ...


class RedisTokenBucket:
    """``per_minute`` units per minute, burst capacity of one minute's worth."""

    def __init__(
        self,
        client: redis.Redis,
        key: str,
        per_minute: int,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if per_minute <= 0:
            raise ValueError("per_minute must be positive")
        self._script = client.register_script(_BUCKET)
        self._key, self._cap, self._rate = key, per_minute, per_minute / 60_000
        self._sleep = sleep

    def acquire(self, cost: int = 1) -> None:
        while True:
            wait_ms = int(
                self._script(keys=[self._key], args=[self._cap, self._rate, max(cost, 1)])
            )
            if wait_ms <= 0:
                return
            self._sleep(wait_ms / 1000 + 0.001)


class ModelRateLimiter:
    """Requests and tokens per model slug, from ``rpm``/``tpm`` in ``config/models.yaml``."""

    def __init__(
        self,
        client: redis.Redis,
        slug: str,
        rpm: int,
        tpm: int,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._requests = RedisTokenBucket(client, f"llm_rl:{slug}:rpm", rpm, sleep=sleep)
        self._tokens = RedisTokenBucket(client, f"llm_rl:{slug}:tpm", tpm, sleep=sleep)

    def acquire(self, est_tokens: int) -> None:
        self._requests.acquire(1)
        self._tokens.acquire(est_tokens)
