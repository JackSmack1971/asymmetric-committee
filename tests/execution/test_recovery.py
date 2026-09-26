"""Durable kill-switch state and peak drawdown (§9): rebuilt from events, not from memory."""

from __future__ import annotations

from datetime import timedelta

import pytest

from contracts.enums import FeedName, KillTrigger, OrderSide
from contracts.models import KillSwitchEvent
from execution.halt import HaltController
from risk.kill_switch import (
    EquityEvidenceError,
    KillSwitch,
    KillSwitchHaltedError,
    peak_drawdown,
    was_flattened,
)
from tests.execution.fakes import RUN, T0, FakeBroker, FakeClock

SLA = {FeedName.PRICE_BARS: 72.0}
FRESH = {FeedName.PRICE_BARS: T0 - timedelta(hours=1)}


def event(trigger: KillTrigger, minutes: int = 0, **kw: object) -> KillSwitchEvent:
    return KillSwitchEvent(
        run_id=RUN,
        triggered_at=T0 + timedelta(minutes=minutes),
        trigger=trigger,
        **kw,  # type: ignore[arg-type]
    )


def rig(
    history: list[float], equity: tuple[float, float] = (100_000.0, 100_000.0)
) -> tuple[FakeBroker, KillSwitch, list[KillSwitchEvent], HaltController]:
    clock = FakeClock()
    broker = FakeBroker(clock, history=history, equity=equity)
    ks = KillSwitch()
    events: list[KillSwitchEvent] = []
    hc = HaltController(
        broker, ks, events.append, run_id=RUN, clock=clock, sleep=clock.sleep, poll_attempts=2
    )
    return broker, ks, events, hc


# --- durable recovery ---------------------------------------------------------------------------


def test_a_stored_trigger_survives_reconstruction() -> None:
    stored = [event(KillTrigger.DAILY_LOSS, daily_loss=0.04)]
    fresh = KillSwitch.from_events(stored)  # a new process: nothing in memory
    assert fresh.halted and fresh.trigger is KillTrigger.DAILY_LOSS
    with pytest.raises(KillSwitchHaltedError):
        fresh.require(OrderSide.BUY)
    fresh.require(OrderSide.SELL)  # exposure-reducing orders still pass
    assert not KillSwitch.from_events([]).halted


def test_replayed_and_reordered_events_do_not_change_the_state() -> None:
    first = event(KillTrigger.STALE_FEED, 0)
    later = event(KillTrigger.MANUAL, 5, flattened=True)
    base = KillSwitch.from_events([first, later])
    assert base.trigger is KillTrigger.STALE_FEED  # the first trigger is kept
    for replay in ([first, first, later, later, first], [later, first], [first, later, later]):
        again = KillSwitch.from_events(replay)
        assert again.trigger is base.trigger and again.halted
    assert was_flattened([first, later, later]) and not was_flattened([first, first])


def test_there_is_no_implicit_reset() -> None:
    ks = KillSwitch.from_events([event(KillTrigger.MANUAL)])
    assert ks.halt(KillTrigger.DAILY_LOSS) is False  # a later trigger never replaces the first
    assert ks.trigger is KillTrigger.MANUAL and ks.halted
    assert not hasattr(ks, "reset") and not hasattr(ks, "clear")


def test_restored_halt_does_not_recheck_and_cancels_what_is_still_working() -> None:
    broker, _, events, hc = rig([100_000.0])
    ks = KillSwitch.from_events([event(KillTrigger.DAILY_LOSS)])
    clock = FakeClock()
    hc = HaltController(
        broker, ks, events.append, run_id=RUN, clock=clock, sleep=clock.sleep, poll_attempts=2
    )
    broker.submit_limit(client_order_id="a", symbol="ZZZ", side=OrderSide.BUY, qty=5, limit_price=1)
    assert hc.check(limit=0.03, last_success=FRESH, sla_hours=SLA) is None  # no second event
    assert hc.settle_open_orders() == ["B1"] and broker.open_orders_now() == []
    assert events == []  # the halt is already on record


def test_restored_flatten_flag_prevents_a_second_flatten() -> None:
    broker, _, events, _ = rig([100_000.0])
    clock = FakeClock()
    ks = KillSwitch.from_events([event(KillTrigger.MANUAL, flattened=True)])
    hc = HaltController(
        broker, ks, events.append, run_id=RUN, clock=clock, sleep=clock.sleep, flattened=True
    )
    broker.pos = {"ZZZ": 3.0}
    hc.manual_halt()
    assert broker.closed == [] and events == []


# --- peak drawdown ------------------------------------------------------------------------------


def test_peak_and_drawdown_over_rising_then_falling_equity() -> None:
    assert peak_drawdown([100.0], 100.0) == (100.0, 0.0)
    assert peak_drawdown([100.0, 110.0, 120.0], 120.0) == (120.0, 0.0)  # rising: at the peak
    peak, dd = peak_drawdown([100.0, 120.0, 90.0], 96.0)  # fell from the 120 peak
    assert peak == 120.0 and dd == pytest.approx(0.2)
    assert peak_drawdown([100.0, 120.0, 90.0], 130.0) == (130.0, 0.0)  # new high resets it
    peak, dd = peak_drawdown([100.0, 120.0, 90.0, 150.0], 135.0)
    assert peak == 150.0 and dd == pytest.approx(0.1)


@pytest.mark.parametrize(
    "history", [[], [100.0, float("nan")], [100.0, float("inf")], [100.0, 0.0], [100.0, -5.0]]
)
def test_missing_or_unusable_equity_history_fails_closed(history: list[float]) -> None:
    with pytest.raises(EquityEvidenceError):
        peak_drawdown(history, 100.0)
    with pytest.raises(EquityEvidenceError):
        peak_drawdown([100.0], float("nan"))


def test_check_without_equity_history_raises_instead_of_reporting_safe() -> None:
    _, ks, events, hc = rig([])
    with pytest.raises(EquityEvidenceError):
        hc.check(limit=0.03, last_success=FRESH, sla_hours=SLA)
    assert not ks.halted and events == []  # not halted, and not silently "ok" either


def test_peak_drawdown_is_recorded_with_a_halt_but_is_never_a_trigger() -> None:
    # 50% below the peak, but only a 1% day: drawdown alone must not halt
    broker, ks, events, hc = rig([200_000.0, 100_000.0], equity=(99_000.0, 100_000.0))
    assert hc.check(limit=0.03, last_success=FRESH, sla_hours=SLA) is None and not ks.halted
    # the existing daily-loss trigger still fires and now carries the drawdown
    broker.equity = (96_000.0, 100_000.0)
    assert hc.check(limit=0.03, last_success=FRESH, sla_hours=SLA) is KillTrigger.DAILY_LOSS
    (ev,) = events
    assert ev.daily_loss == pytest.approx(0.04)
    assert ev.peak_drawdown == pytest.approx(1 - 96_000 / 200_000)


def test_a_halt_that_fires_is_never_blocked_by_missing_history() -> None:
    _, ks, events, hc = rig([], equity=(90_000.0, 100_000.0))
    assert hc.check(limit=0.03, last_success=FRESH, sla_hours=SLA) is KillTrigger.DAILY_LOSS
    assert ks.halted and events[0].peak_drawdown is None
    _, ks2, events2, hc2 = rig([])
    hc2.manual_halt(flatten=False)
    assert ks2.trigger is KillTrigger.MANUAL and events2[0].peak_drawdown is None
