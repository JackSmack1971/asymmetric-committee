"""EDGAR limiter: ≤ 8 req/s across workers (§4.1, §17 P1 gate); User-Agent; 403/429 backoff."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import redis
import respx

from ingest.edgar_client import (
    EdgarClient,
    EdgarConfigError,
    RedisSlidingWindowLimiter,
    user_agent_from_env,
)

UA = "AsymmetricCommittee/1.0 (test@example.com)"
URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{:010d}.json"


def _max_in_window(times: list[float], window: float) -> int:
    times = sorted(times)
    best, j = 0, 0
    for i, t in enumerate(times):
        while times[j] <= t - window:
            j += 1
        best = max(best, i - j + 1)
    return best


def test_50_concurrent_fetches_stay_under_8_per_second(redis_client: redis.Redis) -> None:
    arrivals: list[float] = []
    lock = threading.Lock()

    def server(request: httpx.Request) -> httpx.Response:
        with lock:
            arrivals.append(time.monotonic())
        assert request.headers["User-Agent"] == UA
        return httpx.Response(200, json={"ok": True})

    # Two "workers", each with its own limiter object and HTTP client, sharing the Redis bucket.
    workers = [
        EdgarClient(RedisSlidingWindowLimiter(redis_client), user_agent=UA) for _ in range(2)
    ]
    with respx.mock(assert_all_called=True) as mock:
        mock.get(url__regex=r"https://data\.sec\.gov/.*").mock(side_effect=server)
        with ThreadPoolExecutor(max_workers=50) as pool:
            results = list(pool.map(lambda i: workers[i % 2].get_json(URL.format(i)), range(50)))
    assert len(results) == len(arrivals) == 50
    span = max(arrivals) - min(arrivals)
    assert _max_in_window(arrivals, 1.0) <= 8
    assert (len(arrivals) - 1) / span <= 8.0


@pytest.mark.parametrize("status", [429, 403])
def test_backoff_on_throttle(redis_client: redis.Redis, status: int) -> None:
    sleeps: list[float] = []
    client = EdgarClient(
        RedisSlidingWindowLimiter(redis_client), user_agent=UA, sleep=sleeps.append, backoff_s=2
    )
    with respx.mock() as mock:
        mock.get(URL.format(1)).mock(
            side_effect=[
                httpx.Response(status),
                httpx.Response(status, headers={"Retry-After": "7"}),
                httpx.Response(200, json={"cik": 1}),
            ]
        )
        assert client.get_json(URL.format(1)) == {"cik": 1}
    assert sleeps == [2.0, 7.0]


def test_gives_up_after_max_retries(redis_client: redis.Redis) -> None:
    client = EdgarClient(
        RedisSlidingWindowLimiter(redis_client), user_agent=UA, sleep=lambda _: None, max_retries=2
    )
    with respx.mock() as mock:
        route = mock.get(URL.format(1)).mock(return_value=httpx.Response(429))
        with pytest.raises(httpx.HTTPStatusError):
            client.get(URL.format(1))
    assert route.call_count == 3


def test_user_agent_is_required() -> None:
    with pytest.raises(EdgarConfigError):
        user_agent_from_env({})
    with pytest.raises(EdgarConfigError):
        user_agent_from_env({"SEC_USER_AGENT": "no contact"})
    assert user_agent_from_env({"SEC_USER_AGENT": UA}) == UA
