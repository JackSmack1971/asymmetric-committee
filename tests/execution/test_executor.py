"""Limit -> cancel -> market residual (§9): fills, races, restarts, evidence."""

from __future__ import annotations

from datetime import timedelta

import pytest

from contracts.enums import BrokerOrderStatus as S
from contracts.enums import OrderKind, OrderSide, ReferenceSource
from execution.executor import (
    CancelNotConfirmedError,
    ExecutionTarget,
    OrderExecutor,
    OverfillError,
    client_order_id,
    collar_price,
    slippage_bps,
)
from risk.kill_switch import KillSwitch
from tests.execution.fakes import RUN, FakeBroker, FakeClock, MemoryRecorder

TARGET = ExecutionTarget(
    run_id=RUN, security_id=7, symbol="ZZZ", side=OrderSide.BUY, qty=100.0, decision_price=99.0
)
LIM = client_order_id(RUN, 7, "lim")
MKT = client_order_id(RUN, 7, "mkt")


def ref() -> tuple[float, ReferenceSource]:
    return 100.0, ReferenceSource.IEX_MID


class Rig:
    def __init__(self, ks: KillSwitch | None = None) -> None:
        self.clock = FakeClock()
        self.broker = FakeBroker(self.clock)
        self.rec = MemoryRecorder()
        self.ks = ks or KillSwitch()
        self.ex = OrderExecutor(
            self.broker, self.ks, self.rec, clock=self.clock, sleep=self.clock.sleep,
            poll_attempts=3,
        )  # fmt: skip

    def fill_limit_during_wait(self, qty: float, price: float = 99.9) -> None:
        self.clock.on_sleep.append(lambda: self.broker.fill("B1", qty, price))


def test_collar_and_slippage_math() -> None:
    assert collar_price(OrderSide.BUY, 100.0, 25) == 100.25
    assert collar_price(OrderSide.SELL, 100.0, 25) == 99.75
    assert collar_price(OrderSide.BUY, 33.333, 25) == 33.41  # floored inside the collar
    assert slippage_bps(OrderSide.BUY, 100.0, 100.1) == pytest.approx(10.0)
    assert slippage_bps(OrderSide.SELL, 100.0, 100.1) == pytest.approx(-10.0)


def test_full_limit_fill_needs_no_market_order() -> None:
    r = Rig()
    r.fill_limit_during_wait(100.0)
    out = r.ex.execute(TARGET, ref)
    assert out.complete and out.filled_qty == 100.0
    assert r.broker.submits == [LIM]
    assert (
        r.broker.orders["B1"].limit_price == 100.25
        and r.broker.orders["B1"].kind is OrderKind.LIMIT
    )
    assert r.clock.slept[0] == timedelta(minutes=15).total_seconds()
    assert "cancel" not in r.broker.calls


def test_partial_fill_then_exact_market_residual() -> None:
    r = Rig()
    r.fill_limit_during_wait(37.0)
    out = r.ex.execute(TARGET, ref)
    assert r.broker.orders["B1"].status is S.CANCELED
    assert r.broker.submits == [LIM, MKT]
    assert r.broker.orders["B2"].qty == 63.0 and r.broker.orders["B2"].kind is OrderKind.MARKET
    assert out.complete and out.filled_qty == 100.0


def test_fill_racing_with_cancel_removes_the_residual() -> None:
    r = Rig()
    r.broker.cancel_mode = "race_fill"
    out = r.ex.execute(TARGET, ref)
    assert r.broker.submits == [LIM]  # no market order: the cancel lost the race
    assert out.complete and out.filled_qty == 100.0


def test_partial_fill_racing_cancel_shrinks_the_residual() -> None:
    r = Rig()
    r.fill_limit_during_wait(20.0)
    real_cancel = r.broker.cancel_order

    def cancel_with_late_fill(bid: str) -> None:
        r.broker.fill(bid, 30.0, 99.8)  # 30 more fill before the cancel lands
        real_cancel(bid)

    r.broker.cancel_order = cancel_with_late_fill  # type: ignore[method-assign,assignment]
    out = r.ex.execute(TARGET, ref)
    assert r.broker.orders["B2"].qty == 50.0  # 100 - (20 + 30), from the post-cancel state
    assert out.filled_qty == 100.0


def test_cancel_not_confirmed_means_no_replacement() -> None:
    r = Rig()
    r.broker.cancel_mode = "stuck"
    with pytest.raises(CancelNotConfirmedError):
        r.ex.execute(TARGET, ref)
    assert r.broker.submits == [LIM]
    assert r.rec.rows[LIM].status is S.PENDING_CANCEL  # the honest state was persisted


def test_rejected_limit_is_not_replaced_by_a_market_order() -> None:
    r = Rig()
    r.clock.on_sleep.append(lambda: r.broker.set_status("B1", S.REJECTED))
    out = r.ex.execute(TARGET, ref)
    assert r.broker.submits == [LIM] and not out.complete


def test_restart_after_crash_finds_existing_order_and_does_not_duplicate() -> None:
    r = Rig()
    r.broker.lose_submit_response = True  # accepted at the broker, response lost
    r.fill_limit_during_wait(40.0)
    out = r.ex.execute(TARGET, ref)
    assert r.broker.submits == [LIM, MKT] and out.complete

    # A brand-new executor (process restart) over the same broker and store submits nothing.
    again = OrderExecutor(
        r.broker, KillSwitch(), r.rec, clock=r.clock, sleep=r.clock.sleep, poll_attempts=3
    ).execute(TARGET, lambda: (555.0, ReferenceSource.SIP_LAST))
    assert r.broker.submits == [LIM, MKT]
    assert again.complete and again.filled_qty == 100.0


def test_restart_mid_wait_resumes_the_same_limit_order() -> None:
    r = Rig()
    r.broker.submit_limit(
        client_order_id=LIM, symbol="ZZZ", side=OrderSide.BUY, qty=100.0, limit_price=100.25
    )
    r.clock.now += timedelta(minutes=10)  # 10 of the 15 minutes already elapsed before the crash
    r.ex.execute(TARGET, ref)
    assert r.broker.submits[0] == LIM and r.broker.submits.count(LIM) == 1
    assert r.clock.slept[0] == timedelta(minutes=5).total_seconds()


def test_completed_execution_is_idempotent() -> None:
    r = Rig()
    r.fill_limit_during_wait(60.0)
    r.ex.execute(TARGET, ref)
    orders_before = dict(r.broker.orders)
    calls_before = list(r.broker.calls)
    out = r.ex.execute(TARGET, ref)
    assert out.complete and r.broker.orders == orders_before
    new = r.broker.calls[len(calls_before) :]
    assert not {"submit_limit", "submit_market", "cancel"} & set(new)


def test_overfill_is_detected_never_papered_over() -> None:
    r = Rig()
    r.clock.on_sleep.append(lambda: r.broker.fill("B1", 100.0, 99.0))
    real = r.broker.get_order

    def overfilled(bid: str):  # type: ignore[no-untyped-def]
        return real(bid).model_copy(update={"filled_qty": 120.0})

    r.broker.get_order = overfilled  # type: ignore[method-assign,assignment]
    with pytest.raises(OverfillError):
        r.ex.execute(TARGET, ref)


def test_evidence_comes_from_the_broker_not_defaults() -> None:
    r = Rig()
    r.fill_limit_during_wait(37.0, price=99.5)
    r.ex.execute(TARGET, ref)
    lim, mkt = r.rec.rows[LIM], r.rec.rows[MKT]
    assert (lim.broker_order_id, lim.qty, lim.filled_qty, lim.limit_price) == (
        "B1",
        100.0,
        37.0,
        100.25,
    )
    assert lim.fill_price == pytest.approx(99.5)
    assert lim.slippage_bps == pytest.approx(slippage_bps(OrderSide.BUY, 100.0, 99.5))
    assert (lim.reference_price, lim.reference_source) == (100.0, ReferenceSource.IEX_MID)
    assert lim.decision_price == 99.0 and lim.submitted_at == r.clock.now - timedelta(minutes=15)
    assert (mkt.qty, mkt.filled_qty, mkt.fill_price) == (63.0, 63.0, 101.0)  # fake market fill
    assert mkt.slippage_bps == pytest.approx(100.0)
    assert mkt.reference_price == 100.0  # both legs share the name's reference


def test_unfilled_order_records_no_synthetic_fill() -> None:
    r = Rig()  # nothing fills the limit before the cancel: whole target goes to market
    out = r.ex.execute(TARGET, ref)
    lim = r.rec.rows[LIM]
    assert lim.status is S.CANCELED and lim.filled_qty == 0.0
    assert lim.fill_price is None and lim.slippage_bps is None and lim.filled_at is None
    assert r.broker.orders["B2"].qty == 100.0 and out.complete
