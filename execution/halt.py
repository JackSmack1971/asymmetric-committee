"""Kill-switch actions (§9): automatic halts cancel open orders; only a manual halt flattens.

``close_position`` does not cancel open orders at the broker, so a flatten first cancels every open
order, positively confirms each is terminal, re-lists open orders (a late order would otherwise
re-open exposure) and only then closes positions. If cancels cannot be confirmed nothing is closed.
The halt event is recorded even when the action fails part-way, so a halt is never lost.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import datetime
from uuid import UUID

from contracts.enums import FeedName, KillTrigger
from contracts.models import KillSwitchEvent
from execution.executor import CancelNotConfirmedError, Clock, Sleeper
from execution.gateway import BrokerGateway, OrderNotCancelableError
from risk.kill_switch import (
    EquityEvidenceError,
    KillSwitch,
    daily_loss,
    evaluate,
    peak_drawdown,
)

_ROUNDS = 3


class HaltController:
    def __init__(
        self,
        gateway: BrokerGateway,
        kill_switch: KillSwitch,
        record_event: Callable[[KillSwitchEvent], object],
        *,
        run_id: UUID,
        clock: Clock,
        sleep: Sleeper,
        poll_seconds: float = 1.0,
        poll_attempts: int = 30,
        flattened: bool = False,
    ) -> None:
        self._gw = gateway
        self._ks = kill_switch
        self._record = record_event
        self._run_id = run_id
        self._clock = clock
        self._sleep = sleep
        self._poll_seconds = poll_seconds
        self._poll_attempts = poll_attempts
        self._flattened = flattened  # restored from stored events after a restart

    def check(
        self,
        *,
        limit: float,
        last_success: Mapping[FeedName, datetime | None],
        sla_hours: Mapping[FeedName, float],
    ) -> KillTrigger | None:
        """Evaluate the automatic triggers against the broker's account. Halts on the first.

        Fails closed: if the equity evidence needed for the drawdown record is missing the check
        raises ``EquityEvidenceError`` and the caller must not trade. Peak drawdown is only ever
        logged, never a trigger, so a halt that fires anyway is recorded with it unknown.
        """
        if self._ks.halted:
            return None
        equity, prior = self._gw.account_equity()
        trigger = evaluate(
            equity=equity,
            prior_close_equity=prior,
            limit=limit,
            last_success=last_success,
            sla_hours=sla_hours,
            now=self._clock(),
        )
        if trigger is None:
            self._drawdown(equity, required=True)  # no halt: the evidence must still be there
            return None
        drawdown = self._drawdown(equity, required=False)
        self._halt(trigger, flatten=False, loss=daily_loss(equity, prior), drawdown=drawdown)
        return trigger

    def manual_halt(self, *, flatten: bool = True) -> None:
        """Manual command: engages the switch (even after an automatic halt) and flattens."""
        if flatten and self._flattened:
            return
        self._halt(KillTrigger.MANUAL, flatten=flatten, loss=None, drawdown=self._safe_drawdown())

    def settle_open_orders(self) -> list[str]:
        """After a restart into an already-halted state: cancel and confirm anything still working.

        Writes no event (the halt is already on record); raises if a cancel cannot be confirmed.
        """
        return self._cancel_all_confirmed() if self._ks.halted else []

    def _drawdown(self, current: float, *, required: bool) -> float | None:
        try:
            return peak_drawdown(self._gw.equity_history(), current)[1]
        except EquityEvidenceError:
            if required:
                raise
            return None

    def _safe_drawdown(self) -> float | None:
        """Best effort for a manual halt, which must never be blocked by missing evidence."""
        try:
            return self._drawdown(self._gw.account_equity()[0], required=False)
        except Exception:
            return None

    def _halt(
        self, trigger: KillTrigger, *, flatten: bool, loss: float | None, drawdown: float | None
    ) -> None:
        self._ks.halt(trigger)  # engage first: nothing new may be submitted while we work
        cancelled: list[str] = []
        flattened = False
        try:
            cancelled = self._cancel_all_confirmed()
            if flatten:
                for symbol, qty in self._gw.positions().items():
                    if qty != 0:
                        self._gw.close_position(symbol)
                flattened = True
                self._flattened = True
        finally:
            self._record(
                KillSwitchEvent(
                    run_id=self._run_id,
                    triggered_at=self._clock(),
                    trigger=trigger,
                    daily_loss=loss,
                    peak_drawdown=drawdown,
                    cancelled_order_ids=tuple(cancelled),
                    flattened=flattened,
                )
            )

    def _cancel_all_confirmed(self) -> list[str]:
        cancelled: list[str] = []
        for _ in range(_ROUNDS):
            open_orders = self._gw.open_orders()
            if not open_orders:
                return cancelled
            for o in open_orders:
                with suppress(OrderNotCancelableError):  # already terminal: confirmed below
                    self._gw.cancel_order(o.broker_order_id)
                cancelled.append(o.broker_order_id)
            for o in open_orders:
                self._confirm_terminal(o.broker_order_id)
        if self._gw.open_orders():
            raise CancelNotConfirmedError("open orders remain after repeated cancel rounds")
        return cancelled

    def _confirm_terminal(self, broker_order_id: str) -> None:
        for _ in range(self._poll_attempts):
            if self._gw.get_order(broker_order_id).status.terminal:
                return
            self._sleep(self._poll_seconds)
        raise CancelNotConfirmedError(f"order {broker_order_id} not terminal after cancel")
