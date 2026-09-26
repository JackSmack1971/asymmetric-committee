"""Redis-backed verdict cache and dead-letter queue (§10.2)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import redis

DLQ_KEY = "llm_dlq"


class RedisVerdictCache:
    def __init__(self, client: redis.Redis) -> None:
        self._client = client

    def get(self, key: str) -> str | None:
        value = self._client.get(key)
        if value is None:
            return None
        return value.decode() if isinstance(value, bytes) else str(value)

    def set(self, key: str, value: str) -> None:
        self._client.set(key, value)  # no TTL: replays must stay reproducible


class RedisDeadLetterQueue:
    def __init__(self, client: redis.Redis, key: str = DLQ_KEY) -> None:
        self._client, self._key = client, key

    def push(self, record: dict[str, str]) -> None:
        stamped = {**record, "at": datetime.now(UTC).isoformat()}
        self._client.rpush(self._key, json.dumps(stamped))
