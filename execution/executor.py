"""Limit -> cancel -> market-residual execution of a rebalance (§9).

Safety properties:

* Client order ids are stable per ``(run_id, security_id)``, so a retry or restart finds the
  existing order at the broker (authoritative) and never submits a second one.
* The broker is re-read before every decision; nothing is inferred from local state.
* The book runs in shared phases (``OrderExecutor.execute_book``): every limit order goes out
  back to back, one 15-minute wait covers them all, every still-working limit is cancelled and
  positively confirmed terminal, and only then are market residuals sent. A rebalance of N names
  therefore takes one window, not N, and all names share one reference-price timing.
* The market residual is computed only after the limit order is *confirmed terminal*, from the
  broker's final filled quantity, so a fill racing the cancel shrinks or removes the residual and
  the total can never exceed the target. A cancel that cannot be confirmed submits nothing for
  that name.
* A kill switch blocks new risk-increasing submissions but never blocks waiting on or cancelling
  orders that are already working.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from contracts.enums import BrokerOrderStatus, OrderSide, ReferenceSource
from contracts.models import BrokerOrder, ExecutionRecord
from execution.gateway import (
    BrokerError,
    BrokerGateway,
    DuplicateClientOrderError,
    OrderNotCancelableError,
)
from risk.kill_switch import KillSwitch

Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]
ReferenceFn = Callable[[], tuple[float, ReferenceSource]]

_EPS = 1e-6


class CancelNotConfirmedError(BrokerError):
    """The limit order is still not terminal after cancel: no replacement was submitted."""


class OverfillError(BrokerError):
    """The broker reports more filled than the target quantity. Halt and investigate."""


class ExecutionRecorder(Protocol):
    def record_executions(self, records: Sequence[ExecutionRecord]) -> None: ...

    def find_execution(self, client_order_id: str) -> ExecutionRecord | None: ...


@dataclass(frozen=True)
class ExecutionTarget:
    run_id: UUID
    security_id: int
    symbol: str
    side: OrderSide
    qty: float
    decision_price: float


@dataclass(frozen=True)
class ExecutionOutcome:
    records: tuple[ExecutionRecord, ...]
    filled_qty: float
    complete: bool  # True only when filled_qty == target qty
    blocked_by_kill_switch: bool = False


def client_order_id(run_id: UUID, security_id: int, leg: str) -> str:
    """Stable per run and security; ``leg`` is ``lim`` or ``mkt``. Within Alpaca's 128 chars."""
    return f"{run_id}-{security_id}-{leg}"


def slippage_bps(side: OrderSide, reference: float, fill: float) -> float:
    """Positive = worse than the reference (paid more on a buy, received less on a sell)."""
    sign = 1.0 if side is OrderSide.BUY else -1.0
    return sign * (fill - reference) / reference * 10_000


def collar_price(side: OrderSide, reference: float, collar_bps: float) -> float:
    """Limit at reference +/- collar, rounded to a cent inside it (floor buys, ceil sells)."""
    factor = 1 + collar_bps / 10_000 if side is OrderSide.BUY else 1 - collar_bps / 10_000
    cents = reference * factor * 100
    rounded = math.floor(cents + _EPS) if side is OrderSide.BUY else math.ceil(cents - _EPS)
    return rounded / 100


class OrderExecutor:
    def __init__(
        self,
        gateway: BrokerGateway,
        kill_switch: KillSwitch,
        recorder: ExecutionRecorder,
        *,
        clock: Clock,
        sleep: Sleeper,
        collar_bps: float = 25.0,
        limit_wait: timedelta = timedelta(minutes=15),
        poll_seconds: float = 1.0,
        poll_attempts: int = 30,
    ) -> None:
        self._gw = gateway
        self._ks = kill_switch
        self._rec = recorder
        self._clock = clock
        self._sleep = sleep
        self._collar_bps = collar_bps
        self._limit_wait = limit_wait
        self._poll_seconds = poll_seconds
        self._poll_attempts = poll_attempts

    # -- public ---------------------------------------------------------------------------------

    def execute(self, target: ExecutionTarget, reference: ReferenceFn) -> ExecutionOutcome:
        """Drive one name to completion. Safe to call again after any crash or retry."""
        return self.execute_book([target], lambda _t: reference)[0]

    def execute_book(
        self,
        targets: Sequence[ExecutionTarget],
        reference_for: Callable[[ExecutionTarget], ReferenceFn],
        *,
        gate: Callable[[], object] | None = None,
    ) -> list[ExecutionOutcome]:
        """Drive a whole rebalance through shared phases; outcomes follow the order of ``targets``.

        A. resolve every reference, then submit all limit orders back to back;
        B. wait once, until the latest limit's window (submitted_at + ``limit_wait``) has passed;
        C. re-read every still-working limit and cancel it;
        D. positively confirm each name's limit is terminal;
        E. after ``gate`` (the kill-switch re-check), submit market orders for the residuals.

        Per name this is the same limit -> cancel-confirm -> market-residual rule on the same
        stable client ids; only the waiting is shared. Nothing is remembered in memory: every
        phase starts from the broker (authoritative) and the recorder, so a crash or retry at any
        point resumes without a second order.

        A name that fails (broker error, unconfirmed cancel, unusable reference) does not stop the
        others; once every other name has been driven as far as it safely can and its evidence is
        stored, the first error is raised (the rest are attached as notes). An overfill also stops
        all new exposure. ``gate`` may engage the kill switch (cancelling and recording as it
        does); it runs before the first submission and again before the market phase, and new
        BUYs stop once the switch is engaged.
        """
        for t in targets:
            if t.qty <= 0 or not math.isfinite(t.qty):
                raise ValueError("target quantity must be positive and finite")
        legs = [_Leg(self, t, reference_for(t)) for t in targets]
        errors: list[Exception] = []
        stop_exposure = False

        def guarded(leg: _Leg, step: Callable[[], None]) -> None:
            nonlocal stop_exposure
            try:
                step()
            except OverfillError as exc:
                leg.error, stop_exposure = exc, True
                errors.append(exc)
            except Exception as exc:  # isolate the name; the first error is re-raised below
                leg.error = exc
                errors.append(exc)

        def live() -> list[_Leg]:
            return [leg for leg in legs if leg.error is None]

        if gate is not None:
            gate()
        # A. references first, then every limit submission
        fresh = [leg for leg in legs if leg.open_for_submit()]
        for leg in fresh:
            guarded(leg, leg.resolve_reference)
        for leg in fresh:
            if leg.error is None:
                guarded(leg, leg.submit_limit)
        # B. one shared wait
        self._wait_once([leg.lim for leg in live()])
        # C. re-read and cancel what is still working
        for leg in live():
            guarded(leg, leg.cancel_limit)
        # D. confirm every name's limit is terminal, together
        self._await_all(live())
        for leg in live():
            guarded(leg, leg.close_limit)
        # E. residual market orders, after the kill switch has been consulted again
        if gate is not None:
            gate()
        for leg in live():
            if leg.residual_pending and not stop_exposure:
                guarded(leg, leg.submit_market)
        self._await_all(live())
        for leg in live():
            if leg.mkt is not None:
                guarded(leg, leg.close_market)
        if errors:
            first = errors[0]
            for other in errors[1:]:
                first.add_note(f"also failed: {type(other).__name__}: {other}")
            raise first
        return [leg.outcome() for leg in legs]

    # -- internals ------------------------------------------------------------------------------

    def _submit_once(self, cid: str, submit: Callable[[], BrokerOrder]) -> BrokerOrder:
        try:
            return submit()
        except DuplicateClientOrderError:  # a lost response: the order exists, read it back
            found = self._gw.find_by_client_id(cid)
            if found is None:
                raise
            return found
        except BrokerError:
            # Outcome unknown (e.g. timeout after the broker accepted): reconcile, never resubmit.
            found = self._gw.find_by_client_id(cid)
            if found is None:
                raise
            return found

    def _wait_once(self, working: Sequence[BrokerOrder | None]) -> None:
        """One sleep covering every working limit's full window (its latest deadline).

        Skipped once the kill switch is engaged: the point then is to cancel and settle at once.
        """
        if self._ks.halted:
            return
        deadlines = [
            o.submitted_at + self._limit_wait
            for o in working
            if o is not None and not o.status.terminal
        ]
        if not deadlines:
            return
        remaining = (max(deadlines) - self._clock()).total_seconds()
        if remaining > 0:
            self._sleep(remaining)

    def _await_all(self, legs: Sequence[_Leg]) -> None:
        """Re-read each leg's current order until all are terminal or the attempts run out."""
        waiting = [
            leg for leg in legs if leg.current is not None and not leg.current.status.terminal
        ]
        for _ in range(self._poll_attempts):
            for leg in list(waiting):
                assert leg.current is not None
                leg.current = self._gw.get_order(leg.current.broker_order_id)
                if leg.current.status.terminal:
                    waiting.remove(leg)
            if not waiting:
                return
            self._sleep(self._poll_seconds)

    @staticmethod
    def _guard_overfill(target: ExecutionTarget, filled: float) -> None:
        if filled > target.qty + _EPS:
            raise OverfillError(
                f"{target.symbol}: filled {filled} exceeds target {target.qty} "
                f"(run {target.run_id})"
            )

    @staticmethod
    def _record(
        target: ExecutionTarget, order: BrokerOrder, price: float, source: ReferenceSource
    ) -> ExecutionRecord:
        fill = order.filled_avg_price if order.filled_qty > 0 else None
        return ExecutionRecord(
            run_id=target.run_id,
            security_id=target.security_id,
            client_order_id=order.client_order_id,
            broker_order_id=order.broker_order_id,
            kind=order.kind,
            side=order.side,
            qty=order.qty,
            filled_qty=order.filled_qty,
            limit_price=order.limit_price,
            decision_price=target.decision_price,
            reference_price=price,
            reference_source=source,
            fill_price=fill,
            slippage_bps=None if fill is None else slippage_bps(order.side, price, fill),
            status=order.status,
            submitted_at=order.submitted_at,
            filled_at=order.filled_at,
        )

    @staticmethod
    def _outcome(
        target: ExecutionTarget,
        records: list[ExecutionRecord],
        filled: float,
        *,
        blocked: bool = False,
    ) -> ExecutionOutcome:
        return ExecutionOutcome(
            tuple(records),
            filled,
            complete=abs(target.qty - filled) <= _EPS,
            blocked_by_kill_switch=blocked,
        )


class _Leg:
    """One name's progress through the phases. It holds only what the last broker read said."""

    def __init__(self, ex: OrderExecutor, target: ExecutionTarget, reference: ReferenceFn) -> None:
        self.ex = ex
        self.target = target
        self.lim_id = client_order_id(target.run_id, target.security_id, "lim")
        self.mkt_id = client_order_id(target.run_id, target.security_id, "mkt")
        self.ref = _Reference(ex._rec, reference)
        self.records: list[ExecutionRecord] = []
        self.lim: BrokerOrder | None = ex._gw.find_by_client_id(self.lim_id)
        self.mkt: BrokerOrder | None = None
        self.error: Exception | None = None
        self.blocked = False
        self.residual = 0.0
        self.residual_pending = False
        self._price: float | None = None

    @property
    def current(self) -> BrokerOrder | None:
        """The order being waited on: the market order once it exists, else the limit."""
        return self.mkt if self.mkt is not None else self.lim

    @current.setter
    def current(self, order: BrokerOrder) -> None:
        if self.mkt is not None:
            self.mkt = order
        else:
            self.lim = order

    def note(self, order: BrokerOrder) -> None:
        price, source = self.ref.get(self.lim_id)  # both legs share the name's reference price
        rec = self.ex._record(self.target, order, price, source)
        self.ex._rec.record_executions([rec])
        self.records.append(rec)

    # A: submit
    def open_for_submit(self) -> bool:
        if self.lim is not None:
            return False
        if not self.ex._ks.allows(self.target.side):
            self.blocked = True
            return False
        return True

    def resolve_reference(self) -> None:
        self._price, _ = self.ref.get(self.lim_id)

    def submit_limit(self) -> None:
        t, ex = self.target, self.ex
        if not ex._ks.allows(t.side):  # engaged since the references were resolved
            self.blocked = True
            return
        assert self._price is not None
        price = self._price
        self.lim = ex._submit_once(
            self.lim_id,
            lambda: ex._gw.submit_limit(
                client_order_id=self.lim_id,
                symbol=t.symbol,
                side=t.side,
                qty=t.qty,
                limit_price=collar_price(t.side, price, ex._collar_bps),
            ),
        )
        self.note(self.lim)

    # C: cancel what is still working
    def cancel_limit(self) -> None:
        if self.lim is None or self.lim.status.terminal:
            return
        gw = self.ex._gw
        self.lim = gw.get_order(self.lim.broker_order_id)
        if not self.lim.status.terminal:
            with suppress(OrderNotCancelableError):  # likely filled meanwhile; the re-read decides
                gw.cancel_order(self.lim.broker_order_id)

    # D: the limit's terminal state and the residual it leaves
    def close_limit(self) -> None:
        lim = self.lim
        if lim is None:
            return  # blocked by the kill switch: never submitted
        self.note(lim)
        if not lim.status.terminal:
            raise CancelNotConfirmedError(
                f"order {lim.broker_order_id} not terminal after cancel ({lim.status}); "
                "no replacement submitted"
            )
        OrderExecutor._guard_overfill(self.target, lim.filled_qty)
        if lim.status is BrokerOrderStatus.REJECTED:
            return
        self.residual = round(self.target.qty - lim.filled_qty, 6)
        self.residual_pending = self.residual > 0

    # E: market residual
    def submit_market(self) -> None:
        t, ex = self.target, self.ex
        found = ex._gw.find_by_client_id(self.mkt_id)
        if found is None:
            if not ex._ks.allows(t.side):
                self.blocked = True
                return
            found = ex._submit_once(
                self.mkt_id,
                lambda: ex._gw.submit_market(
                    client_order_id=self.mkt_id, symbol=t.symbol, side=t.side, qty=self.residual
                ),
            )
            self.mkt = found
            self.note(found)
        else:
            self.mkt = found

    def close_market(self) -> None:
        assert self.mkt is not None and self.lim is not None
        self.note(self.mkt)
        OrderExecutor._guard_overfill(self.target, self.lim.filled_qty + self.mkt.filled_qty)

    def outcome(self) -> ExecutionOutcome:
        filled = 0.0 if self.lim is None else self.lim.filled_qty
        if self.mkt is not None:
            filled += self.mkt.filled_qty
        return OrderExecutor._outcome(self.target, self.records, filled, blocked=self.blocked)


def refresh_record(stored: ExecutionRecord, order: BrokerOrder) -> ExecutionRecord:
    """The stored evidence for an order brought up to the broker's current state.

    Only broker-reported state moves (status, fills, fill price and its slippage against the
    *stored* reference, fill time); identity and the reference recorded at first sight never do.
    """
    if order.broker_order_id != stored.broker_order_id:
        raise ValueError("broker order does not match the stored execution record")
    fill = order.filled_avg_price if order.filled_qty > 0 else None
    return ExecutionRecord.model_validate(
        {
            **stored.model_dump(),
            "filled_qty": order.filled_qty,
            "status": order.status,
            "fill_price": fill,
            "slippage_bps": None
            if fill is None
            else slippage_bps(stored.side, stored.reference_price, fill),
            "filled_at": order.filled_at,
        }
    )


class _Reference:
    """Reference price for a name: the one already recorded for its limit order, else one lookup.

    If a crash lands between submitting the limit and recording it, the restart has no stored
    reference and takes a fresh lookup; that window is the only way evidence can differ from the
    price the limit was actually collared around.
    """

    def __init__(self, recorder: ExecutionRecorder, lookup: ReferenceFn) -> None:
        self._rec = recorder
        self._lookup = lookup
        self._fresh: tuple[float, ReferenceSource] | None = None

    def get(self, client_order_id: str) -> tuple[float, ReferenceSource]:
        stored = self._rec.find_execution(client_order_id)
        if stored is not None:
            return stored.reference_price, stored.reference_source
        if self._fresh is None:
            self._fresh = self._lookup()
        return self._fresh


__all__ = [
    "CancelNotConfirmedError",
    "ExecutionOutcome",
    "ExecutionRecorder",
    "ExecutionTarget",
    "OrderExecutor",
    "OverfillError",
    "client_order_id",
    "collar_price",
    "slippage_bps",
]
