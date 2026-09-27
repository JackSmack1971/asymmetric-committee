"""Reference-price resolution (§4.6, §9), separated from scheduling. Pure: no I/O, no clock.

* `resolve_live` is the §9 rule (`execution.reference.reference_price`) for any timestamp. Execution
  calls it at D0 open + 30 minutes and halt capture at its own time; it knows no sessions.
* `resolve_backtest` picks the latest eligible SIP trade at or before a reference time.
* `reconstruct_halt` picks the first eligible SIP trade at or after a halt time inside one session.

Eligibility is `execution.trade_conditions` (CTS/UTP). A candidate that cannot be decided makes the
result unresolved; it is never skipped, and no daily open or close is ever substituted.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from itertools import groupby

from contracts.enums import ConditionEligibility, ReferenceSource, RefReason, RefStatus
from contracts.market_data import ReferenceObservation, SipTrade, TradingSession
from execution.reference import NoReferencePriceError, QuoteSource, reference_price
from execution.trade_conditions import ProviderCodeMap, evaluate_conditions


def _unresolved(symbol: str, at: datetime, reason: RefReason) -> ReferenceObservation:
    return ReferenceObservation(
        symbol_ref=symbol, ref_time=at, status=RefStatus.UNRESOLVED, reason=reason
    )


def resolve_live(source: QuoteSource, symbol: str, at: datetime) -> ReferenceObservation:
    """The §9 live rule at ``at``; an absent price is durable evidence, not an exception."""
    try:
        price, ref_source = reference_price(source, symbol, at)
    except NoReferencePriceError:
        return _unresolved(symbol, at, RefReason.NO_REFERENCE_PRICE)
    return ReferenceObservation(
        symbol_ref=symbol,
        ref_time=at,
        status=RefStatus.RESOLVED,
        price=price,
        source=ref_source,
    )


class Kind(StrEnum):
    QUALIFIES = "qualifies"
    NOT = "not"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Verdict:
    trade: SipTrade
    kind: Kind
    reason: RefReason | None = None


def sequence_verdicts(trades: Sequence[SipTrade], pmap: ProviderCodeMap) -> list[Verdict]:
    """Whether each trade of one session's sequence updates the consolidated last price.

    ``trades`` must be the complete regular-session sequence from the open. A "first or only
    qualifying" condition depends on whether an earlier trade qualified, so it is decided against
    that history; if the history is itself undecided, or trades share an instant and cannot be
    ordered, the answer is unknown.
    """
    ordered = sorted(trades, key=lambda t: t.time)
    out: list[Verdict] = []
    seen_qualifying = False
    seen_unknown = False
    for _, group_iter in groupby(ordered, key=lambda t: t.time):
        group = list(group_iter)
        base = [evaluate_conditions(t.tape, t.conditions, pmap) for t in group]
        verdicts: list[Verdict] = []
        for i, (trade, b) in enumerate(zip(group, base, strict=True)):
            if b.status is ConditionEligibility.ELIGIBLE:
                verdicts.append(Verdict(trade, Kind.QUALIFIES))
            elif b.status is ConditionEligibility.INELIGIBLE:
                verdicts.append(Verdict(trade, Kind.NOT))
            elif b.status is ConditionEligibility.UNKNOWN:
                verdicts.append(Verdict(trade, Kind.UNKNOWN, b.reason))
            else:  # CONDITIONAL
                tied = any(
                    o.status is not ConditionEligibility.INELIGIBLE
                    for j, o in enumerate(base)
                    if j != i
                )
                if seen_qualifying:
                    if b.note3:  # needs the participant / listing market, which we do not hold
                        verdicts.append(Verdict(trade, Kind.UNKNOWN, RefReason.UNKNOWN_CONDITION))
                    else:
                        verdicts.append(Verdict(trade, Kind.NOT))
                elif tied:
                    verdicts.append(Verdict(trade, Kind.UNKNOWN, RefReason.AMBIGUOUS_ORDER))
                elif seen_unknown:
                    verdicts.append(Verdict(trade, Kind.UNKNOWN, RefReason.UNKNOWN_CONDITION))
                else:
                    verdicts.append(Verdict(trade, Kind.QUALIFIES))
        seen_qualifying = seen_qualifying or any(v.kind is Kind.QUALIFIES for v in verdicts)
        seen_unknown = seen_unknown or any(v.kind is Kind.UNKNOWN for v in verdicts)
        out.extend(verdicts)
    return out


def _sip_observation(symbol: str, at: datetime, trade: SipTrade) -> ReferenceObservation:
    return ReferenceObservation(
        symbol_ref=symbol,
        ref_time=at,
        status=RefStatus.RESOLVED,
        price=trade.price,
        source=ReferenceSource.SIP_LAST,
        trade_time=trade.time,
        trade_tape=trade.tape,
        trade_conditions=trade.conditions,
        trade_id=trade.trade_id,
    )


def _decide(symbol: str, at: datetime, groups: list[list[Verdict]]) -> ReferenceObservation:
    """First group (in the given order) that qualifies, or is undecided, decides the result."""
    for group in groups:
        unknown = [v for v in group if v.kind is Kind.UNKNOWN]
        if unknown:
            return _unresolved(symbol, at, unknown[0].reason or RefReason.UNKNOWN_CONDITION)
        qualifying = [v.trade for v in group if v.kind is Kind.QUALIFIES]
        if qualifying:
            if len({t.price for t in qualifying}) > 1:
                return _unresolved(symbol, at, RefReason.AMBIGUOUS_ORDER)
            return _sip_observation(symbol, at, qualifying[0])
    return _unresolved(symbol, at, RefReason.NO_ELIGIBLE_TRADE)


def _groups(verdicts: list[Verdict]) -> list[list[Verdict]]:
    return [list(g) for _, g in groupby(verdicts, key=lambda v: v.trade.time)]


def resolve_backtest(
    symbol: str,
    trades: Sequence[SipTrade],
    session: TradingSession,
    ref_time: datetime,
    pmap: ProviderCodeMap,
) -> ReferenceObservation:
    """Latest eligible regular-session SIP trade at or before ``ref_time`` (open + 30 minutes)."""
    if not session.open_at <= ref_time <= session.close_at:
        return _unresolved(symbol, ref_time, RefReason.NO_ELIGIBLE_TRADE)
    regular = [t for t in trades if session.open_at <= t.time <= ref_time]
    verdicts = sequence_verdicts(regular, pmap)
    return _decide(symbol, ref_time, list(reversed(_groups(verdicts))))


def reconstruct_halt(
    symbol: str,
    trades: Sequence[SipTrade],
    tau: datetime,
    sessions: Sequence[TradingSession],
    pmap: ProviderCodeMap,
) -> ReferenceObservation:
    """First eligible trade at or after ``tau`` in the first regular session that has not closed.

    "Within one session of tau" (§9): the session containing ``tau``, or the next one when ``tau``
    falls outside every session. Never the halt-day close.
    """
    later = sorted((s for s in sessions if s.close_at > tau), key=lambda s: s.open_at)
    if not later:
        return _unresolved(symbol, tau, RefReason.CALENDAR_UNCOVERED)
    session = later[0]
    start = max(tau, session.open_at)
    regular = [t for t in trades if session.open_at <= t.time <= session.close_at]
    verdicts = [v for v in sequence_verdicts(regular, pmap)]
    after = [g for g in _groups(verdicts) if g[0].trade.time >= start]
    return _decide(symbol, tau, after)
