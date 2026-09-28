"""P6.3 storage: reference instruments, calendar + coverage, vintages, references, halt requests."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from contracts.enums import (
    KillTrigger,
    ReferenceSource,
    RefMode,
    RefReason,
    RefStatus,
    RunMode,
    RunStatus,
    SecurityKind,
    Tape,
)
from contracts.errors import ImmutableConflictError
from contracts.market_data import (
    BenchmarkPeriodReference,
    ExecutionReference,
    HaltReference,
    HaltSymbolSet,
    TBillObservation,
    TBillVintageCoverage,
    symbols_sha256,
)
from contracts.models import KillSwitchEvent, RunRecord
from evaluation.calendar_rules import CalendarCoverageError, SessionStatus
from store import as_of, write
from tests.market_data_support import FETCHED, coverage_for, et, sessions_2024
from tests.store import factories as f

NOW = datetime(2024, 6, 30, tzinfo=UTC)


def make_run(conn: Connection, *, status: RunStatus = RunStatus.COMMITTED) -> UUID:
    rid = uuid4()
    write.insert_run(
        conn,
        RunRecord(
            run_id=rid,
            mode=RunMode.LIVE,
            as_of=datetime(2024, 3, 1, 21, 0, tzinfo=UTC),
            config_hash="a" * 64,
            status=status,
            started_at=datetime(2024, 3, 1, 21, 5, tzinfo=UTC),
        ),
    )
    return rid


# --- ETF identity: no synthetic CIK, never surfaced as an equity --------------------------------


def test_reference_instruments_have_a_null_cik_and_are_idempotent(db: Connection) -> None:
    a = write.ensure_reference_instrument(db, ticker="SPY", name="SPDR S&P 500")
    b = write.ensure_reference_instrument(db, ticker="SPY", name="SPDR S&P 500 ETF Trust")
    assert a == b
    row = db.execute(text("SELECT cik, kind, name FROM securities WHERE ticker='SPY'")).one()
    assert row.cik is None and row.kind == "etf" and row.name.endswith("Trust")


def test_the_database_enforces_equity_has_cik_and_etf_has_none(db: Connection) -> None:
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(
            text("INSERT INTO securities (ticker, cik, name, kind) VALUES ('X', 5, 'x', 'etf')")
        )
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(
            text(
                "INSERT INTO securities (ticker, cik, name, kind) VALUES ('Y', NULL, 'y', 'equity')"
            )
        )
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(
            text(
                "INSERT INTO securities (ticker, cik, name, kind) VALUES ('Z', NULL, 'z', 'stock')"
            )
        )
    with pytest.raises(ValueError):
        write.ensure_reference_instrument(db, ticker="Q", name="q", kind=SecurityKind.EQUITY)


def test_etfs_never_surface_through_equity_readers(db: Connection) -> None:
    eq = f.security(db, "AAA", 1001)
    write.ensure_reference_instrument(db, ticker="SPY", name="SPY")
    write.ensure_reference_instrument(db, ticker="XLK", name="XLK")
    assert [s.security_id for s in as_of.securities(db)] == [eq]
    assert [i.ticker for i in as_of.reference_instruments(db)] == ["SPY", "XLK"]
    assert write.security_ids_by_ticker(db) == {"AAA": eq}
    assert set(write.security_ids_by_cik(db)) == {1001}
    listed = as_of.alias_list(db, datetime(2024, 3, 1, tzinfo=UTC))
    assert {s.security_id for s in listed.securities} == {eq}  # no ETF alias is ever built
    assert all(s.cik for s in as_of.securities(db))  # every equity carries a genuine CIK


def test_etf_bars_are_stored_through_the_ordinary_raw_bar_path(db: Connection) -> None:
    sid = write.ensure_reference_instrument(db, ticker="SPY", name="SPY")
    bar = f.bar(sid, date(2024, 3, 4), close=512.0)
    assert write.insert_price_bars(db, [bar]) == 1
    assert write.insert_price_bars(db, [bar]) == 0  # idempotent, same as any equity bar
    got = as_of.prices(db, [sid], datetime(2024, 3, 6, tzinfo=UTC), timedelta(days=30))
    assert [b.close for b in got] == [512.0] and got[0].feed.value == "sip"


# --- calendar + coverage in one point-in-time view ----------------------------------------------


def store_calendar(db: Connection, start: date, end: date, **kw: Any) -> None:
    sessions = sessions_2024(start, end, **kw)
    write.insert_calendar_range(
        db,
        sessions,
        coverage_for(sessions, start, end, **{k: v for k, v in kw.items() if k == "available_at"}),
    )


def test_calendar_round_trips_and_distinguishes_holiday_from_unfetched(db: Connection) -> None:
    store_calendar(db, date(2024, 1, 2), date(2024, 3, 29))
    cal = as_of.trading_calendar(db, NOW)
    assert cal.status(date(2024, 1, 15)) is SessionStatus.CLOSED  # MLK day, inside coverage
    assert cal.status(date(2024, 4, 1)) is SessionStatus.UNCOVERED  # never fetched
    assert cal.session(date(2024, 1, 16)).open_at == et(2024, 1, 16, 9, 30)
    assert cal.d0(date(2024, 1, 12)) == date(2024, 1, 16)


def test_nothing_is_visible_before_it_was_fetched(db: Connection) -> None:
    store_calendar(db, date(2024, 1, 2), date(2024, 1, 31))
    early = as_of.trading_calendar(db, FETCHED - timedelta(days=1))
    assert early.status(date(2024, 1, 16)) is SessionStatus.UNCOVERED


def test_a_revision_is_a_new_row_and_never_changes_an_earlier_read(db: Connection) -> None:
    t1 = FETCHED + timedelta(days=30)
    store_calendar(db, date(2024, 3, 1), date(2024, 3, 8))
    v2 = sessions_2024(date(2024, 3, 1), date(2024, 3, 8), available_at=t1, version="v2")
    v2 = [
        s.model_copy(update={"close_at": et(2024, 3, 4, 13, 0)})
        if s.session_date == date(2024, 3, 4)
        else s
        for s in v2
    ]
    write.insert_calendar_range(
        db, v2, coverage_for(v2, date(2024, 3, 1), date(2024, 3, 8), available_at=t1)
    )
    assert as_of.trading_calendar(db, t1).session(date(2024, 3, 4)).close_at == et(
        2024, 3, 4, 13, 0
    )
    before = as_of.trading_calendar(db, t1 - timedelta(seconds=1))
    assert before.session(date(2024, 3, 4)).close_at == et(2024, 3, 4, 16, 0)
    n = db.execute(text("SELECT count(*) FROM trading_calendar WHERE session_date='2024-03-04'"))
    assert n.scalar_one() == 2  # both versions are kept


def test_exact_replay_is_a_noop_and_keeps_the_first_available_at(db: Connection) -> None:
    store_calendar(db, date(2024, 3, 1), date(2024, 3, 8))
    later = FETCHED + timedelta(days=5)
    again = sessions_2024(date(2024, 3, 1), date(2024, 3, 8), available_at=later)
    assert (
        write.insert_calendar_range(
            db, again, coverage_for(again, date(2024, 3, 1), date(2024, 3, 8), available_at=later)
        )
        == 0
    )
    first: Any = db.execute(text("SELECT min(available_at) FROM trading_calendar")).scalar_one()
    assert first == FETCHED


def test_a_contradicting_session_under_one_version_fails_loudly(db: Connection) -> None:
    store_calendar(db, date(2024, 3, 1), date(2024, 3, 8))
    liar = sessions_2024(date(2024, 3, 1), date(2024, 3, 8))
    liar = [
        s.model_copy(update={"close_at": et(2024, 3, 4, 12, 0)})
        if s.session_date == date(2024, 3, 4)
        else s
        for s in liar
    ]
    with pytest.raises(ImmutableConflictError, match="close_at"):
        write.insert_calendar_range(
            db, liar, coverage_for(liar, date(2024, 3, 1), date(2024, 3, 8))
        )


def test_coverage_must_describe_exactly_its_sessions(db: Connection) -> None:
    sessions = sessions_2024(date(2024, 3, 1), date(2024, 3, 8))
    wrong = coverage_for(sessions[:-1], date(2024, 3, 1), date(2024, 3, 8))
    with pytest.raises(ValueError, match="coverage"):
        write.insert_calendar_range(db, sessions, wrong)
    outside = sessions_2024(date(2024, 3, 1), date(2024, 3, 8))
    with pytest.raises(ValueError, match="outside"):
        write.insert_calendar_range(
            db, outside, coverage_for(outside, date(2024, 3, 1), date(2024, 3, 4))
        )
    assert db.execute(text("SELECT count(*) FROM trading_calendar")).scalar_one() == 0


def test_sessions_and_coverage_are_written_atomically(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = sessions_2024(date(2024, 3, 1), date(2024, 3, 8))
    cov = coverage_for(sessions, date(2024, 3, 1), date(2024, 3, 8))
    original = write._insert_immutable

    def boom(conn: Connection, table: Any, models: Any, key_cols: Any) -> int:
        if table.name == "calendar_coverage":
            raise RuntimeError("disk full")
        return original(conn, table, models, key_cols)

    monkeypatch.setattr(write, "_insert_immutable", boom)
    with pytest.raises(RuntimeError):
        write.insert_calendar_range(db, sessions, cov)
    assert db.execute(text("SELECT count(*) FROM trading_calendar")).scalar_one() == 0  # no orphans


def test_a_tampered_coverage_row_makes_the_view_fail_closed(db: Connection) -> None:
    store_calendar(db, date(2024, 3, 1), date(2024, 3, 8))
    db.execute(text("ALTER TABLE calendar_coverage DISABLE TRIGGER calendar_coverage_immutable"))
    db.execute(text("UPDATE calendar_coverage SET sessions_sha256 = repeat('0', 64)"))
    with pytest.raises(CalendarCoverageError):
        as_of.trading_calendar(db, NOW)


def test_calendar_tables_are_insert_only(db: Connection) -> None:
    store_calendar(db, date(2024, 3, 1), date(2024, 3, 8))
    for stmt in (
        "UPDATE trading_calendar SET close_at = close_at",
        "DELETE FROM trading_calendar",
        "UPDATE calendar_coverage SET session_count = 0",
        "DELETE FROM calendar_coverage",
    ):
        with pytest.raises(DBAPIError, match="insert-only"), db.begin_nested():
            db.execute(text(stmt))


# --- DGS3MO vintages ----------------------------------------------------------------------------


def obs(observation: date, vintage: date, pct: float) -> TBillObservation:
    return TBillObservation(
        series="DGS3MO",
        observation_date=observation,
        yield_pct=pct,
        vintage_date=vintage,
        source_version=f"alfred:{vintage.isoformat()}",
    )


def test_tbill_reader_returns_only_vintages_before_the_accrual_date(db: Connection) -> None:
    rows = [
        obs(date(2024, 3, 6), date(2024, 3, 7), 5.20),
        obs(date(2024, 3, 6), date(2024, 4, 15), 5.30),  # a later revision
        obs(date(2024, 3, 7), date(2024, 3, 11), 5.21),  # vintage on the accrual day
    ]
    assert write.insert_tbill_rates(db, rows) == 3
    got = as_of.tbill_rates(db, "DGS3MO", date(2024, 3, 11))
    assert [(r.observation_date, r.yield_pct) for r in got] == [(date(2024, 3, 6), 5.20)]
    later = as_of.tbill_rates(db, "DGS3MO", date(2024, 5, 1))
    assert sorted(r.yield_pct for r in later) == [5.20, 5.21, 5.30]  # appended, none replaced


def test_tbill_replay_and_contradiction(db: Connection) -> None:
    row = obs(date(2024, 3, 6), date(2024, 3, 7), 5.20)
    assert write.insert_tbill_rates(db, [row]) == 1
    assert write.insert_tbill_rates(db, [row]) == 0
    with pytest.raises(ImmutableConflictError, match="yield_pct"):
        write.insert_tbill_rates(db, [obs(date(2024, 3, 6), date(2024, 3, 7), 5.25)])
    with pytest.raises(DBAPIError, match="insert-only"), db.begin_nested():
        db.execute(text("UPDATE tbill_rates SET yield_pct = 0"))


def test_tbill_has_no_availability_timestamp_column(pg_engine: Engine) -> None:
    from sqlalchemy import inspect

    cols = {c["name"] for c in inspect(pg_engine).get_columns("tbill_rates")}
    assert "available_at" not in cols and {"vintage_date", "ingested_at"} <= cols


def test_vintage_coverage_is_versioned_and_read_as_of(db: Connection) -> None:
    def cov(earliest: date, at: datetime, sha: str) -> TBillVintageCoverage:
        return TBillVintageCoverage(
            series="DGS3MO",
            earliest_vintage=earliest,
            latest_vintage=date(2024, 3, 1),
            vintage_count=10,
            vintage_dates_sha256=sha * 64,
            established_at=at,
            source_version=f"alfred_vintages:{sha * 16}",
        )

    write.insert_tbill_vintage_coverage(
        db, cov(date(2000, 1, 3), datetime(2024, 3, 2, tzinfo=UTC), "a")
    )
    write.insert_tbill_vintage_coverage(
        db, cov(date(1990, 1, 2), datetime(2024, 4, 2, tzinfo=UTC), "b")
    )
    seen = as_of.tbill_vintage_coverage(db, "DGS3MO", datetime(2024, 3, 15, tzinfo=UTC))
    assert seen is not None and seen.earliest_vintage == date(2000, 1, 3)
    assert as_of.tbill_vintage_coverage(db, "DGS3MO", datetime(2024, 3, 1, tzinfo=UTC)) is None
    with pytest.raises(ImmutableConflictError):
        write.insert_tbill_vintage_coverage(
            db, cov(date(1999, 9, 9), datetime(2024, 3, 2, tzinfo=UTC), "a")
        )


# --- run-scoped execution references -------------------------------------------------------------


def ref(run: UUID, symbol: str = "SPY", **kw: Any) -> ExecutionReference:
    base: dict[str, Any] = {
        "run_id": run,
        "symbol_ref": symbol,
        "ref_time": et(2024, 3, 4, 10, 0),
        "mode": RefMode.BACKTEST,
        "session_date": date(2024, 3, 4),
        "available_at": datetime(2024, 3, 4, 16, 0, tzinfo=UTC),
        "source_version": "backtest_reference_v1",
        "status": RefStatus.RESOLVED,
        "price": 512.5,
        "source": ReferenceSource.SIP_LAST,
        "trade_time": et(2024, 3, 4, 9, 59, 58),
        "trade_tape": Tape.A,
        "trade_conditions": ("@", "F"),
        "trade_id": "77",
    }
    return ExecutionReference(**{**base, **kw})


def test_references_are_insert_only_and_idempotent(db: Connection) -> None:
    run = make_run(db)
    assert write.insert_execution_references(db, [ref(run)]) == 1
    assert write.insert_execution_references(db, [ref(run)]) == 0
    with pytest.raises(DBAPIError, match="insert-only"), db.begin_nested():
        db.execute(text("UPDATE execution_references SET price = 1"))
    with pytest.raises(DBAPIError, match="insert-only"), db.begin_nested():
        db.execute(text("DELETE FROM execution_references"))


def test_benchmark_period_references_are_cutoff_bounded_and_immutable(db: Connection) -> None:
    run = make_run(db)
    base = ref(run, session_date=date(2024, 3, 11), ref_time=et(2024, 3, 11, 10, 0))
    endpoint = BenchmarkPeriodReference(
        **{
            **base.model_dump(),
            "available_at": datetime(2024, 3, 11, 16, 0, tzinfo=UTC),
        },
        period_start=date(2024, 3, 4),
    )
    assert write.insert_benchmark_period_references(db, [endpoint]) == 1
    assert write.insert_benchmark_period_references(db, [endpoint]) == 0
    assert as_of.benchmark_period_references(
        db, run, as_of=datetime(2024, 3, 11, 17, 0, tzinfo=UTC)
    ) == [endpoint]
    assert (
        as_of.benchmark_period_references(db, run, as_of=datetime(2024, 3, 11, 15, 0, tzinfo=UTC))
        == []
    )
    with pytest.raises(DBAPIError, match="insert-only"), db.begin_nested():
        db.execute(
            text("UPDATE benchmark_period_references SET price = 1 WHERE run_id=:run"),
            {"run": run},
        )
    conflicting = endpoint.model_copy(update={"price": 513.0})
    with pytest.raises(ImmutableConflictError):
        write.insert_benchmark_period_references(db, [conflicting])


def test_exact_semantic_replay_does_not_false_conflict_after_a_round_trip(db: Connection) -> None:
    run = make_run(db)
    write.insert_execution_references(db, [ref(run)])
    eastern = ref(
        run, trade_time=et(2024, 3, 4, 9, 59, 58).astimezone(UTC), ref_time=et(2024, 3, 4, 10, 0)
    )
    assert write.insert_execution_references(db, [eastern]) == 0  # same instants, other zone
    later = ref(run, available_at=datetime(2024, 3, 9, tzinfo=UTC))
    assert write.insert_execution_references(db, [later]) == 0  # only write time differs


@pytest.mark.parametrize(
    ("change", "column"),
    [
        ({"price": 513.0}, "price"),
        (
            {
                "source": ReferenceSource.IEX_MID,
                "trade_time": None,
                "trade_tape": None,
                "mode": RefMode.LIVE,
            },
            "mode",
        ),
        ({"trade_conditions": ("F", "@")}, "trade_conditions"),
        ({"trade_id": "78"}, "trade_id"),
        ({"ref_time": et(2024, 3, 4, 10, 1)}, "ref_time"),
        (
            {
                "status": RefStatus.UNRESOLVED,
                "reason": RefReason.NO_ELIGIBLE_TRADE,
                "price": None,
                "source": None,
            },
            "status",
        ),
    ],
)
def test_a_contradicting_reference_fails_loudly(
    db: Connection, change: dict[str, Any], column: str
) -> None:
    run = make_run(db)
    write.insert_execution_references(db, [ref(run)])
    with pytest.raises(ImmutableConflictError, match=column):
        write.insert_execution_references(db, [ref(run, **change)])


def test_two_runs_on_the_same_date_keep_distinct_provenance(db: Connection) -> None:
    a, b = make_run(db), make_run(db)
    write.insert_execution_references(db, [ref(a, price=500.0, trade_id="1")])
    write.insert_execution_references(db, [ref(b, price=501.0, trade_id="2")])
    got_a = as_of.execution_references(db, a)
    got_b = as_of.execution_references(db, b)
    assert (got_a[0].price, got_a[0].trade_id) == (500.0, "1")
    assert (got_b[0].price, got_b[0].trade_id) == (501.0, "2")
    assert got_a[0].session_date == got_b[0].session_date  # same date, separate rows


def test_unresolved_references_are_durable_and_the_database_refuses_a_daily_price(
    db: Connection,
) -> None:
    run = make_run(db)
    write.insert_execution_references(
        db,
        [
            ref(
                run,
                "AAA",
                status=RefStatus.UNRESOLVED,
                reason=RefReason.SIP_ENTITLEMENT,
                price=None,
                source=None,
                trade_time=None,
                trade_tape=None,
                trade_conditions=(),
            ),
            ref(
                run,
                "BBB",
                status=RefStatus.UNRESOLVED,
                reason=RefReason.CALENDAR_UNCOVERED,
                price=None,
                source=None,
                trade_time=None,
                trade_tape=None,
                trade_conditions=(),
                ref_time=None,
                session_date=None,
            ),
        ],
    )
    got = {r.symbol_ref: r for r in as_of.execution_references(db, run)}
    assert got["AAA"].reason is RefReason.SIP_ENTITLEMENT and got["AAA"].price is None
    assert got["BBB"].ref_time is None
    with pytest.raises(IntegrityError), db.begin_nested():  # a resolved row without a price
        db.execute(
            text(
                "INSERT INTO execution_references (run_id, symbol_ref, ref_time, mode, status, "
                "session_date, available_at, source_version) VALUES (:r, 'X', now(), 'live', "
                "'resolved', '2024-03-04', now(), 'v')"
            ),
            {"r": run},
        )


def test_reference_reads_enforce_available_at(db: Connection) -> None:
    run = make_run(db)
    write.insert_execution_references(db, [ref(run)])
    assert as_of.execution_references(db, run, as_of=datetime(2024, 3, 4, 15, 59, tzinfo=UTC)) == []
    assert (
        len(as_of.execution_references(db, run, as_of=datetime(2024, 3, 4, 16, 0, tzinfo=UTC))) == 1
    )


# --- the halt transaction and its pending request ------------------------------------------------


def event(run: UUID, trigger: KillTrigger = KillTrigger.DAILY_LOSS) -> KillSwitchEvent:
    return KillSwitchEvent(
        run_id=run,
        triggered_at=datetime(2024, 3, 5, 15, 30, tzinfo=UTC),
        trigger=trigger,
        daily_loss=0.04,
    )


def test_record_halt_commits_event_partial_and_pending_request_together(db: Connection) -> None:
    run = make_run(db)
    assert write.record_halt(db, event(run)) is True
    status: Any = db.execute(
        text("SELECT status FROM runs WHERE run_id = :r"), {"r": run}
    ).scalar_one()
    assert status == RunStatus.PARTIAL.value
    assert db.execute(text("SELECT count(*) FROM kill_switch_events")).scalar_one() == 1
    (req,) = as_of.halt_reference_requests(db, run)
    assert req.trigger is KillTrigger.DAILY_LOSS and req.tau == datetime(
        2024, 3, 5, 15, 30, tzinfo=UTC
    )
    assert [r.run_id for r in as_of.pending_halt_requests(db)] == [run]


def test_halt_replay_is_a_noop_for_the_request(db: Connection) -> None:
    run = make_run(db)
    write.record_halt(db, event(run))
    write.record_halt(db, event(run))
    assert len(as_of.halt_reference_requests(db, run)) == 1


def test_a_rolled_back_halt_leaves_none_of_the_three_effects(
    pg_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pg_engine.begin() as conn:
        run = make_run(conn)

    def boom(_: Any) -> str:
        raise RuntimeError("fault after the event and request inserts")

    monkeypatch.setattr(
        write, "halt_reason", boom
    )  # reached by the PARTIAL update, after both inserts
    with pytest.raises(RuntimeError), pg_engine.begin() as conn:
        write.record_halt(conn, event(run))
    with pg_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM kill_switch_events WHERE run_id=:r"), {"r": run}
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                text("SELECT count(*) FROM halt_reference_requests WHERE run_id=:r"), {"r": run}
            ).scalar_one()
            == 0
        )
        status: Any = conn.execute(
            text("SELECT status FROM runs WHERE run_id=:r"), {"r": run}
        ).scalar_one()
        assert status == RunStatus.COMMITTED.value
        conn.execute(text("DELETE FROM runs WHERE run_id=:r"), {"r": run})
        conn.commit()


def test_the_request_table_can_only_hold_a_pending_row_without_symbols(db: Connection) -> None:
    run = make_run(db)
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(
            text(
                "INSERT INTO halt_reference_requests (run_id, trigger, tau, status, requested_at, "
                "source_version) VALUES (:r, 'manual', now(), 'symbols_resolved', now(), 'v')"
            ),
            {"r": run},
        )
    from sqlalchemy import inspect

    cols = {c["name"] for c in inspect(db).get_columns("halt_reference_requests")}
    assert not any("symbol" in c for c in cols if c != "status")


def halt_ref(run: UUID, symbol: str, **kw: Any) -> HaltReference:
    base: dict[str, Any] = {
        "run_id": run,
        "trigger": KillTrigger.DAILY_LOSS,
        "symbol_ref": symbol,
        "observed_at": datetime(2024, 3, 5, 15, 31, tzinfo=UTC),
        "lag_seconds": 60.0,
        "symbol_set_source": "s",
        "symbol_set_version": "v",
        "available_at": datetime(2024, 3, 5, 15, 32, tzinfo=UTC),
        "source_version": "halt_reference_v1",
        "status": RefStatus.RESOLVED,
        "price": 10.0,
        "source": ReferenceSource.IEX_MID,
    }
    return HaltReference(**{**base, **kw})


def test_pending_requests_clear_only_when_every_symbol_has_a_reference(db: Connection) -> None:
    run = make_run(db)
    write.record_halt(db, event(run))
    symbols = ("AAA", "SPY")
    write.insert_halt_symbol_set(
        db,
        HaltSymbolSet(
            run_id=run,
            trigger=KillTrigger.DAILY_LOSS,
            symbols=symbols,
            symbol_count=2,
            symbols_sha256=symbols_sha256(symbols),
            source="s",
            source_version="v",
            resolved_at=NOW,
        ),
    )
    assert len(as_of.pending_halt_requests(db)) == 1  # set known, no marks yet
    write.insert_halt_references(db, [halt_ref(run, "AAA")])
    assert len(as_of.pending_halt_requests(db)) == 1
    write.insert_halt_references(
        db,
        [
            halt_ref(
                run,
                "SPY",
                status=RefStatus.UNRESOLVED,
                reason=RefReason.NO_ELIGIBLE_TRADE,
                price=None,
                source=None,
            )
        ],
    )
    assert as_of.pending_halt_requests(db) == []
    with pytest.raises(ImmutableConflictError, match="price"):
        write.insert_halt_references(db, [halt_ref(run, "AAA", price=11.0)])


# --- every new immutable writer uses the conflict-checking helper --------------------------------


def test_no_new_writer_uses_the_silent_do_nothing_path() -> None:
    source = Path(write.__file__).read_text(encoding="utf-8")
    tail = source[
        source.index("# --- market-data foundations (P6.3)") : source.index("def persist_outcomes(")
    ]
    assert "_insert(" not in tail.replace("_insert_immutable(", "") and "_insert_rows(" not in tail
    names = re.findall(r"def (insert_\w+)\(", tail)
    assert {
        "insert_calendar_range",
        "insert_tbill_rates",
        "insert_tbill_vintage_coverage",
        "insert_execution_references",
        "insert_halt_reference_requests",
        "insert_halt_symbol_set",
        "insert_halt_references",
    } <= set(names)
    assert "on_conflict_do_nothing" in tail  # only inside the checked helper
    assert tail.count("on_conflict_do_nothing") == 1
