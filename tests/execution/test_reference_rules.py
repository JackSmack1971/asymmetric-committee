"""Reference resolution (§4.6, §9): live rule, backtest SIP selection, halt reconstruction."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from contracts.enums import ReferenceSource, RefReason, RefStatus, Tape
from execution import reference_rules
from execution.reference_rules import reconstruct_halt, resolve_backtest, resolve_live
from execution.trade_conditions import load_provider_map
from tests.market_data_support import (
    et,
    session_for,
    trade,
    validated_map_for_tests,
)

MAP = validated_map_for_tests()
DAY = session_for(et(2024, 3, 4).date())  # Monday 2024-03-04, 09:30-16:00
REF = DAY.open_at + timedelta(minutes=30)  # 10:00


# --- live: the shared §9 resolver, any timestamp ------------------------------------------------


class Quotes:
    def __init__(self, quote: tuple[float, float] | None, sip: float | None) -> None:
        self.quote, self.sip = quote, sip
        self.sip_at: list[datetime] = []

    def iex_quote(self, symbol: str) -> tuple[float, float] | None:
        return self.quote

    def sip_last_trade(self, symbol: str, *, at_or_before: datetime) -> float | None:
        self.sip_at.append(at_or_before)
        return self.sip


def test_live_resolver_reuses_the_section_9_rule() -> None:
    tight = resolve_live(Quotes((99.9, 100.1), None), "SPY", REF)
    assert (tight.status, tight.source, tight.price) == (
        RefStatus.RESOLVED,
        ReferenceSource.IEX_MID,
        100.0,
    )
    wide_quotes = Quotes((90.0, 110.0), 101.0)  # spread far above 50 bps -> SIP trade >= 15 min old
    wide = resolve_live(wide_quotes, "SPY", REF)
    assert (wide.source, wide.price) == (ReferenceSource.SIP_LAST, 101.0)
    assert wide_quotes.sip_at == [REF - timedelta(minutes=15)]


def test_live_resolver_delegates_to_reference_price_not_a_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, datetime]] = []

    def spy(source: object, symbol: str, now: datetime) -> tuple[float, ReferenceSource]:
        seen.append((symbol, now))
        return 5.0, ReferenceSource.IEX_MID

    monkeypatch.setattr(reference_rules, "reference_price", spy)
    at = et(2024, 3, 9, 3, 17)  # a Saturday 03:17: the resolver knows nothing about sessions
    obs = resolve_live(Quotes(None, None), "XLK", at)
    assert seen == [("XLK", at)] and obs.price == 5.0 and obs.ref_time == at


def test_live_resolver_reports_absence_as_unresolved_evidence() -> None:
    obs = resolve_live(Quotes(None, None), "SPY", REF)
    assert obs.status is RefStatus.UNRESOLVED and obs.reason is RefReason.NO_REFERENCE_PRICE
    assert obs.price is None and obs.source is None


# --- backtest: latest eligible SIP trade at or before open + 30 minutes -------------------------


def at(hh: int, mm: int, ss: int = 0) -> datetime:
    return et(2024, 3, 4, hh, mm, ss)


def test_latest_eligible_trade_at_or_before_the_reference_time() -> None:
    trades = [
        trade(at(9, 29, 59), 99.0),  # pre-open: never eligible
        trade(at(9, 31), 100.0),
        trade(at(9, 45), 101.0),
        trade(at(9, 59), 102.0, ["@", "I"]),  # odd lot: ineligible, skipped
        trade(at(10, 0), 103.0),  # exactly at the reference time: included
        trade(at(10, 0, 1), 104.0),  # after: excluded
    ]
    obs = resolve_backtest("SPY", trades, DAY, REF, MAP)
    assert obs.status is RefStatus.RESOLVED and obs.price == 103.0
    assert obs.source is ReferenceSource.SIP_LAST and obs.trade_tape is Tape.C
    assert obs.trade_time == at(10, 0)


def test_ineligible_latest_trade_falls_back_to_the_previous_eligible_one() -> None:
    trades = [trade(at(9, 45), 101.0), trade(at(9, 59), 102.0, ["@", "I"])]
    assert resolve_backtest("SPY", trades, DAY, REF, MAP).price == 101.0


def test_unknown_condition_on_the_candidate_is_unresolved_not_skipped() -> None:
    trades = [trade(at(9, 45), 101.0), trade(at(9, 59), 102.0, ["?"])]
    obs = resolve_backtest("SPY", trades, DAY, REF, MAP)
    assert obs.status is RefStatus.UNRESOLVED and obs.reason is RefReason.UNKNOWN_CONDITION
    assert obs.price is None


def test_no_eligible_trade_is_unresolved_and_never_a_daily_price() -> None:
    for trades in ([], [trade(at(9, 40), 100.0, ["T"])], [trade(at(9, 29), 100.0)]):
        obs = resolve_backtest("SPY", trades, DAY, REF, MAP)
        assert obs.status is RefStatus.UNRESOLVED and obs.reason is RefReason.NO_ELIGIBLE_TRADE
        assert obs.source is None


def test_reference_time_outside_the_session_is_unresolved() -> None:
    obs = resolve_backtest("SPY", [trade(at(9, 45), 101.0)], DAY, at(9, 0), MAP)
    assert obs.status is RefStatus.UNRESOLVED


def test_the_unvalidated_shipped_map_refuses_everything() -> None:
    obs = resolve_backtest("SPY", [trade(at(9, 45), 101.0)], DAY, REF, load_provider_map())
    assert obs.status is RefStatus.UNRESOLVED
    assert obs.reason is RefReason.PROVIDER_MAPPING_UNVALIDATED


def test_tied_eligible_trades_at_different_prices_are_ambiguous() -> None:
    trades = [trade(at(9, 59), 101.0), trade(at(9, 59), 101.5)]
    obs = resolve_backtest("SPY", trades, DAY, REF, MAP)
    assert obs.reason is RefReason.AMBIGUOUS_ORDER
    same = [trade(at(9, 59), 101.0), trade(at(9, 59), 101.0)]
    assert resolve_backtest("SPY", same, DAY, REF, MAP).price == 101.0


def test_conditional_code_is_evaluated_against_the_same_day_history() -> None:
    first = [trade(at(9, 40), 100.0, ["@", "Z"])]  # the first qualifying last of the day: eligible
    assert resolve_backtest("SPY", first, DAY, REF, MAP).price == 100.0
    later = [trade(at(9, 35), 99.0), trade(at(9, 40), 100.0, ["@", "Z"])]  # not the first: skipped
    assert resolve_backtest("SPY", later, DAY, REF, MAP).price == 99.0


def test_cts_note_3_sold_last_needs_facts_we_lack_once_a_last_exists() -> None:
    a = Tape.A  # the provider's "@" is the CTS space (regular sale) on tapes A and B
    only = [trade(at(9, 40), 100.0, ["L"], tape=a)]
    assert resolve_backtest("IBM", only, DAY, REF, MAP).price == 100.0  # the only qualifying last
    after = [trade(at(9, 35), 99.0, ["@"], tape=a), trade(at(9, 40), 100.0, ["L"], tape=a)]
    obs = resolve_backtest("IBM", after, DAY, REF, MAP)
    assert obs.status is RefStatus.UNRESOLVED and obs.reason is RefReason.UNKNOWN_CONDITION


def test_an_undecided_earlier_trade_makes_a_later_conditional_unknown() -> None:
    trades = [trade(at(9, 35), 99.0, ["?"]), trade(at(9, 40), 100.0, ["@", "Z"])]
    obs = resolve_backtest("SPY", trades, DAY, REF, MAP)
    assert obs.status is RefStatus.UNRESOLVED


# --- halt: the first eligible trade at or after the trigger -------------------------------------


def test_halt_reconstruction_takes_the_first_eligible_trade_after_tau() -> None:
    tau = at(10, 15)
    trades = [
        trade(at(10, 14), 100.0),  # before tau
        trade(at(10, 16), 101.0, ["@", "I"]),  # ineligible
        trade(at(10, 17), 102.0),  # first eligible after tau
        trade(at(10, 18), 103.0),
    ]
    obs = reconstruct_halt("SPY", trades, tau, [DAY], MAP)
    assert obs.status is RefStatus.RESOLVED and obs.price == 102.0
    assert obs.trade_time == at(10, 17)


def test_halt_after_the_close_uses_the_next_session_and_never_the_close() -> None:
    tue = session_for(et(2024, 3, 5).date())
    tau = at(17, 0)  # Monday after the close
    monday_close = trade(at(15, 59), 90.0)
    tuesday_open = trade(et(2024, 3, 5, 9, 31), 95.0)
    obs = reconstruct_halt("SPY", [monday_close, tuesday_open], tau, [DAY, tue], MAP)
    assert obs.price == 95.0


def test_halt_with_no_eligible_trade_or_no_session_is_unresolved() -> None:
    tau = at(10, 15)
    none = reconstruct_halt("SPY", [trade(at(10, 16), 1.0, ["T"])], tau, [DAY], MAP)
    assert none.reason is RefReason.NO_ELIGIBLE_TRADE
    nocal = reconstruct_halt("SPY", [], tau, [], MAP)
    assert nocal.reason is RefReason.CALENDAR_UNCOVERED
    unknown = reconstruct_halt("SPY", [trade(at(10, 16), 1.0, ["?"])], tau, [DAY], MAP)
    assert unknown.reason is RefReason.UNKNOWN_CONDITION


def test_halt_works_at_an_arbitrary_tau() -> None:
    trades = [trade(at(9, 30, 5), 100.0), trade(at(15, 59, 59), 110.0)]
    assert reconstruct_halt("SPY", trades, at(9, 30, 5), [DAY], MAP).price == 100.0
    assert reconstruct_halt("SPY", trades, at(12, 0), [DAY], MAP).price == 110.0
