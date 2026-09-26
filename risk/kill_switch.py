"""Kill-switch state and trigger evaluation (§9). Pure: no I/O, no broker, no clock.

Triggers: daily loss beyond the limit (vs prior close equity), any required feed stale beyond its
SLA (a feed that never succeeded is stale: fail closed), or a manual command. Once halted the
switch stays halted for the run; it blocks anything that increases risk (long-only book: BUY) and
still lets exposure-reducing orders through. Only MANUAL flattens. Peak drawdown is reported by the
caller alongside the event but is never a trigger. The benchmark T-bill adjustment is P6, not here.

State is durable: ``kill_switch_events`` is the source of truth and a ``KillSwitch`` is only a cache
of it, rebuilt with ``KillSwitch.from_events``. The first recorded trigger wins, so replayed or
duplicated events cannot change the result. There is no reset here: a halt lasts for its run, and
clearing one is a separate explicit operator action (the run-reset endpoint, P5 step 5).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta

from contracts.enums import FeedName, KillTrigger, OrderSide
from contracts.models import KillSwitchEvent


class KillSwitchHaltedError(RuntimeError):
    """A risk-increasing order was attempted while the kill switch is engaged."""


def daily_loss(equity: float, prior_close_equity: float) -> float:
    """Loss as a positive fraction of prior-close equity (negative = gain)."""
    if not (math.isfinite(equity) and math.isfinite(prior_close_equity)) or prior_close_equity <= 0:
        raise ValueError("equity figures must be finite and prior close equity positive")
    return (prior_close_equity - equity) / prior_close_equity


class EquityEvidenceError(ValueError):
    """Equity history is missing or unusable, so a drawdown cannot be computed: fail closed."""


def peak_drawdown(history: Sequence[float], current: float) -> tuple[float, float]:
    """``(peak, drawdown)`` of account equity, the peak taken over ``history`` and ``current``.

    Drawdown is ``(peak - current) / peak`` in [0, 1). Empty history, or any non-finite or
    non-positive value, raises ``EquityEvidenceError``: nothing is assumed about missing evidence.
    """
    if not history:
        raise EquityEvidenceError("no equity history: peak drawdown cannot be determined")
    values = [*history, current]
    if not all(math.isfinite(v) and v > 0 for v in values):
        raise EquityEvidenceError("equity history contains a non-finite or non-positive value")
    peak = max(values)
    return peak, (peak - current) / peak


def stale_feeds(
    last_success: Mapping[FeedName, datetime | None],
    sla_hours: Mapping[FeedName, float],
    now: datetime,
) -> list[FeedName]:
    """Required feeds (every key of ``sla_hours``) whose last success is missing or too old."""
    out: list[FeedName] = []
    for feed, hours in sla_hours.items():
        seen = last_success.get(feed)
        if seen is None or now - seen > timedelta(hours=hours):
            out.append(feed)
    return out


def evaluate(
    *,
    equity: float,
    prior_close_equity: float,
    limit: float,
    last_success: Mapping[FeedName, datetime | None],
    sla_hours: Mapping[FeedName, float],
    now: datetime,
) -> KillTrigger | None:
    """First automatic trigger that fires, or None. Loss must strictly exceed the limit."""
    if daily_loss(equity, prior_close_equity) > limit:
        return KillTrigger.DAILY_LOSS
    if stale_feeds(last_success, sla_hours, now):
        return KillTrigger.STALE_FEED
    return None


class KillSwitch:
    def __init__(self, halted_by: KillTrigger | None = None) -> None:
        self._trigger = halted_by

    @classmethod
    def from_events(cls, events: Iterable[KillSwitchEvent]) -> KillSwitch:
        """Rebuild the state from stored events (oldest first): the first trigger is kept."""
        ks = cls()
        for e in sorted(events, key=lambda e: e.triggered_at):
            ks.halt(e.trigger)
        return ks

    @property
    def halted(self) -> bool:
        return self._trigger is not None

    @property
    def trigger(self) -> KillTrigger | None:
        return self._trigger

    def halt(self, trigger: KillTrigger) -> bool:
        """Engage the switch. Returns False if it was already engaged (first trigger is kept)."""
        if self._trigger is not None:
            return False
        self._trigger = trigger
        return True

    def allows(self, side: OrderSide) -> bool:
        """Long-only book (§8): BUY adds risk, SELL reduces it."""
        return not self.halted or side is OrderSide.SELL

    def require(self, side: OrderSide) -> None:
        if not self.allows(side):
            raise KillSwitchHaltedError(f"kill switch engaged ({self._trigger}): {side} blocked")


def was_flattened(events: Iterable[KillSwitchEvent]) -> bool:
    """True if any stored halt completed a flatten (only a manual halt does)."""
    return any(e.flattened for e in events)
