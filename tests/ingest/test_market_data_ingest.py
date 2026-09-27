"""P6.3 adapters over synthetic recorded-shape fixtures: SIP trades, ALFRED, calendar, ETF bars."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, text

from contracts.enums import ReferenceSource, RefReason, RefStatus, Tape
from contracts.errors import ImmutableConflictError
from contracts.models import MarketSession
from evaluation.calendar_rules import SessionStatus
from execution.reference_rules import resolve_backtest
from execution.trade_conditions import MAP_PATH, load_provider_map
from ingest import alpaca_trades
from ingest.alpaca import AlpacaBars
from ingest.alpaca_trades import (
    AlpacaTrades,
    IncompleteTradesError,
    SipEntitlementError,
    parse_trade_time,
)
from ingest.condition_probe import compare_conditions, main, propose_map
from ingest.fred import AlfredClient, FredError, establish_vintage_coverage
from ingest.reference_data import ReferenceDataIngestor, benchmark_tickers
from store import as_of
from tests.market_data_support import et, session_for, validated_map_for_tests

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "market_data"
NOW = datetime(2024, 6, 30, 12, 0, tzinfo=UTC)


def fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIX / name).read_text(encoding="utf-8"))
    return data


# --- historical SIP trades ----------------------------------------------------------------------


def trades_transport(seen: list[httpx.Request], *, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if status != 200:
            return httpx.Response(status, json={"message": "no"})
        if "/trades" in request.url.path:
            token = request.url.params.get("page_token")
            return httpx.Response(
                200, json=fixture("sip_trades_page2.json" if token else "sip_trades_page1.json")
            )
        return httpx.Response(200, json=fixture("conditions_trade_C.json"))

    return httpx.MockTransport(handler)


START, END = et(2024, 3, 4, 9, 30), et(2024, 3, 4, 10, 0)


def test_trades_request_explicit_sip_ascending_and_follow_pages() -> None:
    seen: list[httpx.Request] = []
    client = AlpacaTrades(trades_transport(seen), key_id="k", secret="s")
    trades = client.historical_trades("SPY", START, END)
    assert [t.trade_id for t in trades] == ["1", "2", "3", "4"]
    first = seen[0].url.params
    assert first["feed"] == "sip" and first["sort"] == "asc"
    assert seen[1].url.params["page_token"] == "tok2"
    assert all("/bars" not in r.url.path for r in seen)  # never a daily-bar substitute
    assert trades[0].tape is Tape.B and trades[1].conditions == ("@", "F")


def test_nanosecond_timestamps_are_kept_to_microseconds() -> None:
    assert parse_trade_time("2024-03-04T14:31:00.123456789Z") == datetime(
        2024, 3, 4, 14, 31, 0, 123456, tzinfo=UTC
    )
    with pytest.raises(ValueError):
        parse_trade_time("2024-03-04T14:31:00")


def test_the_recorded_sequence_resolves_the_latest_eligible_trade() -> None:
    client = AlpacaTrades(trades_transport([]), key_id="k", secret="s")
    trades = client.historical_trades("SPY", START, END)
    obs = resolve_backtest(
        "SPY", trades, session_for(date(2024, 3, 4)), END, validated_map_for_tests()
    )
    assert obs.status is RefStatus.RESOLVED and obs.price == 510.9
    assert obs.source is ReferenceSource.SIP_LAST


@pytest.mark.parametrize("status", [401, 403, 400, 422])
def test_refusal_or_unsupported_feed_raises_the_fail_closed_error(status: int) -> None:
    client = AlpacaTrades(trades_transport([], status=status), key_id="k", secret="s")
    with pytest.raises(SipEntitlementError):
        client.historical_trades("SPY", START, END)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_statuses_stay_retryable_http_errors(status: int) -> None:
    client = AlpacaTrades(trades_transport([], status=status), key_id="k", secret="s")
    with pytest.raises(httpx.HTTPStatusError):
        client.historical_trades("SPY", START, END)


def test_a_repeating_or_endless_page_chain_is_incomplete_not_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def loop(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={**fixture("sip_trades_page1.json"), "next_page_token": "same"}
        )

    with pytest.raises(IncompleteTradesError):
        AlpacaTrades(httpx.MockTransport(loop), key_id="k", secret="s").historical_trades(
            "SPY", START, END
        )

    counter = iter(range(1000))

    def endless(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={**fixture("sip_trades_page1.json"), "next_page_token": str(next(counter))}
        )

    monkeypatch.setattr(alpaca_trades, "MAX_PAGES", 3)
    with pytest.raises(IncompleteTradesError):
        AlpacaTrades(httpx.MockTransport(endless), key_id="k", secret="s").historical_trades(
            "SPY", START, END
        )


def test_metadata_is_requested_per_tape() -> None:
    seen: list[httpx.Request] = []
    meta = AlpacaTrades(trades_transport(seen), key_id="k", secret="s").conditions(Tape.C)
    assert meta["@"] == "Regular Sale" and seen[0].url.params["tape"] == "C"


# --- the credentialed probe never validates ------------------------------------------------------


def live_meta() -> dict[Tape, dict[str, str]]:
    return {
        Tape.A: fixture("conditions_trade_A.json"),
        Tape.B: fixture("conditions_trade_B.json"),
        Tape.C: fixture("conditions_trade_C.json"),
    }


def test_probe_reports_drift_in_both_directions_and_fails() -> None:
    report = compare_conditions(load_provider_map(), live_meta())
    kinds = {f.kind for f in report.findings}
    assert "missing_live_code" in kinds  # the map expects codes the recorded metadata lacks
    assert not report.ok


def test_probe_flags_name_mismatches_and_unknown_live_codes() -> None:
    live = {Tape.C: {"@": "Something Else", "?": "Mystery"}}
    report = compare_conditions(load_provider_map(), live)
    found = {(f.code, f.kind) for f in report.findings if f.tape is Tape.C}
    assert ("@", "name_mismatch") in found and ("?", "unmapped_live_code") in found


def test_probe_proposal_marks_only_exact_matches_and_leaves_the_repo_map_alone() -> None:
    before = hashlib.sha256(MAP_PATH.read_bytes()).hexdigest()
    report = compare_conditions(load_provider_map(), live_meta())
    proposed = propose_map(report)
    assert proposed.startswith("# PROPOSED") and "validated: true" in proposed
    assert hashlib.sha256(MAP_PATH.read_bytes()).hexdigest() == before
    assert all(not e.validated for e in load_provider_map().entries)  # nothing auto-promoted


def test_probe_without_credentials_skips_and_refuses_to_overwrite_the_map(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    assert main([]) == 0 and "skipped" in capsys.readouterr().out
    monkeypatch.setenv("ALPACA_API_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_API_SECRET", "s")
    assert main(["--out", str(MAP_PATH)]) == 2
    assert all(not e.validated for e in load_provider_map().entries)


# --- ALFRED vintages ------------------------------------------------------------------------------


def alfred_transport(seen: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.path.endswith("/vintagedates"):
            return httpx.Response(200, json=fixture("alfred_vintagedates.json"))
        return httpx.Response(200, json=fixture("alfred_observations.json"))

    return httpx.MockTransport(handler)


def test_alfred_rows_keep_observation_and_vintage_dates_and_skip_missing_values() -> None:
    seen: list[httpx.Request] = []
    client = AlfredClient(alfred_transport(seen), api_key="key")
    rows = client.observations()
    assert len(rows) == 4  # the "." value is absent, never zero
    by = {(r.observation_date, r.vintage_date): r.yield_pct for r in rows}
    assert by[(date(2024, 3, 5), date(2024, 3, 6))] == 5.21  # the first vintage
    assert by[(date(2024, 3, 5), date(2024, 3, 7))] == 5.22  # the revision is a separate row
    assert (date(2024, 3, 6), date(2024, 3, 7)) not in by
    assert seen[0].url.params["realtime_start"] == "1776-07-04"  # every vintage, not just current
    assert seen[0].url.params["realtime_end"] == "9999-12-31"


def test_vintage_coverage_is_derived_from_the_fetched_list() -> None:
    dates = AlfredClient(alfred_transport(), api_key="k").vintage_dates()
    cov = establish_vintage_coverage("DGS3MO", dates, now=NOW)
    assert (cov.earliest_vintage, cov.latest_vintage, cov.vintage_count) == (
        date(2024, 3, 5),
        date(2024, 3, 7),
        3,
    )
    assert cov.source_version.startswith("alfred_vintages:")
    with pytest.raises(FredError):
        establish_vintage_coverage("DGS3MO", [], now=NOW)


def test_a_malformed_response_is_an_error_not_an_empty_vintage_list() -> None:
    bad = httpx.MockTransport(lambda r: httpx.Response(200, json={"nope": 1}))
    with pytest.raises(FredError):
        AlfredClient(bad, api_key="k").vintage_dates()
    with pytest.raises(FredError):
        AlfredClient(bad, api_key="k").observations()


# --- ingestion into the store ---------------------------------------------------------------------


class Broker:
    def __init__(self, sessions: list[MarketSession], *, fail: bool = False) -> None:
        self.sessions_, self.fail = sessions, fail

    def sessions(self, start: date, end: date) -> list[MarketSession]:
        if self.fail:
            raise RuntimeError("broker down")
        return [s for s in self.sessions_ if start <= s.session_date <= end]


def march() -> list[MarketSession]:
    out = []
    for d in range(4, 9):
        s = session_for(date(2024, 3, d))
        out.append(
            MarketSession(session_date=s.session_date, opens_at=s.open_at, closes_at=s.close_at)
        )
    return out


@pytest.fixture
def engine(pg_engine: Engine) -> Iterator[Engine]:
    def wipe() -> None:
        with pg_engine.begin() as c:
            c.execute(
                text(
                    "TRUNCATE trading_calendar, calendar_coverage, tbill_rates, "
                    "tbill_vintage_coverage, price_bars, securities RESTART IDENTITY CASCADE"
                )
            )

    wipe()
    yield pg_engine
    wipe()


def ingestor(engine: Engine, broker: Broker, **kw: Any) -> ReferenceDataIngestor:
    return ReferenceDataIngestor(
        engine,
        calendar=broker,
        bars=kw.pop("bars", AlpacaBars()),
        alfred=kw.pop("alfred", None),
        benchmarks=benchmark_tickers(["XLK", "XLE"]),
        **kw,
    )


def test_calendar_sync_writes_sessions_and_coverage_and_replays_exactly(engine: Engine) -> None:
    ing = ingestor(engine, Broker(march()))
    assert ing.sync_calendar(date(2024, 3, 1), date(2024, 3, 10), now=NOW) == 5
    with engine.connect() as c:
        cal = as_of.trading_calendar(c, NOW)
    assert cal.status(date(2024, 3, 2)) is SessionStatus.CLOSED  # a Saturday inside coverage
    assert cal.status(date(2024, 3, 11)) is SessionStatus.UNCOVERED
    assert ing.sync_calendar(date(2024, 3, 1), date(2024, 3, 10), now=NOW + timedelta(days=1)) == 0
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM calendar_coverage")).scalar_one() == 1


def test_a_failed_fetch_marks_nothing_covered(engine: Engine) -> None:
    with pytest.raises(RuntimeError):
        ingestor(engine, Broker([], fail=True)).sync_calendar(
            date(2024, 3, 1), date(2024, 3, 10), now=NOW
        )
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM calendar_coverage")).scalar_one() == 0
        assert as_of.trading_calendar(c, NOW).status(date(2024, 3, 5)) is SessionStatus.UNCOVERED


def test_a_session_outside_the_requested_range_is_refused(engine: Engine) -> None:
    class Wide(Broker):
        def sessions(self, start: date, end: date) -> list[MarketSession]:
            return self.sessions_  # ignores the window

    with pytest.raises(ValueError, match="outside"):
        ingestor(engine, Wide(march())).sync_calendar(date(2024, 3, 1), date(2024, 3, 6), now=NOW)


def test_a_revised_session_becomes_a_new_version_with_new_coverage(engine: Engine) -> None:
    ing = ingestor(engine, Broker(march()))
    ing.sync_calendar(date(2024, 3, 1), date(2024, 3, 10), now=NOW)
    revised = march()
    revised[0] = MarketSession(
        session_date=date(2024, 3, 4),
        opens_at=et(2024, 3, 4, 9, 30),
        closes_at=et(2024, 3, 4, 13, 0),
    )
    later = NOW + timedelta(days=2)
    ingestor(engine, Broker(revised)).sync_calendar(date(2024, 3, 1), date(2024, 3, 10), now=later)
    with engine.connect() as c:
        assert as_of.trading_calendar(c, later).session(date(2024, 3, 4)).close_at == et(
            2024, 3, 4, 13, 0
        )
        old = as_of.trading_calendar(c, NOW).session(date(2024, 3, 4)).close_at
        assert old == et(2024, 3, 4, 16, 0)
        assert c.execute(text("SELECT count(*) FROM calendar_coverage")).scalar_one() == 2


class FakeBars(AlpacaBars):
    """The real ``history`` path over a mock transport: parse_bars and raw semantics reused."""

    def __init__(self, fail_for: str | None = None) -> None:
        seen: list[httpx.Request] = []
        self.seen = seen
        self.fail_for = fail_for

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            symbol = request.url.path.split("/")[3]
            if symbol == self.fail_for:
                return httpx.Response(500)
            return httpx.Response(
                200,
                json={
                    "bars": [
                        {
                            "t": "2024-03-04T05:00:00Z",
                            "o": 100.0,
                            "h": 101.0,
                            "l": 99.0,
                            "c": 100.5,
                            "v": 1000,
                        }
                    ],
                    "next_page_token": None,
                },
            )

        super().__init__(httpx.MockTransport(handler), key_id="k", secret="s")


def test_benchmark_bars_reuse_the_raw_sip_bar_path(engine: Engine) -> None:
    bars = FakeBars()
    ing = ingestor(engine, Broker([]), bars=bars)
    assert ing.ingest_benchmarks(now=NOW, lookback_days=7) == 3  # SPY, XLE, XLK
    assert all(
        r.url.params["adjustment"] == "raw" and r.url.params["feed"] == "sip" for r in bars.seen
    )
    with engine.connect() as c:
        etfs = {i.ticker for i in as_of.reference_instruments(c)}
        assert etfs == {"SPY", "XLE", "XLK"} and as_of.securities(c) == []  # never equities
        rows = c.execute(text("SELECT feed, source_version FROM price_bars")).all()
    assert {tuple(r) for r in rows} == {("sip", "sip")}
    assert ing.ingest_benchmarks(now=NOW, lookback_days=7) == 0  # idempotent


def test_one_failing_benchmark_does_not_hide_the_others(engine: Engine) -> None:
    ing = ingestor(engine, Broker([]), bars=FakeBars(fail_for="XLE"))
    with pytest.raises(RuntimeError, match="XLE"):
        ing.ingest_benchmarks(now=NOW, lookback_days=7)
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM price_bars")).scalar_one() == 2


def test_dgs3mo_sync_stores_every_vintage_and_the_derived_coverage(engine: Engine) -> None:
    ing = ingestor(engine, Broker([]), alfred=AlfredClient(alfred_transport(), api_key="k"))
    assert ing.sync_dgs3mo(now=NOW) == 4
    assert ing.sync_dgs3mo(now=NOW + timedelta(days=1)) == 0  # replay: exact no-op
    with engine.connect() as c:
        rows = as_of.tbill_rates(c, "DGS3MO", date(2024, 3, 8))
        cov = as_of.tbill_vintage_coverage(c, "DGS3MO", NOW)
    assert len(rows) == 4 and cov is not None and cov.earliest_vintage == date(2024, 3, 5)
    changed = fixture("alfred_observations.json")
    changed["observations"][0]["value"] = "9.99"  # same observation + vintage, different value
    bad = httpx.MockTransport(
        lambda r: (
            httpx.Response(200, json=fixture("alfred_vintagedates.json"))
            if r.url.path.endswith("/vintagedates")
            else httpx.Response(200, json=changed)
        )
    )
    with pytest.raises(ImmutableConflictError, match="yield_pct"):
        ingestor(engine, Broker([]), alfred=AlfredClient(bad, api_key="k")).sync_dgs3mo(now=NOW)


def test_dgs3mo_without_a_key_is_an_explicit_error(engine: Engine) -> None:
    with pytest.raises(RuntimeError, match="FRED_API_KEY"):
        ingestor(engine, Broker([])).sync_dgs3mo(now=NOW)


def test_scheduled_calendar_window_is_dates_around_today_not_weekdays(engine: Engine) -> None:
    seen: list[tuple[date, date]] = []

    class Spy(Broker):
        def sessions(self, start: date, end: date) -> list[MarketSession]:
            seen.append((start, end))
            return []

    ingestor(engine, Spy([]), forward_days=120, back_days=800).run_calendar(NOW)
    ((start, end),) = seen
    assert (end - NOW.date()).days == 120 and (NOW.date() - start).days == 800


def test_reason_enum_has_no_daily_price_reason() -> None:
    assert not {r.value for r in RefReason} & {"daily_open", "daily_close"}
