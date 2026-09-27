"""Reference capture over Postgres: dynamic D0, deferral, backtest builder, halt sweeper, tasks."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine, text

from config.loader import ReferenceDataConfig, load_config
from contracts.commitment import CommitmentIntegrityError
from contracts.data import UniverseMember
from contracts.enums import (
    KillTrigger,
    McapTier,
    ReferenceSource,
    RefMode,
    RefReason,
    RefStatus,
    RunMode,
    RunStatus,
    Tape,
)
from contracts.errors import ImmutableConflictError
from contracts.market_data import SipTrade
from contracts.models import (
    KillSwitchEvent,
    PortfolioSnapshot,
    ProposedBook,
    ProposedPosition,
    RunRecord,
)
from execution.trade_conditions import load_provider_map
from ingest.alpaca_trades import IncompleteTradesError, SipEntitlementError
from orchestration import tasks
from orchestration.references import CaptureState, ReferenceCapture
from orchestration.sink import DecisionSink
from store import as_of, write
from tests.market_data_support import (
    coverage_for,
    et,
    session_for,
    sessions_2024,
    trade,
    validated_map_for_tests,
)
from tests.orchestration.test_tasks import make_runtime

FRIDAY_CLOSE = et(2024, 1, 12, 16, 0)  # the run's as_of; Monday 15 Jan 2024 is a market holiday
COMMITTED = datetime(2024, 1, 12, 21, 30, tzinfo=UTC)
CFG: ReferenceDataConfig = load_config(allow_placeholders=True, env={}).pipeline.reference_data
WAIT = timedelta(minutes=CFG.calendar_coverage_wait_minutes)
D0_REF = et(2024, 1, 16, 10, 0)  # Tuesday open + 30 minutes


class Quotes:
    """IEX quotes per symbol; a symbol with no entry has no price at all."""

    def __init__(self, missing: set[str] | None = None) -> None:
        self.missing = missing or set()
        self.calls = 0

    def iex_quote(self, symbol: str) -> tuple[float, float] | None:
        self.calls += 1
        return None if symbol in self.missing else (99.9, 100.1)

    def sip_last_trade(self, symbol: str, *, at_or_before: datetime) -> float | None:
        return None


class Trades:
    def __init__(
        self, by_symbol: dict[str, list[SipTrade]] | None = None, error: Exception | None = None
    ):
        self.by_symbol = by_symbol or {}
        self.error = error
        self.calls: list[str] = []

    def historical_trades(self, symbol: str, start: datetime, end: datetime) -> list[SipTrade]:
        self.calls.append(symbol)
        if isinstance(self.error, SipEntitlementError):
            raise self.error
        if self.error is not None and symbol == "AAA":
            raise self.error
        return [t for t in self.by_symbol.get(symbol, []) if start <= t.time <= end]


@pytest.fixture
def engine(pg_engine: Engine) -> Iterator[Engine]:
    def wipe() -> None:
        with pg_engine.begin() as c:
            c.execute(
                text(
                    "TRUNCATE runs, securities, trading_calendar, calendar_coverage "
                    "RESTART IDENTITY CASCADE"
                )
            )

    wipe()
    yield pg_engine
    wipe()


def seed(engine: Engine, *, mode: RunMode = RunMode.LIVE, universe: bool = True) -> UUID:
    """AAA in the book, AAA+BBB in the universe, SPY + XLK reference instruments, a commitment."""
    rid = uuid4()
    with engine.begin() as c:
        a = write.ensure_security(c, ticker="AAA", cik=1001, name="A Inc", sector="Tech")
        b = write.ensure_security(c, ticker="BBB", cik=1002, name="B Inc", sector="Tech")
        write.set_listing(c, a, listed_from=date(2015, 1, 1), listed_to=None)
        write.set_listing(c, b, listed_from=date(2015, 1, 1), listed_to=None)
        write.ensure_reference_instrument(c, ticker="SPY", name="SPY")
        write.ensure_reference_instrument(c, ticker="XLK", name="XLK")
        run = RunRecord(
            run_id=rid,
            mode=mode,
            as_of=FRIDAY_CLOSE,
            config_hash="c" * 64,
            status=RunStatus.COMMITTED,
            started_at=COMMITTED - timedelta(minutes=5),
        )
        write.insert_run(c, run)
        book = ProposedBook(
            run_id=rid,
            as_of=FRIDAY_CLOSE,
            positions=(
                ProposedPosition(
                    security_id=a,
                    entity_token="TICKER_07",
                    sector="Tech",
                    pooled_p=0.6,
                    target_weight=0.05,
                ),
            ),
        )
        write.insert_portfolio_snapshot(
            c, PortfolioSnapshot(run_id=rid, as_of=FRIDAY_CLOSE, book=book, cash_weight=0.95)
        )
        if universe:
            snap = datetime(2023, 12, 29, tzinfo=UTC)
            write.insert_universe_snapshot(
                c,
                [
                    UniverseMember(
                        snapshot_date=date(2023, 12, 29),
                        security_id=sid,
                        mcap_usd=5e9,
                        mcap_tier=McapTier.MID,
                        adv_usd=5e7,
                        price=10.0,
                        score=1.0,
                        rank=n,
                        included=True,
                        reason="included",
                        event_time=snap,
                        available_at=snap,
                        source_version="cfg1",
                    )
                    for n, sid in enumerate((a, b), start=1)
                ],
            )
        c.execute(
            text(
                "INSERT INTO decision_commitments (run_id, sha256, committed_at) "
                "VALUES (:r, :h, :t)"
            ),
            {"r": rid, "h": "d" * 64, "t": COMMITTED},
        )
    return rid


def store_calendar(engine: Engine, *, open_hour_delay: int = 0) -> None:
    sessions = sessions_2024(date(2024, 1, 2), date(2024, 6, 28))
    if open_hour_delay:  # a delayed opening on Tuesday 16 Jan
        day = date(2024, 1, 16)
        sessions = [
            s.model_copy(
                update={
                    "open_at": et(2024, 1, 16, 9 + open_hour_delay, 30),
                    "event_time": et(2024, 1, 16, 9 + open_hour_delay, 30),
                }
            )
            if s.session_date == day
            else s
            for s in sessions
        ]
    with engine.begin() as c:
        write.insert_calendar_range(
            c, sessions, coverage_for(sessions, date(2024, 1, 2), date(2024, 6, 28))
        )


def capture(
    engine: Engine,
    *,
    quotes: Quotes | None = None,
    trades: Trades | None = None,
    pmap: Any = None,
    cfg: ReferenceDataConfig = CFG,
) -> ReferenceCapture:
    return ReferenceCapture(
        engine,
        DecisionSink(engine),
        cfg,
        delay_minutes=30,
        config_hash="c" * 64,
        quotes=quotes or Quotes(),
        trades=trades or Trades(),
        provider_map=pmap or validated_map_for_tests(),
    )


def rows(engine: Engine, run: UUID) -> dict[str, Any]:
    with engine.connect() as c:
        return {r.symbol_ref: r for r in as_of.execution_references(c, run)}


# --- live capture: D0 comes from the stored calendar ----------------------------------------------


def test_a_holiday_monday_moves_the_capture_to_tuesday_open_plus_30(engine: Engine) -> None:
    rid = seed(engine)
    store_calendar(engine)
    cap = capture(engine)
    monday_1000 = et(2024, 1, 15, 10, 0)  # a market holiday: there is no session to observe
    assert cap.capture_live(rid, monday_1000).state is CaptureState.DEFERRED
    tuesday_early = et(2024, 1, 16, 9, 59, 59)
    assert cap.capture_live(rid, tuesday_early).state is CaptureState.DEFERRED
    assert rows(engine, rid) == {}
    result = cap.capture_live(rid, et(2024, 1, 16, 10, 0, 30))
    assert result.state is CaptureState.WRITTEN
    got = rows(engine, rid)
    assert {r.ref_time for r in got.values()} == {D0_REF}
    assert {r.session_date for r in got.values()} == {date(2024, 1, 16)}
    assert {r.mode for r in got.values()} == {RefMode.LIVE}
    assert all(
        r.status is RefStatus.RESOLVED and r.source is ReferenceSource.IEX_MID for r in got.values()
    )


def test_every_required_symbol_gets_evidence_not_only_traded_names(engine: Engine) -> None:
    rid = seed(engine)
    store_calendar(engine)
    capture(engine).capture_live(rid, et(2024, 1, 16, 10, 1))
    # AAA is held; BBB is only in the universe; SPY and XLK are the reference instruments
    assert set(rows(engine, rid)) == {"AAA", "BBB", "SPY", "XLK"}


def test_a_changed_session_moves_the_reference_time_with_it(engine: Engine) -> None:
    rid = seed(engine)
    store_calendar(engine, open_hour_delay=1)  # Tuesday opens 10:30, so the reference is 11:00
    cap = capture(engine)
    assert cap.capture_live(rid, et(2024, 1, 16, 10, 5)).state is CaptureState.DEFERRED
    assert cap.capture_live(rid, et(2024, 1, 16, 11, 1)).state is CaptureState.WRITTEN
    assert {r.ref_time for r in rows(engine, rid).values()} == {et(2024, 1, 16, 11, 0)}


def test_redelivery_is_a_noop_and_finished_runs_are_complete(engine: Engine) -> None:
    rid = seed(engine)
    store_calendar(engine)
    quotes = Quotes()
    cap = capture(engine, quotes=quotes)
    cap.capture_live(rid, et(2024, 1, 16, 10, 1))
    before = rows(engine, rid)
    again = cap.capture_live(rid, et(2024, 1, 16, 10, 2))
    assert again.state is CaptureState.COMPLETE and again.written == 0
    assert rows(engine, rid) == before


def test_a_symbol_without_a_price_waits_then_becomes_durable_unresolved(engine: Engine) -> None:
    rid = seed(engine)
    store_calendar(engine)
    cap = capture(engine, quotes=Quotes(missing={"BBB"}))
    cap.capture_live(rid, et(2024, 1, 16, 10, 1))
    assert "BBB" not in rows(engine, rid)  # the §9 rule may still resolve inside its window
    late = D0_REF + timedelta(minutes=CFG.live_capture_grace_minutes, seconds=1)
    cap.capture_live(rid, late)
    bbb = rows(engine, rid)["BBB"]
    assert bbb.status is RefStatus.UNRESOLVED and bbb.reason is RefReason.NO_REFERENCE_PRICE
    assert bbb.price is None  # never a daily open/close


# --- uncovered calendar: defer, then persist only after the operational timeout ------------------


def test_uncovered_calendar_defers_and_writes_nothing_before_the_timeout(engine: Engine) -> None:
    rid = seed(engine)  # no calendar stored at all
    cap = capture(engine)
    out = cap.capture_live(rid, COMMITTED + WAIT - timedelta(seconds=1))
    assert out.state is CaptureState.DEFERRED and rows(engine, rid) == {}


def test_after_the_timeout_uncovered_is_persisted_without_inventing_a_day(engine: Engine) -> None:
    rid = seed(engine)
    cap = capture(engine)
    at = COMMITTED + WAIT
    assert cap.capture_live(rid, at).state is CaptureState.WRITTEN
    got = rows(engine, rid)
    assert set(got) == {"AAA", "BBB", "SPY", "XLK"}
    assert all(
        r.reason is RefReason.CALENDAR_UNCOVERED and r.ref_time is None and r.session_date is None
        for r in got.values()
    )
    assert cap.capture_live(rid, at + timedelta(days=1)).state is CaptureState.COMPLETE


def test_coverage_arriving_before_the_timeout_leads_to_a_normal_capture(engine: Engine) -> None:
    rid = seed(engine)
    patient = CFG.model_copy(update={"calendar_coverage_wait_minutes": 60 * 24 * 10})
    cap = capture(engine, cfg=patient)  # a timeout that outlasts the reference window
    assert cap.capture_live(rid, et(2024, 1, 16, 10, 1)).state is CaptureState.DEFERRED
    store_calendar(engine)  # the calendar sync finally lands
    assert cap.capture_live(rid, et(2024, 1, 16, 10, 2)).state is CaptureState.WRITTEN
    assert all(r.status is RefStatus.RESOLVED for r in rows(engine, rid).values())


def test_the_timeout_never_decides_d0_and_is_deterministic_on_replay(engine: Engine) -> None:
    rid = seed(engine)
    cap = capture(engine)
    early = COMMITTED + WAIT - timedelta(minutes=1)
    assert [cap.capture_live(rid, early).state for _ in range(3)] == [CaptureState.DEFERRED] * 3
    store_calendar(
        engine
    )  # covered now: even long after the timeout the answer comes from the calendar
    assert cap.capture_live(rid, COMMITTED + WAIT + timedelta(days=30)).written == 4
    got = rows(engine, rid)
    assert all(r.session_date == date(2024, 1, 16) and r.ref_time == D0_REF for r in got.values())
    # the live window was long gone, so no quote taken now is passed off as the D0 observation
    assert {r.reason for r in got.values()} == {RefReason.NO_REFERENCE_PRICE}


def test_a_run_without_a_commitment_is_never_captured(engine: Engine) -> None:
    rid = seed(engine)
    with engine.begin() as c:
        c.execute(
            text("ALTER TABLE decision_commitments DISABLE TRIGGER decision_commitments_immutable")
        )
        c.execute(text("DELETE FROM decision_commitments"))
        c.execute(
            text("ALTER TABLE decision_commitments ENABLE TRIGGER decision_commitments_immutable")
        )
    store_calendar(engine)
    assert capture(engine).capture_live(rid, et(2024, 1, 16, 10, 5)).state is CaptureState.DEFERRED


# --- backtest builder -----------------------------------------------------------------------------


def backtest_trades() -> dict[str, list[SipTrade]]:
    def mine(symbol: str) -> list[SipTrade]:
        return [
            trade(et(2024, 1, 16, 9, 31), 100.0, symbol=symbol, tape=Tape.C),
            trade(et(2024, 1, 16, 9, 59), 101.0, ["@", "I"], symbol=symbol, tape=Tape.C),
            trade(et(2024, 1, 16, 9, 58), 100.5, symbol=symbol, tape=Tape.C),
        ]

    return {s: mine(s) for s in ("AAA", "BBB", "SPY", "XLK")}


RETRO = datetime(2024, 6, 30, tzinfo=UTC)


def test_backtest_builder_stores_the_latest_eligible_sip_trade(engine: Engine) -> None:
    rid = seed(engine, mode=RunMode.BACKTEST)
    store_calendar(engine)
    out = capture(engine, trades=Trades(backtest_trades())).build_backtest(rid, RETRO)
    assert out.state is CaptureState.WRITTEN and out.written == 4
    got = rows(engine, rid)
    for r in got.values():
        assert r.status is RefStatus.RESOLVED and r.price == 100.5  # the odd lot is skipped
        assert r.mode is RefMode.BACKTEST and r.source is ReferenceSource.SIP_LAST
        assert r.trade_time == et(2024, 1, 16, 9, 58) and r.trade_tape is Tape.C
        assert r.ref_time == D0_REF


def test_two_backtest_runs_on_the_same_date_stay_independent(engine: Engine) -> None:
    a = seed(engine, mode=RunMode.BACKTEST)
    store_calendar(engine)
    cap = capture(engine, trades=Trades(backtest_trades()))
    cap.build_backtest(a, RETRO)
    b = seed(engine, mode=RunMode.BACKTEST)
    other = backtest_trades()
    other["SPY"] = [trade(et(2024, 1, 16, 9, 40), 77.0, symbol="SPY", tape=Tape.C)]
    capture(engine, trades=Trades(other)).build_backtest(b, RETRO)
    assert rows(engine, a)["SPY"].price == 100.5 and rows(engine, b)["SPY"].price == 77.0
    assert rows(engine, a)["SPY"].run_id != rows(engine, b)["SPY"].run_id


def test_the_shipped_unvalidated_mapping_blocks_resolution_without_fetching(engine: Engine) -> None:
    rid = seed(engine, mode=RunMode.BACKTEST)
    store_calendar(engine)
    trades = Trades(backtest_trades())
    capture(engine, trades=trades, pmap=load_provider_map()).build_backtest(rid, RETRO)
    got = rows(engine, rid)
    assert trades.calls == []  # a systemic refusal decides every symbol alike, with no network
    assert {r.reason for r in got.values()} == {RefReason.PROVIDER_MAPPING_UNVALIDATED}
    assert all(r.price is None and r.source is None for r in got.values())


def test_a_403_or_unsupported_feed_fails_closed_for_every_symbol(engine: Engine) -> None:
    rid = seed(engine, mode=RunMode.BACKTEST)
    store_calendar(engine)
    capture(engine, trades=Trades(error=SipEntitlementError("403"))).build_backtest(rid, RETRO)
    got = rows(engine, rid)
    assert {r.reason for r in got.values()} == {RefReason.SIP_ENTITLEMENT}
    assert all(r.price is None for r in got.values())


def test_incomplete_trades_and_no_eligible_trade_are_per_symbol_unresolved(engine: Engine) -> None:
    rid = seed(engine, mode=RunMode.BACKTEST)
    store_calendar(engine)
    trades = backtest_trades()
    trades["BBB"] = []  # no trade at all
    capture(engine, trades=Trades(trades, error=IncompleteTradesError("partial"))).build_backtest(
        rid, RETRO
    )
    got = rows(engine, rid)
    assert got["AAA"].reason is RefReason.INCOMPLETE_TRADES
    assert got["BBB"].reason is RefReason.NO_ELIGIBLE_TRADE
    assert got["SPY"].status is RefStatus.RESOLVED


def test_the_backtest_builder_waits_for_the_reference_time(engine: Engine) -> None:
    rid = seed(engine, mode=RunMode.BACKTEST)
    store_calendar(engine)
    early = capture(engine, trades=Trades(backtest_trades())).build_backtest(
        rid, et(2024, 1, 16, 9, 45)
    )
    assert early.state is CaptureState.DEFERRED and rows(engine, rid) == {}


def test_a_contradicting_rebuild_fails_loudly_instead_of_hiding_disagreement(
    engine: Engine,
) -> None:
    rid = seed(engine, mode=RunMode.BACKTEST)
    store_calendar(engine)
    capture(engine, trades=Trades(backtest_trades())).build_backtest(rid, RETRO)
    sink = DecisionSink(engine)
    existing = rows(engine, rid)["SPY"]
    with pytest.raises(ImmutableConflictError, match="price"):
        sink.record_execution_references(
            [existing.model_copy(update={"price": existing.price + 1})]
        )


# --- halt sweeper: the only consumer of halt requests ---------------------------------------------

TAU = et(2024, 1, 17, 11, 0)  # Wednesday, in session


def halt(engine: Engine, rid: UUID, trigger: KillTrigger = KillTrigger.DAILY_LOSS) -> None:
    DecisionSink(engine).record_halt(
        KillSwitchEvent(run_id=rid, triggered_at=TAU, trigger=trigger, daily_loss=0.04)
    )


def sweep(cap: ReferenceCapture, now: datetime, errors: list[Exception] | None = None) -> int:
    sink = errors if errors is not None else []
    return cap.sweep_halt_requests(now, on_error=lambda _r, exc: sink.append(exc))


def halt_rows(engine: Engine, rid: UUID) -> dict[str, Any]:
    with engine.connect() as c:
        return {r.symbol_ref: r for r in as_of.halt_references(c, rid, "daily_loss")}


def test_the_halt_writes_only_a_pending_request_and_touches_no_market_source(
    engine: Engine,
) -> None:
    rid = seed(engine)
    store_calendar(engine)
    quotes = Quotes()
    halt(engine, rid)  # no ReferenceCapture exists at all on the halt path
    with engine.connect() as c:
        (req,) = as_of.halt_reference_requests(c, rid)
        assert as_of.halt_symbol_set(c, rid, "daily_loss") is None  # no discovery on the halt path
    assert req.status.value == "symbols_pending" and quotes.calls == 0


def test_the_sweeper_reconstructs_the_canonical_set_then_marks_each_symbol(engine: Engine) -> None:
    rid = seed(engine)
    store_calendar(engine)
    cap = capture(engine, quotes=Quotes())
    assert sweep(cap, TAU) == 0  # no request yet: nothing to do
    halt(engine, rid)
    assert sweep(cap, TAU + timedelta(minutes=1)) == 4  # within the live grace
    with engine.connect() as c:
        symbol_set = as_of.halt_symbol_set(c, rid, "daily_loss")
        pending = as_of.pending_halt_requests(c)
    assert symbol_set is not None and symbol_set.symbols == ("AAA", "BBB", "SPY", "XLK")
    assert symbol_set.source == "committed_book+universe_snapshot+reference_instruments"
    assert symbol_set.source_version.startswith("halt_symbols_v1:snapshot=2023-12-29")
    got = halt_rows(engine, rid)
    assert set(got) == set(symbol_set.symbols) and pending == []
    assert all(
        r.lag_seconds == 60.0 and r.symbol_set_version == symbol_set.source_version
        for r in got.values()
    )
    assert sweep(cap, TAU + timedelta(minutes=2)) == 0  # redelivery: nothing left


def test_an_empty_reconstruction_is_not_success_and_the_request_stays_pending(
    engine: Engine,
) -> None:
    rid = uuid4()
    with engine.begin() as c:
        write.insert_run(
            c,
            RunRecord(
                run_id=rid,
                mode=RunMode.LIVE,
                as_of=FRIDAY_CLOSE,
                config_hash="c" * 64,
                status=RunStatus.COMMITTED,
                started_at=COMMITTED,
            ),
        )
    halt(engine, rid)
    cap = capture(engine)
    assert sweep(cap, TAU + timedelta(minutes=1)) == 0
    with engine.connect() as c:
        assert as_of.halt_symbol_set(c, rid, "daily_loss") is None
        assert len(as_of.pending_halt_requests(c)) == 1
        assert as_of.halt_references(c, rid, "daily_loss") == []


def test_after_the_live_grace_the_first_eligible_sip_trade_after_tau_is_used(
    engine: Engine,
) -> None:
    rid = seed(engine)
    store_calendar(engine)
    halt(engine, rid)
    day = [
        trade(et(2024, 1, 17, 10, 59), 90.0, tape=Tape.C),  # before tau
        trade(et(2024, 1, 17, 11, 2), 91.0, ["@", "I"], tape=Tape.C),  # ineligible
        trade(et(2024, 1, 17, 11, 3), 92.0, tape=Tape.C),  # the mark
        trade(et(2024, 1, 17, 15, 59), 99.0, tape=Tape.C),  # near the close: not used
    ]
    cap = capture(
        engine,
        trades=Trades(
            {
                s: [t.model_copy(update={"symbol": s}) for t in day]
                for s in ("AAA", "BBB", "SPY", "XLK")
            }
        ),
    )
    now = TAU + timedelta(minutes=30)
    assert sweep(cap, now) == 4
    got = halt_rows(engine, rid)
    assert {r.price for r in got.values()} == {92.0}
    assert {r.observed_at for r in got.values()} == {et(2024, 1, 17, 11, 3)}
    assert {r.lag_seconds for r in got.values()} == {180.0}
    assert all(r.source is ReferenceSource.SIP_LAST for r in got.values())


def test_unresolvable_symbols_wait_until_the_deadline_then_persist_unresolved(
    engine: Engine,
) -> None:
    rid = seed(engine)
    store_calendar(engine)
    halt(engine, rid)
    cap = capture(engine, trades=Trades({}))  # SIP readable, but no eligible trade at all
    mid = TAU + timedelta(minutes=30)
    assert sweep(cap, mid) == 0 and halt_rows(engine, rid) == {}  # deferred, nothing persisted
    deadline = et(2024, 1, 17, 16, 0) + timedelta(minutes=15)
    assert sweep(cap, deadline) == 4
    assert {r.reason for r in halt_rows(engine, rid).values()} == {RefReason.NO_ELIGIBLE_TRADE}
    assert all(r.price is None for r in halt_rows(engine, rid).values())  # never the halt-day close


def test_the_unvalidated_mapping_defers_halt_reconstruction_until_the_deadline(
    engine: Engine,
) -> None:
    rid = seed(engine)
    store_calendar(engine)
    halt(engine, rid)
    cap = capture(engine, pmap=load_provider_map())
    assert sweep(cap, TAU + timedelta(minutes=30)) == 0
    assert sweep(cap, et(2024, 1, 17, 16, 15)) == 4
    assert {r.reason for r in halt_rows(engine, rid).values()} == {
        RefReason.PROVIDER_MAPPING_UNVALIDATED
    }


def test_an_uncovered_calendar_defers_a_halt_then_persists_after_the_timeout(
    engine: Engine,
) -> None:
    rid = seed(engine)  # no calendar
    halt(engine, rid)
    cap = capture(engine)
    assert sweep(cap, TAU + timedelta(minutes=30)) == 0
    assert sweep(cap, TAU + WAIT) == 4
    assert {r.reason for r in halt_rows(engine, rid).values()} == {RefReason.CALENDAR_UNCOVERED}


def test_a_market_failure_in_the_sweeper_is_reported_and_leaves_the_request_pending(
    engine: Engine,
) -> None:
    class Down:
        def iex_quote(self, symbol: str) -> tuple[float, float] | None:
            raise httpx.ConnectError("down")

        def sip_last_trade(self, symbol: str, *, at_or_before: datetime) -> float | None:
            raise httpx.ConnectError("down")

    rid = seed(engine)
    store_calendar(engine)
    halt(engine, rid)
    errors: list[Exception] = []
    cap = ReferenceCapture(
        engine,
        DecisionSink(engine),
        CFG,
        delay_minutes=30,
        config_hash="c" * 64,
        quotes=Down(),
        trades=Trades(),
        provider_map=validated_map_for_tests(),
    )
    assert sweep(cap, TAU + timedelta(minutes=1), errors) == 0
    assert errors and isinstance(errors[0], httpx.ConnectError)
    with engine.connect() as c:
        assert len(as_of.pending_halt_requests(c)) == 1  # the halt itself is unaffected
        status: Any = c.execute(
            text("SELECT status FROM runs WHERE run_id = :r"), {"r": rid}
        ).scalar_one()
    assert status == RunStatus.PARTIAL.value


# --- tasks: retry idempotency and the transient/fatal split --------------------------------------


class FakeReferences:
    def __init__(self, engine: Engine, cap: ReferenceCapture) -> None:
        self.cap = cap

    def capture_due(self, now: datetime, *, on_error: Any) -> int:
        return self.cap.capture_due(now, on_error=on_error)

    def build_backtest(self, run_id: UUID, now: datetime) -> Any:
        return self.cap.build_backtest(run_id, now)

    def sweep_halt_requests(self, now: datetime, *, on_error: Any) -> int:
        return self.cap.sweep_halt_requests(now, on_error=on_error)


def test_task_redelivery_is_idempotent(engine: Engine) -> None:
    rid = seed(engine)
    store_calendar(engine)
    rt, *_ = make_runtime(now=et(2024, 1, 16, 10, 2))
    rt.sink = DecisionSink(engine)
    rt.references = FakeReferences(engine, capture(engine))
    first = tasks.capture_due_references(rt)
    again = tasks.capture_due_references(rt)
    assert first == {"written": 4} and again == {"written": 0}
    assert set(rows(engine, rid)) == {"AAA", "BBB", "SPY", "XLK"}


def test_the_run_lock_makes_a_concurrent_delivery_skip_rather_than_double_write(
    engine: Engine,
) -> None:
    rid = seed(engine)
    store_calendar(engine)
    sink = DecisionSink(engine)
    cap = capture(engine)
    with sink.run_lock(rid, "references") as got:
        assert got
        assert cap.capture_due(et(2024, 1, 16, 10, 2), on_error=lambda *_: None) == 0
    assert rows(engine, rid) == {}
    assert cap.capture_due(et(2024, 1, 16, 10, 2), on_error=lambda *_: None) == 4


def test_a_poll_failure_is_reported_after_the_other_runs_were_processed(engine: Engine) -> None:
    good = seed(engine)
    store_calendar(engine)

    class Boom:
        def iex_quote(self, symbol: str) -> tuple[float, float] | None:
            raise RuntimeError("boom")

        def sip_last_trade(self, symbol: str, *, at_or_before: datetime) -> float | None:
            return None

    rt, *_ = make_runtime(now=et(2024, 1, 16, 10, 2))
    rt.sink = DecisionSink(engine)
    rt.references = FakeReferences(
        engine,
        ReferenceCapture(
            engine,
            DecisionSink(engine),
            CFG,
            delay_minutes=30,
            config_hash="c" * 64,
            quotes=Boom(),
            trades=Trades(),
            provider_map=validated_map_for_tests(),
        ),
    )
    with pytest.raises(tasks.ReferenceJobFailedError):
        tasks.capture_due_references(rt)
    assert rows(engine, good) == {}


@pytest.mark.parametrize(
    ("exc", "transient"),
    [
        (httpx.ConnectError("x"), True),
        (SipEntitlementError("403"), False),  # fail closed: never retried into a loop
        (IncompleteTradesError("partial"), False),
        (ImmutableConflictError("different"), False),
        (CommitmentIntegrityError("x"), False),
        (tasks.ReferenceJobFailedError("job", ["boom"]), False),
    ],
)
def test_transient_vs_fatal_reference_failures(exc: BaseException, transient: bool) -> None:
    assert tasks.is_transient(exc) is transient


def test_calendar_coverage_errors_are_not_retried() -> None:
    from evaluation.calendar_rules import CalendarCoverageError

    assert tasks.is_transient(CalendarCoverageError("uncovered")) is False


def test_reference_jobs_are_not_sla_feeds() -> None:
    full = load_config(allow_placeholders=True, env={}).pipeline
    sla = {f.value for f in full.freshness_sla_hours}
    assert sla == {"price_bars", "fundamentals", "insider_trades", "news"}
    assert not {j.value for j in full.reference_data.cadence_minutes} & sla


def test_session_helper_matches_the_calendar_fixture() -> None:
    assert session_for(date(2024, 1, 16)).open_at == et(2024, 1, 16, 9, 30)
