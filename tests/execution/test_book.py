"""Whole-book execution (§9): one shared 15-minute window, phase order, exact residuals, restarts.

``OrderExecutor.execute_book`` runs A) every limit submit, B) one wait, C) cancel, D) confirm
terminal, E) market residuals. These tests drive it over the in-memory broker.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import timedelta

import pytest

from contracts.enums import BrokerOrderStatus as S
from contracts.enums import KillTrigger, OrderKind, OrderSide, ReferenceSource
from execution.executor import (
    CancelNotConfirmedError,
    ExecutionOutcome,
    ExecutionTarget,
    OrderExecutor,
    OverfillError,
    client_order_id,
)
from execution.gateway import BrokerError, OrderNotCancelableError
from risk.kill_switch import KillSwitch
from tests.execution.fakes import RUN, FakeBroker, FakeClock, MemoryRecorder

WINDOW = timedelta(minutes=15).total_seconds()


class Crash(BaseException):
    """A process dying: unlike an Exception, nothing in the executor may swallow it."""


def targets(n: int, qty: float = 100.0, side: OrderSide = OrderSide.BUY) -> list[ExecutionTarget]:
    return [ExecutionTarget(RUN, i, f"S{i}", side, qty, 99.0) for i in range(1, n + 1)]


def ref_for(_t: ExecutionTarget) -> Callable[[], tuple[float, ReferenceSource]]:
    return lambda: (100.0, ReferenceSource.IEX_MID)


def lim(i: int) -> str:
    return client_order_id(RUN, i, "lim")


def mkt(i: int) -> str:
    return client_order_id(RUN, i, "mkt")


class Rig:
    def __init__(self, ks: KillSwitch | None = None) -> None:
        self.clock = FakeClock()
        self.broker = FakeBroker(self.clock)
        self.rec = MemoryRecorder()
        self.ks = ks or KillSwitch()
        self.ex = self.new_executor(self.ks)

    def new_executor(self, ks: KillSwitch | None = None) -> OrderExecutor:
        """A fresh executor over the same broker and store: what a restarted process has."""
        return OrderExecutor(
            self.broker, ks or KillSwitch(), self.rec, clock=self.clock, sleep=self.clock.sleep,
            poll_attempts=3,
        )  # fmt: skip

    def order(self, cid: str) -> S:
        o = self.broker._by_client(cid)
        assert o is not None
        return o.status

    def long_sleeps(self) -> list[float]:
        return [s for s in self.clock.slept if s >= 60]


def test_a_book_of_n_names_waits_one_window_not_n() -> None:
    r = Rig()
    out = r.ex.execute_book(targets(6), ref_for)
    assert r.long_sleeps() == [WINDOW]
    assert all(o.complete for o in out)
    assert r.clock.now < r.broker.orders["B1"].submitted_at + timedelta(minutes=16)  # not 6 x 15


def test_every_limit_is_submitted_before_any_market_order() -> None:
    r = Rig()
    r.ex.execute_book(targets(4), ref_for)
    assert r.broker.submits == [lim(i) for i in range(1, 5)] + [mkt(i) for i in range(1, 5)]
    assert Counter(r.broker.submits).most_common(1)[0][1] == 1  # nothing sent twice
    submitted = {r.broker.orders[f"B{i}"].submitted_at for i in range(1, 5)}
    assert len(submitted) == 1  # one moment: no reference-timing drift between names


def test_mixed_full_partial_and_unfilled_names_get_exact_residuals() -> None:
    r = Rig()

    def fills() -> None:
        r.broker.fill("B1", 100.0, 99.9)  # full
        r.broker.fill("B2", 37.0, 99.9)  # partial
        # B3 unfilled; B4 filled 99.5 of 100 (a sub-share sliver still counts)
        r.broker.fill("B4", 99.5, 99.9)

    r.clock.on_sleep.append(fills)
    out = r.ex.execute_book(targets(4), ref_for)
    assert mkt(1) not in r.broker.submits
    residuals = [o for i in (2, 3, 4) if (o := r.broker._by_client(mkt(i))) is not None]
    assert [o.qty for o in residuals] == [63.0, 100.0, 0.5]
    assert all(o.kind is OrderKind.MARKET for o in residuals)
    assert [o.filled_qty for o in out] == [100.0, 100.0, 100.0, 100.0]
    assert all(o.complete for o in out)


def test_a_fill_racing_the_cancel_can_never_overfill() -> None:
    r = Rig()
    r.clock.on_sleep.append(lambda: r.broker.fill("B1", 20.0, 99.9))
    real_cancel = r.broker.cancel_order

    def racing_cancel(bid: str) -> None:
        o = r.broker.orders[bid]
        if bid == "B1":
            r.broker.fill(bid, 30.0, 99.8)  # 30 more fill as the cancel lands
        elif bid == "B2":
            r.broker.fill(bid, o.qty - o.filled_qty, 99.8)  # B2 fills completely in the race
            raise OrderNotCancelableError("filled")  # a refused cancel: the re-read decides
        real_cancel(bid)

    r.broker.cancel_order = racing_cancel  # type: ignore[method-assign,assignment]
    out = r.ex.execute_book(targets(3), ref_for)
    assert r.broker._by_client(mkt(1)).qty == 50.0  # type: ignore[union-attr]  # 100-(20+30)
    assert r.broker._by_client(mkt(2)) is None  # filled during the cancel: no residual
    for i, o in enumerate(out, start=1):
        total = r.broker._by_client(lim(i)).filled_qty  # type: ignore[union-attr]
        m = r.broker._by_client(mkt(i))
        total += m.filled_qty if m else 0.0
        assert total == pytest.approx(100.0) and o.filled_qty <= 100.0 + 1e-9


def test_restart_after_the_initial_submits_creates_no_duplicate_orders() -> None:
    r = Rig()

    def crash(_seconds: float) -> None:
        raise Crash

    r.ex._sleep = crash
    with pytest.raises(Crash):
        r.ex.execute_book(targets(5), ref_for)  # died in the shared wait, all limits out
    assert r.broker.submits == [lim(i) for i in range(1, 6)]

    r.clock.now += timedelta(minutes=4)  # the restart is 4 minutes into the window
    out = r.new_executor().execute_book(targets(5), ref_for)
    assert Counter(r.broker.submits).most_common(1)[0][1] == 1
    assert sorted(r.broker.submits) == sorted(
        [lim(i) for i in range(1, 6)] + [mkt(i) for i in range(1, 6)]
    )
    assert r.long_sleeps() == [WINDOW - 4 * 60]  # resumes the remaining window, no new one
    assert all(o.complete for o in out)


def test_restart_after_some_cancellations_creates_no_duplicate_orders() -> None:
    r = Rig()
    real_cancel = r.broker.cancel_order
    cancels: list[str] = []

    def dying_cancel(bid: str) -> None:
        if len(cancels) == 2:
            raise Crash  # the process dies while cancelling the third limit
        cancels.append(bid)
        real_cancel(bid)

    r.broker.cancel_order = dying_cancel  # type: ignore[method-assign,assignment]
    r.clock.on_sleep.append(lambda: r.broker.fill("B1", 60.0, 99.9))
    with pytest.raises(Crash):
        r.ex.execute_book(targets(4), ref_for)
    assert [r.order(lim(i)) for i in (1, 2, 3, 4)] == [S.CANCELED, S.CANCELED, S.OPEN, S.OPEN]
    assert r.broker.submits == [lim(i) for i in range(1, 5)]  # no market order yet

    r.broker.cancel_order = real_cancel  # type: ignore[method-assign]
    out = r.new_executor().execute_book(targets(4), ref_for)
    assert Counter(r.broker.submits).most_common(1)[0][1] == 1
    assert [r.broker._by_client(mkt(i)).qty for i in (1, 2, 3, 4)] == [40.0, 100.0, 100.0, 100.0]  # type: ignore[union-attr]
    assert all(o.complete for o in out)

    # a further restart after everything finished sends nothing at all
    before = list(r.broker.submits)
    r.new_executor().execute_book(targets(4), ref_for)
    assert r.broker.submits == before


def test_one_names_stuck_cancel_blocks_only_that_name_and_nothing_is_duplicated() -> None:
    r = Rig()
    real_cancel = r.broker.cancel_order

    def cancel(bid: str) -> None:
        if bid == "B2":
            r.broker.set_status(bid, S.PENDING_CANCEL)  # never confirms
        else:
            real_cancel(bid)

    r.broker.cancel_order = cancel  # type: ignore[method-assign,assignment]
    with pytest.raises(CancelNotConfirmedError):
        r.ex.execute_book(targets(3), ref_for)
    assert mkt(2) not in r.broker.submits  # no replacement while its limit may still fill
    assert mkt(1) in r.broker.submits and mkt(3) in r.broker.submits  # the others went ahead
    assert r.rec.rows[lim(2)].status is S.PENDING_CANCEL  # honest state stored

    r.broker.cancel_order = real_cancel  # type: ignore[method-assign]
    r.broker.set_status("B2", S.CANCELED)
    out = r.new_executor().execute_book(targets(3), ref_for)
    assert Counter(r.broker.submits).most_common(1)[0][1] == 1
    assert all(o.complete for o in out)


def test_an_unusable_reference_for_one_name_does_not_stop_the_others() -> None:
    r = Rig()

    def refs(t: ExecutionTarget) -> Callable[[], tuple[float, ReferenceSource]]:
        if t.security_id != 2:
            return ref_for(t)

        def bad() -> tuple[float, ReferenceSource]:
            raise BrokerError("no usable quote")

        return bad

    with pytest.raises(BrokerError, match="no usable quote"):
        r.ex.execute_book(targets(3), refs)
    assert lim(2) not in r.broker.submits and {lim(1), lim(3)} <= set(r.broker.submits)
    out = r.new_executor().execute_book(targets(3), ref_for)  # next attempt: only name 2 is new
    assert Counter(r.broker.submits).most_common(1)[0][1] == 1 and all(o.complete for o in out)


def test_an_overfill_stops_all_new_exposure() -> None:
    r = Rig()
    real_get = r.broker.get_order

    def get(bid: str):  # type: ignore[no-untyped-def]
        o = real_get(bid)
        return o.model_copy(update={"filled_qty": 120.0}) if bid == "B1" else o

    r.broker.get_order = get  # type: ignore[method-assign,assignment]
    with pytest.raises(OverfillError):
        r.ex.execute_book(targets(3), ref_for)
    assert not [c for c in r.broker.submits if c.endswith("-mkt")]  # nothing new after the alarm


def test_the_gate_runs_before_first_submission_and_before_the_market_phase() -> None:
    r = Rig()
    seen: list[list[str]] = []
    r.ex.execute_book(targets(3), ref_for, gate=lambda: seen.append(list(r.broker.submits)))
    assert seen == [
        [],
        [lim(1), lim(2), lim(3)],
    ]  # nothing sent at the first call, no markets at the second


def test_a_halt_before_the_market_phase_stops_new_exposure_and_keeps_evidence() -> None:
    r = Rig()
    calls = 0

    def gate() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            r.ks.halt(KillTrigger.STALE_FEED)

    r.clock.on_sleep.append(lambda: r.broker.fill("B1", 100.0, 99.9))
    out = r.ex.execute_book(targets(3), ref_for, gate=gate)
    assert r.broker.submits == [lim(1), lim(2), lim(3)]  # no BUY market order after the halt
    assert not r.broker.open_orders_now()  # every limit was cancelled and confirmed
    assert [r.rec.rows[lim(i)].status for i in (1, 2, 3)] == [S.FILLED, S.CANCELED, S.CANCELED]
    assert [o.blocked_by_kill_switch for o in out] == [False, True, True]  # 1 was already whole
    assert [o.complete for o in out] == [True, False, False]


def test_a_halt_before_the_first_submission_sends_no_buys_but_still_lets_sells_out() -> None:
    r = Rig()
    ts = [*targets(2), ExecutionTarget(RUN, 9, "S9", OrderSide.SELL, 50.0, 99.0)]
    r.ex.execute_book(ts, ref_for, gate=lambda: r.ks.halt(KillTrigger.DAILY_LOSS))
    assert lim(1) not in r.broker.submits and lim(2) not in r.broker.submits
    assert lim(9) in r.broker.submits  # reducing risk is allowed while halted
    # halted: no waiting out a window; the SELL is settled at once (cancelled, residual to market)
    assert r.long_sleeps() == [] and r.broker._by_client(mkt(9)).qty == 50.0  # type: ignore[union-attr]


def test_a_halt_mid_window_skips_the_rest_of_the_wait() -> None:
    r2 = Rig(KillSwitch(KillTrigger.MANUAL))
    r2.broker.submit_limit(
        client_order_id=lim(1), symbol="S1", side=OrderSide.BUY, qty=100.0, limit_price=100.25
    )
    r2.ex.execute_book(targets(1), ref_for)  # halted restart: cancel at once, no new window
    assert r2.long_sleeps() == [] and r2.order(lim(1)) is S.CANCELED
    assert mkt(1) not in r2.broker.submits


def test_outcomes_follow_target_order_and_evidence_is_per_name() -> None:
    r = Rig()
    r.clock.on_sleep.append(lambda: r.broker.fill("B2", 10.0, 99.9))
    out: list[ExecutionOutcome] = r.ex.execute_book(targets(3), ref_for)
    assert all(o.records for o in out)
    assert {rec.security_id for o in out for rec in o.records} == {1, 2, 3}
    assert all(rec.reference_price == 100.0 for o in out for rec in o.records)


def test_a_non_positive_target_is_refused_before_anything_is_sent() -> None:
    r = Rig()
    bad = [*targets(1), ExecutionTarget(RUN, 2, "S2", OrderSide.BUY, 0.0, 99.0)]
    with pytest.raises(ValueError, match="positive"):
        r.ex.execute_book(bad, ref_for)
    assert r.broker.submits == []
