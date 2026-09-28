"""P6.4 delisting evidence hierarchy (pure): §4.6 and the owner-ratified rules."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from pydantic import ValidationError

from contracts.corporate_actions import (
    DEFAULT_TERMINAL_RETURN,
    AssetStatusObservation,
    CorporateAction,
    Delisting,
    DelistingFiling,
    SecuritySymbol,
    SymbolResolver,
    ex_in_window,
)
from contracts.enums import (
    AssetStatus,
    CorporateActionType,
    DelistingReason,
    DelistingStatus,
    Form25Provision,
    KnowledgeBasis,
    SymbolSource,
    TerminalReturnSource,
)
from evaluation.calendar_rules import build_calendar
from ingest.corporate_actions import ProviderAction, to_action
from tests.corporate_actions_support import coverage, record, utc
from tests.market_data_support import coverage_for, et, sessions_2024
from universe.delistings import DelistingEvidence, covers, derive, window_blockers

SID, ACQ = 1, 2
CAL = build_calendar(
    sessions_2024(), [coverage_for(sessions_2024(), date(2024, 1, 1), date(2024, 12, 31))]
)
NOW = et(2024, 4, 15, 18)


def action(kind: CorporateActionType, **changes: object) -> CorporateAction:
    return to_action(
        ProviderAction(kind, record(kind, **changes)),
        security_id=SID,
        observed_at=utc(2024, 3, 25),
        basis=KnowledgeBasis.PROSPECTIVE,
        resolve=lambda sym, day: ACQ if sym == "BBB" else None,
    )


def poll(at: datetime, status: AssetStatus) -> AssetStatusObservation:
    return AssetStatusObservation(
        security_id=SID,
        symbol="AAA",
        observed_at=at,
        status=status,
        tradable=None,
        source_version="v",
    )


def form25(
    filed: date,
    *,
    accepted: datetime | None = None,
    stated: date | None = None,
    form: str = "25",
    provision: Form25Provision | str | None = "auto",
    accession: str = "0001234567-24-000001",
) -> DelistingFiling:
    paragraph = (
        (Form25Provision.C if form in ("25", "25/A") else None)
        if isinstance(provision, str) and not isinstance(provision, Form25Provision)
        else provision
    )
    return DelistingFiling(
        cik=1001,
        accession=accession,
        form=form,
        filing_date=filed,
        earliest_effective_date=filed + timedelta(days=10),
        rule_provision=paragraph,
        stated_effective_date=stated,
        available_at=accepted or et(filed.year, filed.month, filed.day, 17),
    )


def bars(last: date, first: date = date(2024, 1, 2)) -> list[date]:
    return [s.session_date for s in sessions_2024(first, last)]


def symbol_row(sid: int, symbol: str, valid_from: date = date(1, 1, 1)) -> SecuritySymbol:
    return SecuritySymbol(
        security_id=sid,
        symbol=symbol,
        valid_from=valid_from,
        source=SymbolSource.SEED,
        source_ref="t",
        available_at=utc(2024, 1, 1),
    )


HISTORY = SymbolResolver([symbol_row(SID, "AAA"), symbol_row(ACQ, "BBB")])


def ev(**kw: object) -> DelistingEvidence:
    base: dict[str, object] = {
        "security_id": SID,
        "now": NOW,
        "actions": (),
        "coverages": (),
        "polls": (),
        "filings": (),
        "calendar": CAL,
        "bar_dates": bars(date(2024, 3, 15)),
        "symbols": HISTORY,
    }
    base.update(kw)
    return DelistingEvidence(**base)  # type: ignore[arg-type]


FULL_COVERAGE = (coverage(SID, start=date(2024, 1, 1), end=date(2024, 4, 15), established_at=NOW),)
INACTIVE_POLLS = (
    poll(et(2024, 3, 20, 8), AssetStatus.INACTIVE),
    poll(et(2024, 3, 21, 18), AssetStatus.INACTIVE),
)


# --- rules 1-3: terminal actions -----------------------------------------------------------------


def test_cash_merger_with_consideration_is_delisted_with_inputs_not_a_return() -> None:
    d = derive(ev(actions=[action(CorporateActionType.CASH_MERGER)]))
    assert d is not None and d.status is DelistingStatus.DELISTED
    assert d.terminal_return_source is TerminalReturnSource.CONSIDERATION
    assert d.cash_per_share == pytest.approx(5.37) and d.terminal_return is None
    assert d.reason is DelistingReason.CASH_MERGER
    assert d.last_trade_date == date(2024, 3, 15)  # last bar on or before the effective date


def test_stock_and_mixed_merger_consideration_names_the_acquirer() -> None:
    d = derive(ev(actions=[action(CorporateActionType.STOCK_MERGER)]))
    assert d is not None and d.terminal_return_source is TerminalReturnSource.CONSIDERATION
    assert (d.acquirer_security_id, d.acquirer_rate, d.cash_per_share) == (ACQ, 0.895, None)
    mixed = derive(ev(actions=[action(CorporateActionType.STOCK_AND_CASH_MERGER)]))
    assert mixed is not None and mixed.cash_per_share == pytest.approx(7.8)
    assert mixed.acquirer_rate == pytest.approx(0.7733)


def test_worthless_removal_is_minus_one_hundred_percent() -> None:
    d = derive(ev(actions=[action(CorporateActionType.WORTHLESS_REMOVAL)]))
    assert d is not None and d.terminal_return == -1.0
    assert d.terminal_return_source is TerminalReturnSource.WORTHLESS


def test_terminal_action_without_consideration_takes_the_flagged_default() -> None:
    zero = derive(ev(actions=[action(CorporateActionType.CASH_MERGER, rate=0)]))
    assert zero is not None and zero.status is DelistingStatus.DELISTED
    assert zero.terminal_return_source is TerminalReturnSource.DEFAULT
    assert zero.terminal_return == DEFAULT_TERMINAL_RETURN
    no_acquirer = derive(ev(actions=[action(CorporateActionType.STOCK_MERGER, acquirer_symbol="")]))
    assert no_acquirer is not None
    assert no_acquirer.terminal_return_source is TerminalReturnSource.DEFAULT


def test_known_stock_terms_stay_consideration_with_an_unresolved_acquirer() -> None:
    """D3: an acquirer we cannot resolve yet is not missing consideration."""
    d = derive(ev(actions=[action(CorporateActionType.STOCK_MERGER, acquirer_symbol="QQQQ")]))
    assert d is not None and d.terminal_return_source is TerminalReturnSource.CONSIDERATION
    assert d.terminal_return is None
    assert (d.acquirer_symbol, d.acquirer_security_id, d.acquirer_rate) == ("QQQQ", None, 0.895)
    assert any(e.startswith("action:sm-1:payload:") for e in d.evidence)  # exact version
    # The acquirer is seeded later: the next derivation resolves it (point in time) and differs.
    later = derive(
        ev(
            actions=[action(CorporateActionType.STOCK_MERGER, acquirer_symbol="QQQQ")],
            symbols=SymbolResolver([symbol_row(SID, "AAA"), symbol_row(7, "QQQQ")]),
        )
    )
    assert later is not None and later.acquirer_security_id == 7
    assert later.source_version != d.source_version


def test_acquirer_resolves_by_the_symbol_valid_on_the_effective_date() -> None:
    history = SymbolResolver(
        [
            symbol_row(8, "BBB"),
            symbol_row(8, "OLDB", date(2024, 3, 1)),
            symbol_row(9, "BBB", date(2024, 3, 5)),
        ]
    )
    d = derive(ev(actions=[action(CorporateActionType.STOCK_MERGER)], symbols=history))
    assert d is not None and d.acquirer_security_id == 9  # BBB on 2024-03-20 is security 9


def test_terminal_action_outranks_a_name_change_and_listing_status() -> None:
    d = derive(
        ev(
            actions=[
                action(CorporateActionType.NAME_CHANGE),
                action(CorporateActionType.CASH_MERGER),
            ],
            polls=INACTIVE_POLLS,
        )
    )
    assert d is not None and d.reason is DelistingReason.CASH_MERGER


# --- rule 4: identity ----------------------------------------------------------------------------


def test_name_change_is_identity_continuity_never_a_default() -> None:
    d = derive(ev(actions=[action(CorporateActionType.NAME_CHANGE)], polls=INACTIVE_POLLS))
    assert d is not None and d.status is DelistingStatus.IDENTITY_CHANGED
    assert d.terminal_return is None and d.terminal_return_source is None


# --- rule 5: status-derived default --------------------------------------------------------------


def test_two_inactive_polls_form25_coverage_and_calendar_give_the_default() -> None:
    d = derive(
        ev(polls=INACTIVE_POLLS, filings=[form25(date(2024, 3, 8))], coverages=FULL_COVERAGE)
    )
    assert d is not None and d.status is DelistingStatus.DELISTED
    assert d.reason is DelistingReason.LISTING_TERMINATED
    assert d.terminal_return_source is TerminalReturnSource.DEFAULT
    assert any(e.startswith("form25:") for e in d.evidence)


@pytest.mark.parametrize(
    "missing",
    ["second_poll", "session_apart", "form25", "form25_effective", "coverage", "calendar"],
)
def test_status_default_needs_every_piece_of_evidence(missing: str) -> None:
    polls: tuple[AssetStatusObservation, ...] = INACTIVE_POLLS
    filings = [form25(date(2024, 3, 8))]
    cov = FULL_COVERAGE
    cal = CAL
    if missing == "second_poll":
        polls = INACTIVE_POLLS[:1]
    if missing == "session_apart":  # same evening, no session closes between them
        polls = (
            poll(et(2024, 3, 20, 17), AssetStatus.INACTIVE),
            poll(et(2024, 3, 20, 19), AssetStatus.INACTIVE),
        )
    if missing == "form25":
        filings = []
    if missing == "form25_effective":  # filed, but not yet effective (10 days) at derivation time
        filings = [form25(date(2024, 4, 10))]
    if missing == "coverage":
        cov = (coverage(SID, start=date(2024, 3, 20), end=date(2024, 4, 15), established_at=NOW),)
    if missing == "calendar":
        cal = build_calendar(
            sessions_2024(end=date(2024, 3, 31)),
            [
                coverage_for(
                    sessions_2024(end=date(2024, 3, 31)), date(2024, 1, 1), date(2024, 3, 31)
                )
            ],
        )
    assert derive(ev(polls=polls, filings=filings, coverages=cov, calendar=cal)) is None


def test_form15_is_neither_required_nor_accepted_as_form25() -> None:
    with pytest.raises(ValidationError):
        DelistingFiling(
            cik=1001,
            accession="0001234567-24-000002",
            form="15-12B",
            filing_date=date(2024, 3, 20),
            earliest_effective_date=date(2024, 3, 30),
            available_at=et(2024, 3, 20, 17),
        )
    # The rule fires with Form 25 alone (no Form 15 in the evidence).
    d = derive(
        ev(polls=INACTIVE_POLLS, filings=[form25(date(2024, 3, 8))], coverages=FULL_COVERAGE)
    )
    assert d is not None


def test_a_later_active_poll_cancels_the_status_rule() -> None:
    polls = (*INACTIVE_POLLS, poll(et(2024, 3, 22, 18), AssetStatus.ACTIVE))
    d = derive(ev(polls=polls, filings=[form25(date(2024, 3, 8))], coverages=FULL_COVERAGE))
    assert d is None or d.status is not DelistingStatus.DELISTED


# --- rule 6 and missing bars ---------------------------------------------------------------------


def test_ten_confirmed_sessions_without_bars_while_active_is_only_a_suspected_gap() -> None:
    last = date(2024, 3, 28)
    active = (poll(et(2024, 4, 15, 8), AssetStatus.ACTIVE),)
    d = derive(ev(bar_dates=bars(last), polls=active))
    assert d is not None and d.status is DelistingStatus.SUSPECTED_GAP
    assert d.terminal_return is None and d.terminal_return_source is None


def test_nine_missing_sessions_derive_nothing() -> None:
    now = et(2024, 4, 11, 18)  # sessions 3/28 .. 4/11 minus the last bar = 9 missing after 3/28
    active = (poll(et(2024, 4, 11, 8), AssetStatus.ACTIVE),)
    assert derive(ev(now=now, bar_dates=bars(date(2024, 3, 28)), polls=active)) is None


@pytest.mark.parametrize("polls", [(), (poll(et(2024, 4, 15, 8), AssetStatus.NOT_FOUND),)])
def test_missing_bars_alone_never_authorize_the_default(
    polls: tuple[AssetStatusObservation, ...],
) -> None:
    d = derive(ev(bar_dates=bars(date(2024, 1, 31)), polls=polls, coverages=FULL_COVERAGE))
    assert d is None or d.terminal_return_source is not TerminalReturnSource.DEFAULT
    one_bar_missing = derive(ev(bar_dates=bars(date(2024, 4, 12)), polls=polls))
    assert one_bar_missing is None


def test_inactive_without_form25_and_long_gap_is_not_a_default() -> None:
    d = derive(ev(bar_dates=bars(date(2024, 1, 31)), polls=INACTIVE_POLLS, coverages=FULL_COVERAGE))
    assert d is None


# --- coverage, window blockers, contracts --------------------------------------------------------


def test_coverage_union_and_fetch_day_clamp() -> None:
    a = coverage(SID, start=date(2024, 3, 1), end=date(2024, 3, 31), established_at=utc(2024, 4, 1))
    b = coverage(
        SID, start=date(2024, 4, 1), end=date(2024, 4, 30), established_at=utc(2024, 4, 20)
    )
    assert covers([a, b], date(2024, 3, 5), date(2024, 4, 18))
    # A query cannot vouch for process dates on or after the day it ran.
    assert not covers([a, b], date(2024, 3, 5), date(2024, 4, 20))
    assert not covers([b], date(2024, 3, 31), date(2024, 4, 2))
    assert b.covered_through == date(2024, 4, 19)


def test_uninterpreted_actions_block_windows_containing_them() -> None:
    spin = action(CorporateActionType.SPIN_OFF)  # ex 2024-03-15
    rights = action(CorporateActionType.RIGHTS_DISTRIBUTION)  # ex 2024-03-17
    split = action(CorporateActionType.FORWARD_SPLIT)  # interpreted
    assert window_blockers([spin, rights, split], date(2024, 3, 14), date(2024, 3, 20)) == [
        spin,
        rights,
    ]
    assert window_blockers([spin], date(2024, 3, 15), date(2024, 3, 20)) == []  # entry day excluded


def test_ex_date_eligibility_is_entry_exclusive_exit_inclusive() -> None:
    assert not ex_in_window(date(2024, 3, 12), date(2024, 3, 12), date(2024, 3, 20))
    assert ex_in_window(date(2024, 3, 11), date(2024, 3, 12), date(2024, 3, 12))
    assert not ex_in_window(date(2024, 3, 1), date(2024, 3, 13), date(2024, 3, 12))


def test_delisting_contract_pins_the_default_and_its_flag() -> None:
    d = derive(ev(actions=[action(CorporateActionType.CASH_MERGER, rate=0)]))
    assert d is not None
    data = d.model_dump()
    with pytest.raises(ValidationError):
        Delisting.model_validate({**data, "terminal_return": -0.5})
    with pytest.raises(ValidationError):
        Delisting.model_validate({**data, "status": DelistingStatus.SUSPECTED_GAP})
    with pytest.raises(ValidationError):  # source_version must match the conclusion
        Delisting.model_validate({**data, "evidence": ("other",)})


# --- D4/P6.4b: Form 25 timing by 17 CFR 240.12d2-2 path -------------------------------------------


def status_default(
    filings: list[DelistingFiling], polls: tuple[AssetStatusObservation, ...]
) -> Delisting | None:
    return derive(ev(polls=polls, filings=filings, coverages=FULL_COVERAGE))


def effective_of(d: Delisting | None) -> str | None:
    if d is None:
        return None
    (ref,) = [e for e in d.evidence if e.startswith("form25:")]
    return ref.rsplit(":", 1)[1]


LATER = (*INACTIVE_POLLS, poll(et(2024, 3, 26, 18), AssetStatus.INACTIVE))
LATEST = (*LATER, poll(et(2024, 4, 2, 18), AssetStatus.INACTIVE))


def test_issuer_form25_is_effective_ten_days_after_filing() -> None:
    # Filed 3/15 -> 3/25: polls on 3/20-3/21 are too early; one on 3/26 corroborates.
    assert status_default([form25(date(2024, 3, 15))], INACTIVE_POLLS) is None
    assert effective_of(status_default([form25(date(2024, 3, 15))], LATER)) == "2024-03-25"


def test_exchange_paragraph_b_is_effective_ten_days_after_filing() -> None:
    nse_b = form25(date(2024, 3, 15), form="25-NSE", provision=Form25Provision.B)
    assert status_default([nse_b], INACTIVE_POLLS) is None
    assert effective_of(status_default([nse_b], LATER)) == "2024-03-25"


def test_exchange_paragraph_a_needs_the_stated_date() -> None:
    no_date = form25(date(2024, 3, 15), form="25-NSE", provision=Form25Provision.A1)
    assert status_default([no_date], LATEST) is None  # never inferred from the floor
    dated = form25(
        date(2024, 3, 15), form="25-NSE", provision=Form25Provision.A2, stated=date(2024, 4, 1)
    )
    assert status_default([dated], LATER) is None  # polls end 3/26, before 4/1
    assert effective_of(status_default([dated], LATEST)) == "2024-04-01"


def test_unparsed_exchange_filing_fails_closed() -> None:
    unparsed = form25(date(2024, 3, 8), form="25-NSE")
    assert unparsed.rule_provision is None
    assert status_default([unparsed], LATEST) is None


def test_an_amendment_restarts_the_ten_day_clock() -> None:
    base = form25(date(2024, 3, 8))  # floor 3/18
    amended = form25(date(2024, 3, 20), form="25/A", accession="0001234567-24-000009")  # 3/30
    assert status_default([base, amended], LATER) is None  # 3/26 precedes the new floor
    d = status_default([base, amended], LATEST)
    assert effective_of(d) == "2024-03-30"
    assert d is not None and any("0001234567-24-000009" in e for e in d.evidence)


def test_two_base_filings_fail_closed() -> None:
    base = form25(date(2024, 3, 8))
    other = form25(date(2024, 3, 9), accession="0001234567-24-000010")
    assert status_default([base, other], LATEST) is None
    assert status_default([base], LATEST) is not None


def test_stated_date_before_the_floor_and_mislabelled_paragraphs_are_rejected() -> None:
    with pytest.raises(ValidationError):
        form25(
            date(2024, 3, 15), form="25-NSE", provision=Form25Provision.A1, stated=date(2024, 3, 20)
        )
    with pytest.raises(ValidationError):  # an issuer filing is paragraph (c)
        form25(date(2024, 3, 15), form="25", provision=Form25Provision.B)
    with pytest.raises(ValidationError):  # an exchange cannot file under (c)
        form25(date(2024, 3, 15), form="25-NSE", provision=Form25Provision.C)
    with pytest.raises(ValidationError):  # Form 15 is not delisting evidence
        form25(date(2024, 3, 15), form="15-12B", provision=None)
