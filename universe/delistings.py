"""Delisting ledger: the §4.6 evidence hierarchy as a pure function of stored evidence (P6.4).

First matching rule wins:

1. a terminal action (cash/stock/mixed merger, redemption) with known consideration
   -> DELISTED, ``consideration`` (inputs stored; the value is computed at scoring);
2. a worthless removal -> DELISTED, ``worthless`` (-100%);
3. a terminal action whose consideration is unavailable -> DELISTED, ``default`` (-30%);
4. a name change -> IDENTITY_CHANGED (same ``security_id``; never a default);
5. status-derived: Alpaca ``inactive`` on >= 2 persisted polls at least one confirmed session
   apart, an effective Form 25, complete action coverage over [last trade, Form 25 effective]
   showing no terminal/identity action, and confirmed calendar coverage -> DELISTED, ``default``;
6. >= N confirmed sessions without a bar while the asset is still ``active`` -> SUSPECTED_GAP.

Anything else derives nothing (the security stays unresolved). Missing bars alone never authorize
the -30% default; Form 15 is not required.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from contracts.corporate_actions import (
    DEFAULT_TERMINAL_RETURN,
    WORTHLESS_TERMINAL_RETURN,
    AssetStatusObservation,
    CorporateAction,
    CorporateActionCoverage,
    Delisting,
    DelistingFiling,
    SymbolResolver,
    delisting_version,
)
from contracts.enums import (
    ActionInterpretation,
    AssetStatus,
    CorporateActionType,
    DelistingReason,
    DelistingStatus,
    Form25Provision,
    KnowledgeBasis,
    TerminalReturnSource,
)
from evaluation.calendar_rules import SessionStatus, TradingCalendar

DERIVATION_VERSION = "delistings_v1"
DEFAULT_GAP_SESSIONS = 10
_ET = ZoneInfo("America/New_York")
_TERMINAL_REASON = {
    CorporateActionType.CASH_MERGER: DelistingReason.CASH_MERGER,
    CorporateActionType.STOCK_MERGER: DelistingReason.STOCK_MERGER,
    CorporateActionType.STOCK_AND_CASH_MERGER: DelistingReason.STOCK_AND_CASH_MERGER,
    CorporateActionType.REDEMPTION: DelistingReason.REDEMPTION,
    CorporateActionType.WORTHLESS_REMOVAL: DelistingReason.WORTHLESS_REMOVAL,
}


@dataclass(frozen=True)
class DelistingEvidence:
    """Everything the hierarchy may use, as visible at ``now`` (one knowledge basis)."""

    security_id: int
    now: datetime
    actions: Sequence[CorporateAction]  # live, attributed to this security
    coverages: Sequence[CorporateActionCoverage]
    polls: Sequence[AssetStatusObservation]
    filings: Sequence[DelistingFiling]  # only when attributable (issuer has one security)
    calendar: TradingCalendar | None
    bar_dates: Sequence[date]
    symbols: SymbolResolver | None = None  # dated identity for the acquirer (point in time)
    knowledge_basis: KnowledgeBasis = KnowledgeBasis.PROSPECTIVE


def covers(coverages: Sequence[CorporateActionCoverage], start: date, end: date) -> bool:
    """True when the union of ``[range_start, covered_through]`` spans every date in the range."""
    if end < start:
        return True
    cursor = start
    for lo, hi in sorted((c.range_start, c.covered_through) for c in coverages):
        if lo > cursor:
            break
        if hi >= cursor:
            cursor = hi + timedelta(days=1)
        if cursor > end:
            return True
    return False


def window_blockers(
    actions: Sequence[CorporateAction], entry_day: date, exit_day: date
) -> list[CorporateAction]:
    """Uninterpreted actions taking effect in ``(entry_day, exit_day]``: a return window that
    contains one must be UNRESOLVED, never computed as if the action did not exist."""
    return [
        a
        for a in actions
        if a.interpretation is ActionInterpretation.UNINTERPRETED
        and entry_day < a.event_date <= exit_day
    ]


def _calendar_confirms(cal: TradingCalendar, start: date, end: date) -> bool:
    day = start
    while day <= end:
        if cal.status(day) is SessionStatus.UNCOVERED:
            return False
        day += timedelta(days=1)
    return True


def _sessions_closed_between(cal: TradingCalendar, after: datetime, until: datetime) -> int | None:
    """Confirmed sessions closing in ``(after, until]``; None if any date between is uncovered."""
    day, last = after.astimezone(_ET).date(), until.astimezone(_ET).date()
    n = 0
    while day <= last:
        status = cal.status(day)
        if status is SessionStatus.UNCOVERED:
            return None
        if status is SessionStatus.OPEN and after < cal.sessions[day].close_at <= until:
            n += 1
        day += timedelta(days=1)
    return n


def _row(
    ev: DelistingEvidence,
    *,
    status: DelistingStatus,
    reason: DelistingReason,
    last_trade: date | None,
    evidence: list[str],
    source: TerminalReturnSource | None = None,
    terminal_return: float | None = None,
    cash: float | None = None,
    acquirer: int | None = None,
    acquirer_symbol: str | None = None,
    acquirer_rate: float | None = None,
) -> Delisting:
    body = {
        "security_id": ev.security_id,
        "status": status,
        "reason": reason,
        "last_trade_date": last_trade,
        "terminal_return": terminal_return,
        "terminal_return_source": source,
        "cash_per_share": cash,
        "acquirer_security_id": acquirer,
        "acquirer_symbol": acquirer_symbol,
        "acquirer_rate": acquirer_rate,
        "evidence": tuple(sorted(set(evidence))),
        "knowledge_basis": ev.knowledge_basis,
        "derivation_version": DERIVATION_VERSION,
    }
    return Delisting.model_validate(
        {**body, "available_at": ev.now, "source_version": delisting_version(body)}
    )


def _action_ref(a: CorporateAction) -> str:
    return f"action:{a.provider_action_id}:{a.source_version}"[:128]


def _terminal(ev: DelistingEvidence, a: CorporateAction, bars: Sequence[date]) -> Delisting:
    last = max((d for d in bars if d <= a.event_date), default=None)
    reason, refs = _TERMINAL_REASON[a.action_type], [_action_ref(a)]
    t = a.action_type
    if t is CorporateActionType.WORTHLESS_REMOVAL:
        return _row(
            ev,
            status=DelistingStatus.DELISTED,
            reason=reason,
            last_trade=last,
            evidence=refs,
            source=TerminalReturnSource.WORTHLESS,
            terminal_return=WORTHLESS_TERMINAL_RETURN,
        )
    # Consideration is known when its terms are: cash per share, or an acquirer (by symbol) and
    # an exchange ratio. Pricing the acquirer is scoring's job (P6.5); an acquirer that does not
    # resolve to a security yet leaves the return unresolved there, never a -30% default here.
    cash_ok = a.cash_rate is not None and a.cash_rate > 0
    stock_ok = a.stock_rate is not None and a.stock_rate > 0 and a.acquirer_symbol is not None
    known = (
        (t in (CorporateActionType.CASH_MERGER, CorporateActionType.REDEMPTION) and cash_ok)
        or (t is CorporateActionType.STOCK_MERGER and stock_ok)
        or (t is CorporateActionType.STOCK_AND_CASH_MERGER and stock_ok and a.cash_rate is not None)
    )
    if known:
        acquirer = None
        if stock_ok and a.acquirer_symbol is not None and ev.symbols is not None:
            acquirer = ev.symbols.resolve(a.acquirer_symbol, a.event_date)
        return _row(
            ev,
            status=DelistingStatus.DELISTED,
            reason=reason,
            last_trade=last,
            evidence=refs,
            source=TerminalReturnSource.CONSIDERATION,
            cash=a.cash_rate if t is not CorporateActionType.STOCK_MERGER else None,
            acquirer=acquirer,
            acquirer_symbol=a.acquirer_symbol if stock_ok else None,
            acquirer_rate=a.stock_rate if stock_ok else None,
        )
    # A confirmed terminal action proves the delisting; its consideration is unavailable.
    return _row(
        ev,
        status=DelistingStatus.DELISTED,
        reason=reason,
        last_trade=last,
        evidence=refs,
        source=TerminalReturnSource.DEFAULT,
        terminal_return=DEFAULT_TERMINAL_RETURN,
    )


def _effective_form25(
    filings: Sequence[DelistingFiling], today: date
) -> tuple[DelistingFiling, date] | None:
    """The issuer's Form 25 chain effective by ``today``, with its latest filing and the date.

    One base filing plus any amendments; more than one base filing is ambiguous (Form 25 is per
    class, EDGAR is per issuer): fail closed. The latest filing in the chain anchors the 10-day
    floor (an amendment restarts the clock). Paragraph (b)/(c): effective at the floor. Paragraph
    (a): the exchange-specified date (>= floor) is required. Unknown paragraph (an exchange
    ``25-NSE`` whose document is not parsed): fail closed. Commission postponement ((d)(3)) is not
    observable here; the caller still needs an inactive poll on or after the date.
    """
    base = [f for f in filings if not f.amendment]
    if len(base) != 1:
        return None
    chain = sorted(filings, key=lambda f: (f.filing_date, f.available_at, f.accession))
    latest = chain[-1]
    provision = latest.rule_provision or base[0].rule_provision
    floor = latest.earliest_effective_date
    if provision in (Form25Provision.B, Form25Provision.C):
        effective = floor
    elif provision is not None:  # (a)(1)-(4): the exchange's date, never inferred
        stated = latest.stated_effective_date
        if stated is None or stated < floor:
            return None
        effective = stated
    else:
        return None
    return (latest, effective) if effective <= today else None


def derive(ev: DelistingEvidence, *, gap_sessions: int = DEFAULT_GAP_SESSIONS) -> Delisting | None:
    if gap_sessions < 1:
        raise ValueError("gap_sessions must be positive")
    bars = sorted(ev.bar_dates)
    last = bars[-1] if bars else None
    today = ev.now.astimezone(_ET).date()

    terminal = sorted(
        (a for a in ev.actions if a.interpretation is ActionInterpretation.TERMINAL),
        key=lambda a: (a.event_date, a.process_date, a.provider_action_id),
    )
    if terminal:  # rules 1-3
        return _terminal(ev, terminal[0], bars)

    names = [a for a in ev.actions if a.interpretation is ActionInterpretation.IDENTITY]
    if names:  # rule 4
        return _row(
            ev,
            status=DelistingStatus.IDENTITY_CHANGED,
            reason=DelistingReason.NAME_CHANGE,
            last_trade=last,
            evidence=[_action_ref(a) for a in names],
        )

    polls = sorted(ev.polls, key=lambda p: p.observed_at)
    cal = ev.calendar
    if not polls or cal is None or last is None:
        return None

    if polls[-1].status is AssetStatus.INACTIVE:  # rule 5
        inactive = [p for p in polls if p.status is AssetStatus.INACTIVE]
        found = _effective_form25(ev.filings, today)
        if found is None:
            return None
        filing, effective = found
        # Two inactive polls a confirmed session apart, the later one on or after the date the
        # delisting is effective: observed inactivity corroborates actual effectiveness.
        apart = next(
            (
                (a, b)
                for i, a in enumerate(inactive)
                for b in inactive[i + 1 :]
                if b.observed_at.astimezone(_ET).date() >= effective
                and (_sessions_closed_between(cal, a.observed_at, b.observed_at) or 0) >= 1
            ),
            None,
        )
        if (
            apart is not None
            and _calendar_confirms(cal, last, today)
            and covers(ev.coverages, last, effective)
        ):
            refs = [f"poll:{p.observed_at.isoformat()}" for p in apart] + [
                f"form25:{filing.accession}:{effective.isoformat()}"
            ]
            refs += [f"coverage:{c.source_version}" for c in ev.coverages]
            return _row(
                ev,
                status=DelistingStatus.DELISTED,
                reason=DelistingReason.LISTING_TERMINATED,
                last_trade=last,
                evidence=refs,
                source=TerminalReturnSource.DEFAULT,
                terminal_return=DEFAULT_TERMINAL_RETURN,
            )
        return None

    if polls[-1].status is AssetStatus.ACTIVE:  # rule 6
        start = last + timedelta(days=1)
        if not _calendar_confirms(cal, start, today):
            return None
        missing = [
            d
            for d in (start + timedelta(days=i) for i in range((today - start).days + 1))
            if cal.status(d) is SessionStatus.OPEN and cal.sessions[d].close_at <= ev.now
        ]
        if len(missing) >= gap_sessions:
            return _row(
                ev,
                status=DelistingStatus.SUSPECTED_GAP,
                reason=DelistingReason.MARKET_DATA_ABSENCE,
                last_trade=last,
                evidence=[f"gap:{len(missing)}", f"poll:{polls[-1].observed_at.isoformat()}"],
            )
    return None
