"""P6.3 contracts: instruments, sessions, references, halt request/symbol set (extra="forbid")."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from contracts.enums import (
    HaltRequestStatus,
    KillTrigger,
    ReferenceSource,
    RefMode,
    RefReason,
    RefStatus,
    SecurityKind,
    Tape,
)
from contracts.market_data import (
    BenchmarkPeriodReference,
    CalendarCoverage,
    ExecutionReference,
    HaltReference,
    HaltReferenceRequest,
    HaltSymbolSet,
    ReferenceInstrument,
    SipTrade,
    TBillObservation,
    TradingSession,
    symbols_sha256,
)

T = datetime(2024, 3, 4, 15, 0, tzinfo=UTC)
RUN = uuid4()


def test_a_reference_instrument_has_no_cik_and_is_never_an_equity() -> None:
    inst = ReferenceInstrument(security_id=1, ticker="SPY", name="SPDR")
    assert inst.kind is SecurityKind.ETF and "cik" not in ReferenceInstrument.model_fields
    with pytest.raises(ValidationError):
        ReferenceInstrument(security_id=1, ticker="SPY", name="x", kind=SecurityKind.EQUITY)
    with pytest.raises(ValidationError):
        ReferenceInstrument(security_id=1, ticker="SPY", name="x", cik=123)  # type: ignore[call-arg]


def test_trading_session_validators() -> None:
    open_at = datetime(2024, 3, 4, 14, 30, tzinfo=UTC)
    kw: dict[str, Any] = {
        "event_time": open_at,
        "available_at": T,
        "source_version": "v",
        "session_date": date(2024, 3, 4),
        "open_at": open_at,
        "close_at": datetime(2024, 3, 4, 21, 0, tzinfo=UTC),
    }
    TradingSession(**kw)
    with pytest.raises(ValidationError):
        TradingSession(**{**kw, "close_at": open_at})
    with pytest.raises(ValidationError):
        TradingSession(**{**kw, "event_time": T})


def test_coverage_range_must_be_ordered() -> None:
    kw: dict[str, Any] = {
        "source": "s",
        "range_start": date(2024, 3, 5),
        "range_end": date(2024, 3, 4),
        "available_at": T,
        "session_count": 0,
        "sessions_sha256": "0" * 64,
        "source_version": "v",
    }
    with pytest.raises(ValidationError):
        CalendarCoverage(**kw)


def test_tbill_observation_is_date_granular_with_no_available_at() -> None:
    assert "available_at" not in TBillObservation.model_fields
    assert "vintage_date" in TBillObservation.model_fields
    with pytest.raises(ValidationError):
        TBillObservation(
            series="DGS3MO",
            observation_date=date(2024, 3, 4),
            yield_pct=5.0,
            vintage_date=date(2024, 3, 1),
            source_version="v",
        )


def _ref(**kw: Any) -> ExecutionReference:
    base: dict[str, Any] = {
        "run_id": RUN,
        "symbol_ref": "SPY",
        "ref_time": T,
        "mode": RefMode.BACKTEST,
        "session_date": date(2024, 3, 4),
        "available_at": T,
        "source_version": "v",
        "status": RefStatus.RESOLVED,
        "price": 100.0,
        "source": ReferenceSource.SIP_LAST,
        "trade_time": T,
        "trade_tape": Tape.C,
    }
    return ExecutionReference(**{**base, **kw})


def test_a_resolved_reference_has_a_price_and_source_and_no_reason() -> None:
    _ref()
    for bad in (
        {"price": None},
        {"source": None},
        {"reason": RefReason.NO_ELIGIBLE_TRADE},
        {"price": 0.0},
    ):
        with pytest.raises(ValidationError):
            _ref(**bad)


def test_an_unresolved_reference_has_a_reason_and_never_a_price() -> None:
    _ref(status=RefStatus.UNRESOLVED, reason=RefReason.NO_ELIGIBLE_TRADE, price=None, source=None)
    with pytest.raises(ValidationError):
        _ref(status=RefStatus.UNRESOLVED, price=None, source=None)  # no reason
    with pytest.raises(ValidationError):
        _ref(status=RefStatus.UNRESOLVED, reason=RefReason.NO_ELIGIBLE_TRADE)  # still has a price


def test_no_reason_or_source_can_name_a_daily_price() -> None:
    assert {r.value for r in RefReason}.isdisjoint({"daily_open", "daily_close", "close", "open"})
    assert {s.value for s in ReferenceSource} == {"iex_mid", "sip_last"}


def test_a_backtest_reference_is_a_sip_trade_with_time_and_tape() -> None:
    with pytest.raises(ValidationError):
        _ref(source=ReferenceSource.IEX_MID)
    with pytest.raises(ValidationError):
        _ref(trade_time=None)
    _ref(mode=RefMode.LIVE, source=ReferenceSource.IEX_MID, trade_time=None, trade_tape=None)


def test_benchmark_period_reference_is_same_run_and_ends_after_its_start() -> None:
    start = date(2024, 3, 4)
    endpoint = BenchmarkPeriodReference(
        **_ref(session_date=date(2024, 3, 11)).model_dump(), period_start=start
    )
    assert endpoint.run_id == RUN
    assert endpoint.period_start == start
    with pytest.raises(ValidationError, match="after period_start"):
        BenchmarkPeriodReference(**_ref(session_date=start).model_dump(), period_start=start)


def test_only_calendar_uncovered_may_lack_the_reference_day() -> None:
    kw: dict[str, Any] = {
        "status": RefStatus.UNRESOLVED,
        "price": None,
        "source": None,
        "trade_time": None,
        "trade_tape": None,
        "ref_time": None,
        "session_date": None,
    }
    _ref(reason=RefReason.CALENDAR_UNCOVERED, **kw)
    with pytest.raises(ValidationError):
        _ref(reason=RefReason.NO_ELIGIBLE_TRADE, **kw)
    with pytest.raises(ValidationError):  # a resolved reference always states its day
        _ref(ref_time=None)


def test_halt_request_can_only_be_pending_and_holds_no_symbols() -> None:
    req = HaltReferenceRequest(
        run_id=RUN, trigger=KillTrigger.MANUAL, tau=T, requested_at=T, source_version="v"
    )
    assert req.status is HaltRequestStatus.SYMBOLS_PENDING
    assert not any("symbol" in f for f in HaltReferenceRequest.model_fields if f != "status")
    assert list(HaltRequestStatus) == [HaltRequestStatus.SYMBOLS_PENDING]


def _set(**kw: Any) -> HaltSymbolSet:
    symbols = kw.pop("symbols", ("AAA", "SPY"))
    base: dict[str, Any] = {
        "run_id": RUN,
        "trigger": KillTrigger.DAILY_LOSS,
        "symbols": symbols,
        "symbol_count": len(symbols),
        "symbols_sha256": symbols_sha256(symbols),
        "source": "s",
        "source_version": "v",
        "resolved_at": T,
    }
    return HaltSymbolSet(**{**base, **kw})


def test_halt_symbol_set_is_sorted_unique_nonempty_and_hash_checked() -> None:
    _set()
    with pytest.raises(ValidationError):
        _set(symbols=())
    with pytest.raises(ValidationError):
        _set(symbols=("SPY", "AAA"))
    with pytest.raises(ValidationError):
        _set(symbols=("AAA", "AAA"))
    with pytest.raises(ValidationError):
        _set(symbol_count=5)
    with pytest.raises(ValidationError):
        _set(symbols_sha256="0" * 64)


def test_halt_reference_shares_the_observation_rules() -> None:
    kw: dict[str, Any] = {
        "run_id": RUN,
        "trigger": KillTrigger.MANUAL,
        "symbol_ref": "SPY",
        "observed_at": T,
        "lag_seconds": 1.5,
        "symbol_set_source": "s",
        "symbol_set_version": "v",
        "available_at": T,
        "source_version": "v",
        "status": RefStatus.RESOLVED,
        "price": 10.0,
        "source": ReferenceSource.IEX_MID,
    }
    HaltReference(**kw)
    with pytest.raises(ValidationError):
        HaltReference(**{**kw, "price": None})


def test_sip_trade_keeps_provider_conditions_and_tape() -> None:
    tr = SipTrade(
        symbol="SPY", time=T, price=1.0, size=1, tape=Tape.A, conditions=("@", "F"), trade_id="9"
    )
    assert tr.conditions == ("@", "F")
    with pytest.raises(ValidationError):
        SipTrade(symbol="SPY", time=T, price=1.0, size=1, tape="D", conditions=("@",))  # type: ignore[arg-type]
