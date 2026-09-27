"""OpenRouter chat-completions transport (§10.2): strict schema, model fallback, retries."""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Sequence
from typing import Any

import httpx

from agents.base import ChatResponse, LLMTransientError, Message
from agents.llm.ratelimit import RateLimiter
from config.loader import ModelEntry
from contracts.models import Contract
from contracts.schema_export import response_format

BASE_URL = "https://openrouter.ai/api/v1"
MAX_RETRIES = 3
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 30.0
OUTPUT_TOKEN_RESERVE = 600  # verdicts are short; reserved from the tpm bucket up front
CHARS_PER_TOKEN = 3


class LLMRequestError(RuntimeError):
    """A non-retryable failure (bad key, rejected request, malformed body). A config bug."""


def build_payload(
    entry: ModelEntry, out_model: type[Contract], messages: Sequence[Message]
) -> dict[str, Any]:
    """The request body (§10.2). ``models`` is a sequential fallback array."""
    payload: dict[str, Any] = {
        "models": list(entry.all_slugs),
        "messages": list(messages),
        "response_format": response_format(out_model),
        "provider": {"require_parameters": True, "data_collection": "deny"},
    }
    if entry.primary.accepts_temperature:
        payload["temperature"] = 0.0
    if entry.primary.reasoning_effort is not None:
        payload["reasoning"] = {"effort": entry.primary.reasoning_effort.value}
    return payload


def estimate_tokens(messages: Sequence[Message]) -> int:
    return sum(len(m["content"]) for m in messages) // CHARS_PER_TOKEN + OUTPUT_TOKEN_RESERVE


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


class OpenRouterClient:
    """Implements ``agents.base.ChatClient``. The served model comes from ``response.model``."""

    def __init__(
        self,
        http: httpx.Client,
        *,
        api_key: str,
        entry: ModelEntry,
        out_model: type[Contract],
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        if not api_key:
            raise LLMRequestError("OPENROUTER_API_KEY is empty")
        self._http, self._key, self._entry, self._out = http, api_key, entry, out_model
        self._limiter, self._sleep, self._jitter = limiter, sleep, jitter

    def _delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after is not None:
            try:
                return min(max(float(retry_after), 0.0), BACKOFF_CAP_S)
            except ValueError:
                pass
        ceiling = min(BACKOFF_CAP_S, BACKOFF_BASE_S * float(2**attempt))
        return ceiling * (0.5 + 0.5 * self._jitter())

    def complete(self, messages: Sequence[Message]) -> ChatResponse:
        payload = build_payload(self._entry, self._out, messages)
        est = estimate_tokens(messages)
        last = "no attempt"
        for attempt in range(MAX_RETRIES + 1):
            if self._limiter is not None:
                self._limiter.acquire(est)  # every attempt spends quota
            retry_after: str | None = None
            try:
                resp = self._http.post(
                    f"{BASE_URL}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._key}"},
                )
            except httpx.TransportError as e:
                last = f"transport error: {type(e).__name__}"
            else:
                if resp.status_code == 200:
                    parsed = self._parse(resp)
                    if isinstance(parsed, ChatResponse):
                        return parsed
                    last = parsed
                elif _retryable(resp.status_code):
                    last, retry_after = f"HTTP {resp.status_code}", resp.headers.get("retry-after")
                else:
                    raise LLMRequestError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            if attempt < MAX_RETRIES:
                self._sleep(self._delay(attempt, retry_after))
        raise LLMTransientError(f"{last} after {MAX_RETRIES} retries")

    @staticmethod
    def _parse(resp: httpx.Response) -> ChatResponse | str:
        """The response, or a retryable-error description. Raises on a hard error."""
        try:
            body = resp.json()
        except ValueError as e:
            raise LLMRequestError("200 response is not JSON") from e
        if not isinstance(body, dict):
            raise LLMRequestError("200 response is not a JSON object")
        err = body.get("error")
        if err is not None:  # OpenRouter can report upstream failures inside a 200
            code = err.get("code") if isinstance(err, dict) else None
            if isinstance(code, int) and _retryable(code):
                return f"upstream error {code}"
            raise LLMRequestError(f"upstream error: {str(err)[:200]}")
        try:
            content = body["choices"][0]["message"]["content"]
            model = body["model"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMRequestError("response lacks choices[0].message.content or model") from e
        if not isinstance(content, str) or not isinstance(model, str) or not model:
            raise LLMRequestError("response content/model have the wrong type")
        usage = body.get("usage")
        return ChatResponse(model, content, usage if isinstance(usage, dict) else None)
