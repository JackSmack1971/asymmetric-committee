"""Kill switch (§9): each trigger, blocking new exposure, cancel-before-flatten."""

from __future__ import annotations

from datetime import timedelta

import pytest

from contracts.enums import BrokerOrderStatus as S
from contracts.enums import FeedName, KillTrigger, OrderSide, ReferenceSource
from contracts.models import KillSwitchEvent
from execution.executor import CancelNotConfirmedError, ExecutionTarget, OrderExecutor
from execution.halt import HaltController
from risk.kill_switch import KillSwitch, KillSwitchHaltedError, daily_loss, evaluate, stale_feeds
from tests.execution.fakes import RUN, T0, FakeBroker, FakeClock, MemoryRecorder

SLA = {FeedName.PRICE_BARS: 72.0, FeedName.NEWS: 1.0}
FRESH = {FeedName.PRICE_BARS: T0 - timedelta(hours=2), FeedName.NEWS: T0 - timedelta(minutes=5)}


def rig() -> tuple[FakeClock, FakeBroker, KillSwitch, list[KillSwitchEvent], HaltController]:
    clock = FakeClock()
    broker = FakeBroker(clock)
    ks = KillSwitch()
    events: list[KillSwitchEvent] = []
    hc = HaltController(
        broker, ks, events.append, run_id=RUN, clock=clock, sleep=clock.sleep, poll_attempts=2
    )
    return clock, broker, ks, events, hc


def test_daily_loss_trigger_is_strictly_greater_than_limit() -> None:
    assert daily_loss(97_000.0, 100_000.0) == pytest.approx(0.03)
    kw = dict(limit=0.03, last_success=FRESH, sla_hours=SLA, now=T0)
    assert evaluate(equity=97_000.0, prior_close_equity=100_000.0, **kw) is None  # type: ignore[arg-type]
    hit = evaluate(equity=96_999.0, prior_close_equity=100_000.0, **kw)  # type: ignore[arg-type]
    assert hit is KillTrigger.DAILY_LOSS


def test_stale_feed_trigger_including_never_ingested() -> None:
    old = {**FRESH, FeedName.NEWS: T0 - timedelta(hours=2)}
    assert stale_feeds(old, SLA, T0) == [FeedName.NEWS]
    assert stale_feeds({FeedName.PRICE_BARS: T0}, SLA, T0) == [FeedName.NEWS]  # missing = stale
    assert stale_feeds({**FRESH, FeedName.NEWS: None}, SLA, T0) == [FeedName.NEWS]
    assert stale_feeds(FRESH, SLA, T0) == []


def test_auto_halt_on_daily_loss_cancels_orders_but_does_not_flatten() -> None:
    _, broker, ks, events, hc = rig()
    broker.equity = (96_000.0, 100_000.0)
    broker.pos = {"ZZZ": 10.0}
    broker.submit_limit(client_order_id="x", symbol="ZZZ", side=OrderSide.BUY, qty=5, limit_price=1)
    trig = hc.check(limit=0.03, last_success=FRESH, sla_hours=SLA)
    assert trig is KillTrigger.DAILY_LOSS and ks.halted
    assert broker.orders["B1"].status is S.CANCELED and broker.closed == []
    (ev,) = events
    assert ev.trigger is KillTrigger.DAILY_LOSS and ev.daily_loss == pytest.approx(0.04)
    assert ev.cancelled_order_ids == ("B1",) and ev.flattened is False


def test_auto_halt_on_stale_feed() -> None:
    _, _, ks, events, hc = rig()
    stale = {**FRESH, FeedName.NEWS: T0 - timedelta(hours=3)}
    assert hc.check(limit=0.03, last_success=stale, sla_hours=SLA) is KillTrigger.STALE_FEED
    assert ks.trigger is KillTrigger.STALE_FEED and events[0].trigger is KillTrigger.STALE_FEED


def test_no_halt_when_healthy_and_halt_is_not_refired() -> None:
    _, broker, ks, events, hc = rig()
    assert hc.check(limit=0.03, last_success=FRESH, sla_hours=SLA) is None and not ks.halted
    broker.equity = (90_000.0, 100_000.0)
    hc.check(limit=0.03, last_success=FRESH, sla_hours=SLA)
    assert hc.check(limit=0.03, last_success=FRESH, sla_hours=SLA) is None
    assert len(events) == 1


def test_manual_halt_cancels_and_confirms_before_closing_positions() -> None:
    _, broker, ks, events, hc = rig()
    broker.pos = {"AAA": 10.0, "BBB": 4.0}
    broker.submit_limit(client_order_id="x", symbol="AAA", side=OrderSide.BUY, qty=5, limit_price=1)
    hc.manual_halt()
    assert ks.halted and broker.closed == ["AAA", "BBB"]  # fake asserts no open orders at close
    first_close = broker.calls.index("close_position")
    assert "cancel" in broker.calls[:first_close]
    assert broker.calls[:first_close].count("open_orders") >= 2  # re-listed after cancelling
    (ev,) = events
    assert ev.trigger is KillTrigger.MANUAL and ev.flattened and ev.cancelled_order_ids == ("B1",)
    hc.manual_halt()  # repeating a completed flatten is a no-op
    assert len(events) == 1 and broker.closed == ["AAA", "BBB"]


def test_manual_flatten_after_auto_halt_still_flattens() -> None:
    _, broker, _, events, hc = rig()
    broker.equity = (90_000.0, 100_000.0)
    broker.pos = {"AAA": 1.0}
    hc.check(limit=0.03, last_success=FRESH, sla_hours=SLA)
    hc.manual_halt()
    assert broker.closed == ["AAA"] and [e.trigger for e in events] == [
        KillTrigger.DAILY_LOSS,
        KillTrigger.MANUAL,
    ]


def test_flatten_closes_nothing_if_cancel_cannot_be_confirmed() -> None:
    _, broker, ks, events, hc = rig()
    broker.cancel_mode = "stuck"
    broker.pos = {"AAA": 10.0}
    broker.submit_limit(client_order_id="x", symbol="AAA", side=OrderSide.BUY, qty=5, limit_price=1)
    with pytest.raises(CancelNotConfirmedError):
        hc.manual_halt()
    assert broker.closed == [] and ks.halted  # halt stays engaged
    assert len(events) == 1 and events[0].flattened is False  # and is still recorded


def test_kill_switch_blocks_new_exposure_but_allows_reduction() -> None:
    ks = KillSwitch()
    assert ks.allows(OrderSide.BUY)
    ks.halt(KillTrigger.STALE_FEED)
    assert not ks.allows(OrderSide.BUY) and ks.allows(OrderSide.SELL)
    with pytest.raises(KillSwitchHaltedError):
        ks.require(OrderSide.BUY)
    assert not ks.halt(KillTrigger.MANUAL) and ks.trigger is KillTrigger.STALE_FEED


def test_halted_executor_submits_no_new_buy() -> None:
    clock = FakeClock()
    broker = FakeBroker(clock)
    ks = KillSwitch(halted_by=KillTrigger.DAILY_LOSS)
    ex = OrderExecutor(broker, ks, MemoryRecorder(), clock=clock, sleep=clock.sleep)
    target = ExecutionTarget(RUN, 7, "ZZZ", OrderSide.BUY, 10.0, 50.0)
    out = ex.execute(target, lambda: (50.0, ReferenceSource.IEX_MID))
    assert out.blocked_by_kill_switch and broker.submits == []
    sell = ExecutionTarget(RUN, 8, "ZZZ", OrderSide.SELL, 10.0, 50.0)
    ex.execute(sell, lambda: (50.0, ReferenceSource.IEX_MID))
    assert len(broker.submits) >= 1  # reducing exposure is still allowed


def test_halt_mid_execution_blocks_the_market_residual() -> None:
    clock = FakeClock()
    broker = FakeBroker(clock)
    ks = KillSwitch()
    ex = OrderExecutor(broker, ks, MemoryRecorder(), clock=clock, sleep=clock.sleep)
    clock.on_sleep.append(lambda: (broker.fill("B1", 10.0, 50.0), ks.halt(KillTrigger.MANUAL)))
    target = ExecutionTarget(RUN, 7, "ZZZ", OrderSide.BUY, 100.0, 50.0)
    out = ex.execute(target, lambda: (50.0, ReferenceSource.IEX_MID))
    assert out.blocked_by_kill_switch and not out.complete and out.filled_qty == 10.0
    assert len(broker.submits) == 1  # only the limit order
