"""Alpaca **paper** trading gateway over httpx (§9). There is no live-trading code path.

Fail closed: the trading host is fixed to ``paper-api.alpaca.markets``. A configured base URL (or
the SDK's ``APCA_API_BASE_URL``) that names anything else raises ``LiveTradingError`` before any
request is built, and a request hook re-checks the host of every outgoing request. Redirects are
never followed. (httpx rather than alpaca-py: the SDK is not a project dependency and httpx is
what every other Alpaca module here uses, so tests stay transport-mocked.)
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, date, datetime, time
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from contracts.enums import BrokerOrderStatus, OrderKind, OrderSide
from contracts.models import BrokerOrder, MarketSession
from execution.gateway import BrokerError, DuplicateClientOrderError, OrderNotCancelableError

PAPER_HOST = "paper-api.alpaca.markets"
PAPER_URL = f"https://{PAPER_HOST}"
_URL_ENV_VARS = ("ALPACA_PAPER_BASE_URL", "APCA_API_BASE_URL", "ALPACA_BASE_URL")

_STATUS = {
    "filled": BrokerOrderStatus.FILLED,
    "canceled": BrokerOrderStatus.CANCELED,
    "expired": BrokerOrderStatus.EXPIRED,
    "done_for_day": BrokerOrderStatus.DONE_FOR_DAY,
    "rejected": BrokerOrderStatus.REJECTED,
    "partially_filled": BrokerOrderStatus.PARTIALLY_FILLED,
    "pending_cancel": BrokerOrderStatus.PENDING_CANCEL,
}


def parse_utc(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"timestamp without zone: {s!r}")
    return dt.astimezone(UTC)


class LiveTradingError(RuntimeError):
    """A configuration could target something other than the Alpaca paper endpoint."""


def _check_paper_url(url: str) -> str:
    parts = urlsplit(url.strip())
    ok = (
        parts.scheme == "https"
        and parts.hostname == PAPER_HOST
        and parts.port in (None, 443)
        and parts.username is None
        and parts.password is None
        and parts.path.rstrip("/") in ("", "/v2")
        and not parts.query
        and not parts.fragment
    )
    if not ok:
        raise LiveTradingError(f"refusing non-paper Alpaca trading URL: {url!r}")
    return PAPER_URL


def paper_base_url(env: Mapping[str, str] | None = None) -> str:
    """The paper URL, after rejecting every configured trading URL that is not exactly paper."""
    env = os.environ if env is None else env
    for name in _URL_ENV_VARS:
        value = env.get(name, "").strip()
        if value:
            _check_paper_url(value)
    return PAPER_URL


def _paper_only(request: httpx.Request) -> None:
    if request.url.host != PAPER_HOST or request.url.scheme != "https":
        raise LiveTradingError(f"refusing request to {request.url.host!r}")


def parse_order(payload: Mapping[str, Any]) -> BrokerOrder:
    def num(key: str) -> float | None:
        raw = payload.get(key)
        return None if raw in (None, "") else float(raw)

    return BrokerOrder(
        broker_order_id=payload["id"],
        client_order_id=payload["client_order_id"],
        symbol=payload["symbol"],
        side=OrderSide(payload["side"]),
        kind=OrderKind(payload["type"]),
        qty=float(payload["qty"]),
        filled_qty=float(payload.get("filled_qty") or 0.0),
        limit_price=num("limit_price"),
        filled_avg_price=num("filled_avg_price"),
        status=_STATUS.get(str(payload["status"]), BrokerOrderStatus.OPEN),
        submitted_at=parse_utc(payload.get("submitted_at") or payload["created_at"]),
        filled_at=parse_utc(payload["filled_at"]) if payload.get("filled_at") else None,
    )


_ET = ZoneInfo("America/New_York")


def parse_session(payload: Mapping[str, Any]) -> MarketSession:
    """One row of ``GET /v2/calendar``: ``date`` plus Eastern ``open``/``close`` (early closes
    included, which is why this, not a fixed 16:00, is the scheduling authority)."""
    day = date.fromisoformat(str(payload["date"]))
    opens = time.fromisoformat(str(payload["open"]))
    closes = time.fromisoformat(str(payload["close"]))
    return MarketSession(
        session_date=day,
        opens_at=datetime.combine(day, opens, _ET),
        closes_at=datetime.combine(day, closes, _ET),
    )


class AlpacaPaperGateway:
    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        *,
        key_id: str | None = None,
        secret: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        base = paper_base_url(env)
        source = os.environ if env is None else env
        self._http = httpx.Client(
            base_url=base,
            transport=transport,
            timeout=30.0,
            follow_redirects=False,
            event_hooks={"request": [_paper_only]},
            headers={
                "APCA-API-KEY-ID": key_id or source.get("ALPACA_API_KEY_ID", ""),
                "APCA-API-SECRET-KEY": secret or source.get("ALPACA_API_SECRET", ""),
            },
        )

    def _send(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            return self._http.request(method, path, **kw)
        except httpx.HTTPError as e:  # network failure: outcome unknown, caller must reconcile
            raise BrokerError(f"{method} {path}: {e}") from e

    @staticmethod
    def _ok(r: httpx.Response) -> Any:
        if r.status_code >= 300:
            raise BrokerError(f"{r.request.method} {r.request.url.path}: {r.status_code} {r.text}")
        return r.json() if r.content else None

    def find_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        r = self._send(
            "GET", "/v2/orders:by_client_order_id", params={"client_order_id": client_order_id}
        )
        if r.status_code == 404:
            return None
        return parse_order(self._ok(r))

    def get_order(self, broker_order_id: str) -> BrokerOrder:
        return parse_order(self._ok(self._send("GET", f"/v2/orders/{broker_order_id}")))

    def _submit(self, body: dict[str, Any]) -> BrokerOrder:
        r = self._send("POST", "/v2/orders", json={**body, "time_in_force": "day"})
        if r.status_code == 422 and "client_order_id" in r.text:
            raise DuplicateClientOrderError(r.text)
        return parse_order(self._ok(r))

    def submit_limit(
        self, *, client_order_id: str, symbol: str, side: OrderSide, qty: float, limit_price: float
    ) -> BrokerOrder:
        return self._submit(
            {
                "client_order_id": client_order_id,
                "symbol": symbol,
                "side": side.value,
                "type": "limit",
                "qty": str(qty),
                "limit_price": f"{limit_price:.2f}",
            }
        )

    def submit_market(
        self, *, client_order_id: str, symbol: str, side: OrderSide, qty: float
    ) -> BrokerOrder:
        return self._submit(
            {
                "client_order_id": client_order_id,
                "symbol": symbol,
                "side": side.value,
                "type": "market",
                "qty": str(qty),
            }
        )

    def cancel_order(self, broker_order_id: str) -> None:
        r = self._send("DELETE", f"/v2/orders/{broker_order_id}")
        if r.status_code in (404, 422):  # unknown or no longer cancelable (already terminal)
            raise OrderNotCancelableError(r.text)
        self._ok(r)

    def open_orders(self) -> list[BrokerOrder]:
        rows = self._ok(self._send("GET", "/v2/orders", params={"status": "open", "limit": 500}))
        return [parse_order(p) for p in rows]

    def positions(self) -> dict[str, float]:
        rows = self._ok(self._send("GET", "/v2/positions"))
        return {p["symbol"]: float(p["qty"]) for p in rows}

    def close_position(self, symbol: str) -> None:
        self._ok(self._send("DELETE", f"/v2/positions/{symbol}"))

    def account_equity(self) -> tuple[float, float]:
        acct = self._ok(self._send("GET", "/v2/account"))
        return float(acct["equity"]), float(acct["last_equity"])

    def sessions(self, start: date, end: date) -> list[MarketSession]:
        """Trading sessions in ``[start, end]`` from Alpaca's official calendar, oldest first."""
        rows = self._ok(
            self._send(
                "GET", "/v2/calendar", params={"start": start.isoformat(), "end": end.isoformat()}
            )
        )
        return sorted((parse_session(r) for r in rows or []), key=lambda s: s.session_date)

    def equity_history(self) -> list[float]:
        """Daily equity from the broker's portfolio history. Nulls before the first data point are
        dropped; a null after it is a gap in the evidence and raises rather than being skipped."""
        body = self._ok(
            self._send(
                "GET",
                "/v2/account/portfolio/history",
                params={"period": "1A", "timeframe": "1D", "intraday_reporting": "market_hours"},
            )
        )
        raw = body.get("equity") or []
        out: list[float] = []
        for v in raw:
            if v is None:
                if out:
                    raise BrokerError("portfolio history has a gap after its first value")
                continue
            out.append(float(v))
        return out
