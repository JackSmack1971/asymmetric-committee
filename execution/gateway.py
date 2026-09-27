"""Narrow broker interface used by execution (§9). Tests use fakes; production uses ``alpaca``."""

from __future__ import annotations

from typing import Protocol

from contracts.enums import OrderSide
from contracts.models import BrokerOrder


class BrokerError(RuntimeError):
    """The broker rejected or failed a request."""


class DuplicateClientOrderError(BrokerError):
    """``client_order_id`` already exists at the broker: the order was already submitted."""


class OrderNotCancelableError(BrokerError):
    """The broker refused a cancel, typically because the order is already terminal (a race)."""


class BrokerGateway(Protocol):
    def find_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        """Authoritative lookup by our stable id; None only if the broker has no such order."""
        ...

    def get_order(self, broker_order_id: str) -> BrokerOrder: ...

    def submit_limit(
        self, *, client_order_id: str, symbol: str, side: OrderSide, qty: float, limit_price: float
    ) -> BrokerOrder:
        """DAY limit. Raises ``DuplicateClientOrderError`` if the id was already used."""
        ...

    def submit_market(
        self, *, client_order_id: str, symbol: str, side: OrderSide, qty: float
    ) -> BrokerOrder:
        """DAY market. Raises ``DuplicateClientOrderError`` if the id was already used."""
        ...

    def cancel_order(self, broker_order_id: str) -> None:
        """Request cancellation. Success means *requested*; confirm with ``get_order``."""
        ...

    def open_orders(self) -> list[BrokerOrder]: ...

    def positions(self) -> dict[str, float]:
        """Symbol -> signed quantity."""
        ...

    def close_position(self, symbol: str) -> None:
        """Does not cancel open orders (Alpaca); callers must cancel and confirm first."""
        ...

    def account_equity(self) -> tuple[float, float]:
        """``(equity, prior_close_equity)``."""
        ...

    def equity_history(self) -> list[float]:
        """Daily account equity, oldest first (the broker's portfolio history). Empty if none."""
        ...
