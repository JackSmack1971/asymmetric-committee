"""Historical SIP trades and trade-condition metadata (§4.6). Market-data host only.

Backtest and halt-reconstruction references need the SIP trade sequence, so every request states
``feed=sip`` explicitly. A refusal (401/403), an unsupported request (400/422) or a page chain that
cannot be completed is an error that the caller turns into an unresolved reference; nothing here
ever falls back to a bar.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx

from contracts.enums import Tape
from contracts.market_data import SipTrade
from ingest.alpaca import DATA_URL

MAX_PAGES = 400  # 4M trades at the page size below; more than this is treated as incomplete
PAGE_LIMIT = 10_000
_REFUSED = frozenset({400, 401, 403, 422})
_FRACTION = re.compile(r"\.(\d+)")


class SipEntitlementError(RuntimeError):
    """The SIP trade history was refused or is unsupported for this account/request."""


class IncompleteTradesError(RuntimeError):
    """The trade sequence could not be read completely; a partial sequence decides nothing."""


def parse_trade_time(s: str) -> datetime:
    """RFC 3339 with up to nanoseconds; kept to microseconds (finer ties are ambiguous anyway)."""
    fixed = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), s.replace("Z", "+00:00"))
    dt = datetime.fromisoformat(fixed)
    if dt.tzinfo is None:
        raise ValueError(f"trade timestamp without zone: {s!r}")
    return dt.astimezone(UTC)


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_trades(symbol: str, payload: dict[str, Any]) -> list[SipTrade]:
    out: list[SipTrade] = []
    for tr in payload.get("trades") or []:
        conditions = tuple(str(c) for c in (tr.get("c") or ()))
        out.append(
            SipTrade(
                symbol=symbol,
                time=parse_trade_time(tr["t"]),
                price=tr["p"],
                size=tr.get("s", 0),
                tape=Tape(tr["z"]),
                conditions=conditions,
                trade_id=str(tr["i"]) if tr.get("i") is not None else None,
            )
        )
    return out


class AlpacaTrades:
    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        *,
        key_id: str | None = None,
        secret: str | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=DATA_URL,
            transport=transport,
            timeout=30.0,
            follow_redirects=False,
            headers={
                "APCA-API-KEY-ID": key_id or os.environ.get("ALPACA_API_KEY_ID", ""),
                "APCA-API-SECRET-KEY": secret or os.environ.get("ALPACA_API_SECRET", ""),
            },
        )

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        r = self._http.get(path, params=params)
        if r.status_code in _REFUSED:
            raise SipEntitlementError(f"{path} refused with HTTP {r.status_code}")
        r.raise_for_status()  # 429 / 5xx stay transient errors for the task's retry policy
        payload: dict[str, Any] = r.json()
        return payload

    def _pages(self, symbol: str, params: dict[str, str]) -> Iterator[dict[str, Any]]:
        token: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            q = {**params, **({"page_token": token} if token else {})}
            page = self._get(f"/v2/stocks/{symbol}/trades", q)
            yield page
            token = page.get("next_page_token")
            if not token:
                return
            if token in seen:
                raise IncompleteTradesError(f"{symbol}: page token repeated")
            seen.add(token)
        raise IncompleteTradesError(f"{symbol}: more than {MAX_PAGES} pages of trades")

    def historical_trades(self, symbol: str, start: datetime, end: datetime) -> list[SipTrade]:
        """Every SIP trade in ``[start, end]``, oldest first, or an error (never a partial list)."""
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start and end must be timezone-aware")
        params = {
            "start": _rfc3339(start),
            "end": _rfc3339(end),
            "feed": "sip",
            "sort": "asc",
            "limit": str(PAGE_LIMIT),
        }
        trades = [t for page in self._pages(symbol, params) for t in parse_trades(symbol, page)]
        return [t for t in trades if start <= t.time <= end]

    def conditions(self, tape: Tape) -> dict[str, str]:
        """``/v2/stocks/meta/conditions/trade?tape=`` as {code: name} (for the smoke probe)."""
        payload = self._get("/v2/stocks/meta/conditions/trade", {"tape": tape.value})
        return {str(k): str(v) for k, v in payload.items()}

    def close(self) -> None:
        self._http.close()
