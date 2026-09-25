"""Alpaca daily bars (§4.1). Market-data host only; this module never touches a trading endpoint.

History older than 15 minutes comes from the SIP feed with ``available_at = session close + 15m``.
Live bars come from the free IEX feed with ``available_at = receipt time``. Prices are unadjusted
(``adjustment=raw``): split-adjusting history would leak later corporate actions (look-ahead).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from contracts.data import PriceBar
from contracts.enums import PriceFeed
from ingest.timeutil import ET, parse_utc, session_close

DATA_URL = "https://data.alpaca.markets"
SIP_DELAY = timedelta(minutes=15)


def parse_bars(
    payload: dict[str, Any],
    security_id: int,
    feed: PriceFeed,
    *,
    received_at: datetime | None = None,
) -> list[PriceBar]:
    """One page of ``/v2/stocks/{symbol}/bars``. Live (IEX) bars need ``received_at``."""
    if feed is PriceFeed.IEX and received_at is None:
        raise ValueError("live IEX bars are only knowable at receipt time")
    out = []
    for b in payload.get("bars") or []:
        start = parse_utc(b["t"])  # daily bars are stamped at midnight ET
        day = start.astimezone(ET).date()
        if feed is PriceFeed.SIP:
            available = session_close(day).astimezone(UTC) + SIP_DELAY
        else:
            assert received_at is not None
            available = received_at
        out.append(
            PriceBar(
                security_id=security_id,
                event_time=start,
                available_at=available,
                source_version=feed.value,
                open=b["o"],
                high=b["h"],
                low=b["l"],
                close=b["c"],
                volume=b["v"],
                feed=feed,
            )
        )
    return out


class AlpacaBars:
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
            headers={
                "APCA-API-KEY-ID": key_id or os.environ.get("ALPACA_API_KEY_ID", ""),
                "APCA-API-SECRET-KEY": secret or os.environ.get("ALPACA_API_SECRET", ""),
            },
        )

    def _pages(self, symbol: str, params: dict[str, str]) -> Iterator[dict[str, Any]]:
        token: str | None = None
        while True:
            q = {**params, **({"page_token": token} if token else {})}
            r = self._http.get(f"/v2/stocks/{symbol}/bars", params=q)
            r.raise_for_status()
            page: dict[str, Any] = r.json()
            yield page
            token = page.get("next_page_token")
            if not token:
                return

    def history(
        self, symbol: str, security_id: int, start: date, end: date, *, now: datetime
    ) -> list[PriceBar]:
        """SIP daily bars for ``[start, end]``, clipped to what is at least 15 minutes old."""
        end_ts = min(session_close(end).astimezone(UTC), now - SIP_DELAY)
        params = {
            "timeframe": "1Day",
            "start": start.isoformat(),
            "end": end_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "feed": PriceFeed.SIP.value,
            "adjustment": "raw",
            "limit": "10000",
        }
        bars = [
            b
            for page in self._pages(symbol, params)
            for b in parse_bars(page, security_id, PriceFeed.SIP)
        ]
        return [b for b in bars if b.available_at <= now]

    def live(
        self, symbol: str, security_id: int, day: date, *, received_at: datetime
    ) -> list[PriceBar]:
        """Today's bar from the free real-time IEX feed."""
        params = {
            "timeframe": "1Day",
            "start": day.isoformat(),
            "feed": PriceFeed.IEX.value,
            "adjustment": "raw",
        }
        return [
            b
            for page in self._pages(symbol, params)
            for b in parse_bars(page, security_id, PriceFeed.IEX, received_at=received_at)
        ]

    def close(self) -> None:
        self._http.close()
