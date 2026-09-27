"""In-memory broker, clock and recorder. No network, no credentials."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from contracts.enums import BrokerOrderStatus as S
from contracts.enums import OrderKind, OrderSide
from contracts.models import BrokerOrder, ExecutionRecord
from execution.gateway import (
    BrokerError,
    DuplicateClientOrderError,
    OrderNotCancelableError,
)

T0 = datetime(2024, 3, 4, 14, 30, tzinfo=UTC)
RUN = UUID("00000000-0000-0000-0000-000000000042")


class FakeClock:
    def __init__(self) -> None:
        self.now = T0
        self.slept: list[float] = []
        self.on_sleep: list[Callable[[], object]] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)
        if self.on_sleep:
            self.on_sleep.pop(0)()


@dataclass
class FakeBroker:
    clock: FakeClock
    orders: dict[str, BrokerOrder] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    submits: list[str] = field(default_factory=list)
    pos: dict[str, float] = field(default_factory=dict)
    equity: tuple[float, float] = (100_000.0, 100_000.0)
    history: list[float] = field(default_factory=lambda: [100_000.0])
    cancel_mode: str = "confirm"  # confirm | stuck | race_fill
    lose_submit_response: bool = False  # order accepted, but the caller sees an exception
    closed: list[str] = field(default_factory=list)

    def _by_client(self, cid: str) -> BrokerOrder | None:
        return next((o for o in self.orders.values() if o.client_order_id == cid), None)

    def _put(self, o: BrokerOrder) -> BrokerOrder:
        self.orders[o.broker_order_id] = o
        return o

    def find_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        self.calls.append("find")
        return self._by_client(client_order_id)

    def get_order(self, broker_order_id: str) -> BrokerOrder:
        self.calls.append("get")
        return self.orders[broker_order_id]

    def _new(self, cid: str, symbol: str, side: OrderSide, qty: float, kind: OrderKind,
             limit: float | None) -> BrokerOrder:  # fmt: skip
        if self._by_client(cid) is not None:
            raise DuplicateClientOrderError(cid)
        self.submits.append(cid)
        o = BrokerOrder(
            broker_order_id=f"B{len(self.orders) + 1}",
            client_order_id=cid,
            symbol=symbol,
            side=side,
            kind=kind,
            qty=qty,
            filled_qty=0.0,
            limit_price=limit,
            status=S.OPEN,
            submitted_at=self.clock.now,
        )
        self._put(o)
        if self.lose_submit_response:
            self.lose_submit_response = False
            raise BrokerError("timeout after accept")
        return o

    def submit_limit(self, *, client_order_id: str, symbol: str, side: OrderSide, qty: float,
                     limit_price: float) -> BrokerOrder:  # fmt: skip
        self.calls.append("submit_limit")
        return self._new(client_order_id, symbol, side, qty, OrderKind.LIMIT, limit_price)

    def submit_market(self, *, client_order_id: str, symbol: str, side: OrderSide,
                      qty: float) -> BrokerOrder:  # fmt: skip
        self.calls.append("submit_market")
        o = self._new(client_order_id, symbol, side, qty, OrderKind.MARKET, None)
        return self.fill(o.broker_order_id, qty, 101.0)  # market orders fill at once

    def fill(self, broker_id: str, qty: float, price: float) -> BrokerOrder:
        """Apply a (further) fill of ``qty`` at ``price`` to an order."""
        o = self.orders[broker_id]
        total = round(o.filled_qty + qty, 6)
        prev = o.filled_avg_price or 0.0
        avg = (prev * o.filled_qty + price * qty) / total
        status = S.FILLED if total >= o.qty else S.PARTIALLY_FILLED
        return self._put(
            o.model_copy(
                update={
                    "filled_qty": total,
                    "filled_avg_price": avg,
                    "status": status,
                    "filled_at": self.clock.now,
                }
            )
        )

    def set_status(self, broker_id: str, status: S) -> None:
        self._put(self.orders[broker_id].model_copy(update={"status": status}))

    def cancel_order(self, broker_order_id: str) -> None:
        self.calls.append("cancel")
        o = self.orders[broker_order_id]
        if o.status.terminal:
            raise OrderNotCancelableError("already terminal")
        if self.cancel_mode == "confirm":
            self.set_status(broker_order_id, S.CANCELED)
        elif self.cancel_mode == "stuck":
            self.set_status(broker_order_id, S.PENDING_CANCEL)
        elif self.cancel_mode == "race_fill":  # the rest fills while the cancel is in flight
            self.fill(broker_order_id, o.qty - o.filled_qty, 100.0)
            raise OrderNotCancelableError("filled")

    def open_orders(self) -> list[BrokerOrder]:
        self.calls.append("open_orders")
        return [o for o in self.orders.values() if not o.status.terminal]

    def positions(self) -> dict[str, float]:
        self.calls.append("positions")
        return dict(self.pos)

    def close_position(self, symbol: str) -> None:
        self.calls.append("close_position")
        assert not self.open_orders_now(), "close_position with open orders"
        self.closed.append(symbol)

    def open_orders_now(self) -> list[BrokerOrder]:
        return [o for o in self.orders.values() if not o.status.terminal]

    def account_equity(self) -> tuple[float, float]:
        return self.equity

    def equity_history(self) -> list[float]:
        return list(self.history)


class MemoryRecorder:
    def __init__(self) -> None:
        self.rows: dict[str, ExecutionRecord] = {}
        self.writes = 0

    def record_executions(self, records: Sequence[ExecutionRecord]) -> None:
        for r in records:
            self.writes += 1
            prior = self.rows.get(r.client_order_id)
            if prior is not None:  # reference is never overwritten (matches the store)
                r = r.model_copy(
                    update={
                        "reference_price": prior.reference_price,
                        "reference_source": prior.reference_source,
                    }
                )
            self.rows[r.client_order_id] = r

    def find_execution(self, client_order_id: str) -> ExecutionRecord | None:
        return self.rows.get(client_order_id)

    def load_open_executions(self, run_id: UUID) -> list[ExecutionRecord]:
        return [r for r in self.rows.values() if r.run_id == run_id and not r.status.terminal]
