"""A small stateful stand-in for the Alpaca paper REST API, served through ``httpx.MockTransport``.

It speaks the wire format ``AlpacaPaperGateway`` really uses, so tests drive the production gateway
end to end without a network. Behaviour is deliberately simple and explicit:

- a limit order fills completely at its limit price on submit, unless its symbol is in ``partial``
  (then half fills and the rest stays open until cancelled) or ``never`` (nothing fills);
- a market order fills completely at ``market_price`` on submit;
- ``DELETE /v2/orders/{id}`` cancels an open order (422 if it is already terminal);
- ``client_order_id`` is unique: a duplicate submit is a 422 naming it, like the real API.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

import httpx

TERMINAL = {"filled", "canceled", "expired", "rejected", "done_for_day"}


class FakeAlpaca:
    def __init__(self, now: Callable[[], datetime]) -> None:
        self.now = now
        self.orders: dict[str, dict[str, Any]] = {}
        self.positions: dict[str, float] = {}
        self.equity = 100_000.0
        self.last_equity = 100_000.0
        self.history: list[float] = [100_000.0]
        self.market_price = 10.0
        self.partial: set[str] = set()
        self.never: set[str] = set()
        self.calendar: list[dict[str, str]] = []
        self.requests: list[httpx.Request] = []
        self.closed: list[str] = []
        self.fail_next: list[int] = []  # status codes returned by the next requests, one each

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    # -- helpers --------------------------------------------------------------------------------

    @property
    def submitted(self) -> list[str]:
        return [o["client_order_id"] for o in self.orders.values()]

    def _iso(self) -> str:
        return self.now().isoformat().replace("+00:00", "Z")

    def _by_client(self, cid: str) -> dict[str, Any] | None:
        return next((o for o in self.orders.values() if o["client_order_id"] == cid), None)

    def _fill(self, order: dict[str, Any], qty: float, price: float) -> None:
        total = float(order["filled_qty"]) + qty
        prior = float(order["filled_avg_price"] or 0.0) * float(order["filled_qty"])
        order["filled_qty"] = str(total)
        order["filled_avg_price"] = str((prior + qty * price) / total)
        sign = 1.0 if order["side"] == "buy" else -1.0
        sym = order["symbol"]
        self.positions[sym] = self.positions.get(sym, 0.0) + sign * qty
        if total >= float(order["qty"]) - 1e-9:
            order["status"] = "filled"
            order["filled_at"] = self._iso()
        else:
            order["status"] = "partially_filled"

    # -- the API --------------------------------------------------------------------------------

    def _handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0), text="upstream failure")
        path, method = req.url.path, req.method
        if method == "GET" and path == "/v2/account":
            return _ok({"equity": str(self.equity), "last_equity": str(self.last_equity)})
        if method == "GET" and path == "/v2/account/portfolio/history":
            return _ok({"equity": self.history})
        if method == "GET" and path == "/v2/positions":
            return _ok([{"symbol": s, "qty": str(q)} for s, q in self.positions.items() if q != 0])
        if method == "GET" and path == "/v2/calendar":
            lo = date.fromisoformat(req.url.params["start"])
            hi = date.fromisoformat(req.url.params["end"])
            return _ok([c for c in self.calendar if lo <= date.fromisoformat(c["date"]) <= hi])
        if method == "GET" and path == "/v2/orders:by_client_order_id":
            found = self._by_client(req.url.params["client_order_id"])
            return _ok(found) if found else httpx.Response(404, json={"message": "not found"})
        if method == "GET" and path == "/v2/orders":
            return _ok([o for o in self.orders.values() if o["status"] not in TERMINAL])
        if method == "POST" and path == "/v2/orders":
            return self._create(json.loads(req.content))
        if match := re.fullmatch(r"/v2/orders/([^/]+)", path):
            order = self.orders.get(match.group(1))
            if method == "GET":
                return _ok(order) if order else httpx.Response(404, json={"message": "nope"})
            if method == "DELETE":
                if order is None or order["status"] in TERMINAL:
                    return httpx.Response(422, json={"message": "order not cancelable"})
                order["status"] = "canceled"
                return httpx.Response(204)
        if method == "DELETE" and (m := re.fullmatch(r"/v2/positions/([^/]+)", path)):
            self.closed.append(m.group(1))
            self.positions[m.group(1)] = 0.0
            return _ok({})
        return httpx.Response(404, json={"message": f"unhandled {method} {path}"})

    def _create(self, body: dict[str, Any]) -> httpx.Response:
        cid = body["client_order_id"]
        if self._by_client(cid) is not None:
            return httpx.Response(422, json={"message": f"client_order_id {cid} must be unique"})
        oid = f"alpaca-{len(self.orders) + 1}"
        order: dict[str, Any] = {
            "id": oid,
            "client_order_id": cid,
            "symbol": body["symbol"],
            "side": body["side"],
            "type": body["type"],
            "qty": body["qty"],
            "filled_qty": "0",
            "limit_price": body.get("limit_price"),
            "filled_avg_price": None,
            "status": "new",
            "submitted_at": self._iso(),
            "created_at": self._iso(),
            "filled_at": None,
        }
        self.orders[oid] = order
        qty, symbol = float(body["qty"]), body["symbol"]
        if body["type"] == "market":
            self._fill(order, qty, self.market_price)
        elif symbol in self.never:
            pass
        elif symbol in self.partial:
            self._fill(order, qty / 2, float(body["limit_price"]))
        else:
            self._fill(order, qty, float(body["limit_price"]))
        return _ok(order)


def _ok(payload: Any) -> httpx.Response:
    return httpx.Response(200, json=payload)


def session_row(day: date, *, close: str = "16:00") -> dict[str, str]:
    return {"date": day.isoformat(), "open": "09:30", "close": close}


def weekday_calendar(
    start: date, days: int, holidays: tuple[date, ...] = ()
) -> list[dict[str, str]]:
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        if d.weekday() < 5 and d not in holidays:
            out.append(session_row(d))
    return out
