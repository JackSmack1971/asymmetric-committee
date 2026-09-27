"""Execution reference price (§9): IEX quote midpoint when tight, else a delayed SIP trade.

The free real-time feed is IEX only, while the paper engine matches against the SIP NBBO, so an IEX
midpoint is trusted only when the IEX spread is at most 50 bps. Otherwise the latest SIP trade that
is at least 15 minutes old is used. The source is always returned so it can be recorded. The market
data lookup is a small protocol (``QuoteSource``) so the rule is testable without any network.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Protocol

import httpx

from contracts.enums import ReferenceSource
from execution.alpaca import parse_utc

DATA_URL = "https://data.alpaca.markets"
MAX_IEX_SPREAD_BPS = 50.0
SIP_DELAY = timedelta(minutes=15)


class NoReferencePriceError(RuntimeError):
    """Neither an acceptable IEX midpoint nor an eligible SIP trade exists: do not trade."""


class QuoteSource(Protocol):
    def iex_quote(self, symbol: str) -> tuple[float, float] | None:
        """Latest IEX ``(bid, ask)``, or None when there is no usable quote."""
        ...

    def sip_last_trade(self, symbol: str, *, at_or_before: datetime) -> float | None:
        """Price of the latest SIP trade at or before the given time."""
        ...


def _positive(x: float | None) -> bool:
    return x is not None and math.isfinite(x) and x > 0


def reference_price(
    source: QuoteSource, symbol: str, now: datetime, *, max_spread_bps: float = MAX_IEX_SPREAD_BPS
) -> tuple[float, ReferenceSource]:
    quote = source.iex_quote(symbol)
    if quote is not None:
        bid, ask = quote
        if _positive(bid) and _positive(ask) and ask >= bid:
            mid = (bid + ask) / 2
            if (ask - bid) / mid * 10_000 <= max_spread_bps:
                return mid, ReferenceSource.IEX_MID
    trade = source.sip_last_trade(symbol, at_or_before=now - SIP_DELAY)
    if _positive(trade):
        assert trade is not None
        return trade, ReferenceSource.SIP_LAST
    raise NoReferencePriceError(f"no eligible reference price for {symbol}")


class AlpacaMarketData:
    """``QuoteSource`` over the Alpaca market-data host. Never touches the trading host."""

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        *,
        key_id: str = "",
        secret: str = "",
    ) -> None:
        self._http = httpx.Client(
            base_url=DATA_URL,
            transport=transport,
            timeout=30.0,
            headers={"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret},
        )

    def iex_quote(self, symbol: str) -> tuple[float, float] | None:
        r = self._http.get(f"/v2/stocks/{symbol}/quotes/latest", params={"feed": "iex"})
        r.raise_for_status()
        q: dict[str, Any] = r.json().get("quote") or {}
        bid, ask = q.get("bp"), q.get("ap")
        return None if bid is None or ask is None else (float(bid), float(ask))

    def sip_last_trade(self, symbol: str, *, at_or_before: datetime) -> float | None:
        r = self._http.get(
            f"/v2/stocks/{symbol}/trades",
            params={
                "feed": "sip",
                "end": at_or_before.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "start": (at_or_before - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "sort": "desc",
                "limit": "1",
            },
        )
        r.raise_for_status()
        trades = r.json().get("trades") or []
        if not trades:
            return None
        if parse_utc(trades[0]["t"]) > at_or_before:  # never use a trade inside the delay window
            return None
        return float(trades[0]["p"])

    def close(self) -> None:
        self._http.close()
