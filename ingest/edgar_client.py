"""SEC EDGAR client (§4.1): one global limit across every worker, declared User-Agent, backoff.

The SEC allows 10 requests/s per user across all machines and blocks offenders for ~10 minutes.
We stay at 8: a Redis sliding-window log (atomic Lua, Redis clock) admits at most ``limit``
requests per ``window_s`` for every process sharing the key. The default window has a 5% margin so
network jitter between admission and arrival cannot push a server-side 1 s window over 8.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from typing import Any, Protocol

import httpx
import redis

EDGAR_LIMIT = 8
EDGAR_WINDOW_S = 1.05
LIMITER_KEY = "ratelimit:sec_edgar"

_SLIDING_WINDOW = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000000 + tonumber(t[2])
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
if redis.call('ZCARD', KEYS[1]) < limit then
  redis.call('ZADD', KEYS[1], now, ARGV[3])
  redis.call('PEXPIRE', KEYS[1], math.ceil(window / 1000) + 1000)
  return 0
end
local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
return tonumber(oldest[2]) + window - now
"""


class Limiter(Protocol):
    def acquire(self) -> None: ...


class RedisSlidingWindowLimiter:
    """At most ``limit`` acquisitions per ``window_s`` across all processes sharing ``key``."""

    def __init__(
        self,
        client: redis.Redis,
        *,
        key: str = LIMITER_KEY,
        limit: int = EDGAR_LIMIT,
        window_s: float = EDGAR_WINDOW_S,
    ) -> None:
        self._script = client.register_script(_SLIDING_WINDOW)
        self.key, self.limit, self.window_us = key, limit, int(window_s * 1_000_000)

    def acquire(self) -> None:
        while True:
            wait_us = int(
                self._script(keys=[self.key], args=[self.window_us, self.limit, uuid.uuid4().hex])
            )
            if wait_us <= 0:
                return
            time.sleep(wait_us / 1_000_000 + 0.001)


class LocalSlidingWindowLimiter:
    """In-process equivalent; only for replaying fixtures, never for live EDGAR traffic."""

    def __init__(self, *, limit: int = EDGAR_LIMIT, window_s: float = EDGAR_WINDOW_S) -> None:
        self.limit, self.window_s = limit, window_s
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._times and self._times[0] <= now - self.window_s:
                    self._times.popleft()
                if len(self._times) < self.limit:
                    self._times.append(now)
                    return
                wait = self._times[0] + self.window_s - now
            time.sleep(wait + 0.001)


class EdgarConfigError(RuntimeError):
    pass


def user_agent_from_env(env: dict[str, str] | None = None) -> str:
    ua = (os.environ if env is None else env).get("SEC_USER_AGENT", "").strip()
    if "@" not in ua:
        raise EdgarConfigError("SEC_USER_AGENT must be set and include a contact email (§4.1)")
    return ua


RETRY_STATUS = frozenset({403, 429, 500, 502, 503, 504})


class EdgarClient:
    def __init__(
        self,
        limiter: Limiter,
        *,
        user_agent: str | None = None,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 5,
        backoff_s: float = 2.0,
        max_backoff_s: float = 600.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.limiter = limiter
        self.max_retries, self.backoff_s, self.max_backoff_s = max_retries, backoff_s, max_backoff_s
        self._sleep = sleep
        self._http = httpx.Client(
            transport=transport,
            timeout=30.0,
            headers={
                "User-Agent": user_agent or user_agent_from_env(),
                "Accept-Encoding": "gzip, deflate",
            },
        )

    def get(self, url: str) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            self.limiter.acquire()
            response = self._http.get(url)
            if response.status_code not in RETRY_STATUS or attempt == self.max_retries:
                response.raise_for_status()
                return response
            self._sleep(self._delay(response, attempt))
        raise AssertionError("unreachable")

    def _delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After", "")
        if retry_after.isdigit():
            return min(float(retry_after), self.max_backoff_s)
        return min(self.backoff_s * 2.0**attempt, self.max_backoff_s)

    def get_json(self, url: str) -> Any:
        return self.get(url).json()

    def get_text(self, url: str) -> str:
        return self.get(url).text

    def close(self) -> None:
        self._http.close()
