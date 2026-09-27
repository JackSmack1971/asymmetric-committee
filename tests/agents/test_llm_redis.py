"""Redis-backed token bucket, cache and DLQ. Needs Redis (TEST_REDIS_URL or a local binary)."""

from __future__ import annotations

import json
import threading

import redis

from agents.llm.ratelimit import ModelRateLimiter, RedisTokenBucket
from agents.llm.store import DLQ_KEY, RedisDeadLetterQueue, RedisVerdictCache


def test_bucket_grants_burst_then_waits(redis_client: redis.Redis) -> None:
    waits: list[float] = []
    bucket = RedisTokenBucket(redis_client, "b", 600, sleep=lambda s: waits.append(s))
    bucket.acquire(600)  # a full minute's burst is available at once, in one atomic grant
    assert waits == []
    # Drained: the next call must wait ~100 ms (600/min = 10/s). One grant, not 600 round trips,
    # so real refill during the drain cannot hide the wait.
    real: list[float] = []

    def sleep(s: float) -> None:
        real.append(s)
        import time

        time.sleep(s)

    RedisTokenBucket(redis_client, "b", 600, sleep=sleep).acquire(1)
    assert real and 0 < real[0] <= 0.2


def test_bucket_is_shared_across_workers(redis_client: redis.Redis) -> None:
    granted: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        b = RedisTokenBucket(redis.Redis(connection_pool=redis_client.connection_pool), "s", 60)
        for _ in range(30):
            wait = int(b._script(keys=["s"], args=[60, 60 / 60_000, 1]))
            if wait <= 0:
                with lock:
                    granted.append(1)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert 60 <= len(granted) <= 62  # capacity 60 (+ at most a refilled token during the run)


def test_oversized_cost_is_clamped_not_deadlocked(redis_client: redis.Redis) -> None:
    RedisTokenBucket(redis_client, "big", 100, sleep=lambda s: None).acquire(10_000)


def test_model_limiter_spends_both_buckets(redis_client: redis.Redis) -> None:
    ModelRateLimiter(redis_client, "m/x", rpm=10, tpm=1000, sleep=lambda s: None).acquire(400)
    assert redis_client.exists("llm_rl:m/x:rpm") and redis_client.exists("llm_rl:m/x:tpm")
    tokens = float(redis_client.hget("llm_rl:m/x:tpm", "tokens"))  # type: ignore[arg-type]
    assert 590 <= tokens <= 610


def test_cache_round_trip_and_dlq(redis_client: redis.Redis) -> None:
    cache = RedisVerdictCache(redis_client)
    assert cache.get("k") is None
    cache.set("k", "v")
    assert cache.get("k") == "v"
    RedisDeadLetterQueue(redis_client).push({"agent": "value"})
    record = json.loads(redis_client.lpop(DLQ_KEY))  # type: ignore[arg-type]
    assert record["agent"] == "value" and "at" in record
