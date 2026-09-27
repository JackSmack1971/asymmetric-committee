"""Reference capture (P6.3): run-scoped execution references and the halt-reference sweeper.

* `capture_live` is a polling capture: D0 is the next *stored* session after the run's ``as_of`` and
  the reference time is that session's open plus the configured delay, so a holiday Monday moves it
  to Tuesday and an early or changed session moves it with the calendar. Nothing is hard-coded to a
  weekday or clock time. Missing calendar coverage defers; only after the configured operational
  timeout (`calendar_coverage_wait_minutes`, measured from the run's immutable commitment time) is
  ``calendar_uncovered`` persisted, and that timeout never decides D0 or whether a day was open.
* `build_backtest` resolves the latest eligible historical SIP trade at or before that time.
* `sweep_halt_requests` is the only consumer of halt-reference requests: it reconstructs the symbol
  set from durable state, then resolves each symbol with the shared live resolver (close to the
  trigger) or, later, the first eligible SIP trade after it. Nothing ever substitutes a daily price.

Reads go through ``store.as_of``; run-scoped writes go through `DecisionSink`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from sqlalchemy import Engine

from config.loader import ReferenceDataConfig
from contracts.enums import (
    RefMode,
    RefReason,
    RefStatus,
    RunMode,
    RunStatus,
)
from contracts.market_data import (
    ExecutionReference,
    HaltReference,
    HaltReferenceRequest,
    HaltSymbolSet,
    ReferenceObservation,
    SipTrade,
    TradingSession,
)
from contracts.models import RunRecord
from evaluation.calendar_rules import CalendarCoverageError, TradingCalendar
from execution.halt_symbols import canonical_symbols, reconstruct_symbol_set, symbol_set_version
from execution.reference import SIP_DELAY, QuoteSource
from execution.reference_rules import reconstruct_halt, resolve_backtest, resolve_live
from execution.trade_conditions import ProviderCodeMap
from ingest.alpaca_trades import IncompleteTradesError, SipEntitlementError
from ingest.timeutil import ET
from orchestration.sink import DecisionSink
from store import as_of as point_in_time

log = logging.getLogger(__name__)

LIVE_VERSION = "live_reference_v1"
BACKTEST_VERSION = "backtest_reference_v1"
HALT_VERSION = "halt_reference_v1"
_LIVE_STATUSES = (
    RunStatus.COMMITTED,
    RunStatus.ANCHORED,
    RunStatus.EXECUTED,
    RunStatus.PARTIAL,
)
_RECENT = timedelta(days=21)


class TradesSource(Protocol):
    def historical_trades(self, symbol: str, start: datetime, end: datetime) -> list[SipTrade]: ...


class CaptureState(StrEnum):
    DEFERRED = "deferred"  # nothing written: not yet due, or waiting for coverage/data
    WRITTEN = "written"
    COMPLETE = "complete"  # every required symbol already has a row


@dataclass(frozen=True)
class CaptureResult:
    state: CaptureState
    written: int = 0
    detail: str = ""


def _observed_fields(obs: ReferenceObservation) -> dict[str, object]:
    return obs.model_dump(exclude={"symbol_ref", "ref_time"})


def _unresolved(symbol: str, at: datetime, reason: RefReason) -> ReferenceObservation:
    return ReferenceObservation(
        symbol_ref=symbol, ref_time=at, status=RefStatus.UNRESOLVED, reason=reason
    )


class ReferenceCapture:
    def __init__(
        self,
        engine: Engine,
        sink: DecisionSink,
        cfg: ReferenceDataConfig,
        *,
        delay_minutes: int,
        config_hash: str,
        quotes: QuoteSource,
        trades: TradesSource,
        provider_map: ProviderCodeMap,
    ) -> None:
        self._engine = engine
        self._sink = sink
        self._cfg = cfg
        self._delay = timedelta(minutes=delay_minutes)
        self._config_hash = config_hash
        self._quotes = quotes
        self._trades = trades
        self._map = provider_map

    # --- shared reads --------------------------------------------------------------------------

    def _calendar(self, now: datetime) -> TradingCalendar:
        """The calendar as of ``now`` from one REPEATABLE READ snapshot (sessions + coverage)."""
        with self._engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
            return point_in_time.trading_calendar(conn, now)

    def _symbol_groups(self, run: RunRecord) -> tuple[list[str], list[str], list[str], str]:
        """Book, universe-snapshot and reference-instrument symbols, and the version."""
        book = self._sink.load_book(run.run_id)
        with self._engine.connect() as conn:
            book_ids = [p.security_id for p in book.positions] if book else []
            members = point_in_time.universe(conn, run.as_of)
            ids = sorted({*book_ids, *(m.security_id for m in members)})
            tickers = {s.security_id: s.ticker for s in point_in_time.securities(conn, ids)}
            instruments = [i.ticker for i in point_in_time.reference_instruments(conn)]
        snapshot = max((m.snapshot_date for m in members), default=None)
        version = symbol_set_version(
            universe_snapshot=snapshot.isoformat() if snapshot else None,
            config_hash=self._config_hash,
        )
        return (
            [tickers[i] for i in book_ids if i in tickers],
            [tickers[m.security_id] for m in members if m.security_id in tickers],
            instruments,
            version,
        )

    def _required(self, run: RunRecord) -> tuple[str, ...]:
        """Every symbol any book needs a mark for (the canonical union)."""
        book, universe, instruments, _ = self._symbol_groups(run)
        return canonical_symbols(book, universe, instruments)

    def _missing_references(self, run_id: UUID, symbols: Sequence[str]) -> list[str]:
        with self._engine.connect() as conn:
            have = {r.symbol_ref for r in point_in_time.execution_references(conn, run_id)}
        return [s for s in symbols if s not in have]

    def _reference_day(self, run: RunRecord, now: datetime) -> tuple[TradingSession, datetime]:
        """D0 = the next stored session after ``as_of``; raises when coverage is missing."""
        cal = self._calendar(now)
        d0 = cal.next_session(run.as_of.astimezone(ET).date())
        session = cal.session(d0)
        return session, session.open_at + self._delay

    # --- execution references ------------------------------------------------------------------

    def _uncovered(
        self,
        run: RunRecord,
        mode: RefMode,
        missing: Sequence[str],
        committed_at: datetime,
        now: datetime,
    ) -> CaptureResult:
        """No coverage yet: wait, and only after the operational timeout persist the fact."""
        deadline = committed_at + timedelta(minutes=self._cfg.calendar_coverage_wait_minutes)
        if now < deadline:
            return CaptureResult(CaptureState.DEFERRED, detail="calendar not covered yet")
        rows = [
            ExecutionReference(
                run_id=run.run_id,
                symbol_ref=s,
                ref_time=None,
                mode=mode,
                session_date=None,
                available_at=now,
                source_version=LIVE_VERSION if mode is RefMode.LIVE else BACKTEST_VERSION,
                status=RefStatus.UNRESOLVED,
                reason=RefReason.CALENDAR_UNCOVERED,
            )
            for s in missing
        ]
        n = self._sink.record_execution_references(rows)
        return CaptureResult(CaptureState.WRITTEN, n, "calendar_uncovered")

    def capture_live(self, run_id: UUID, now: datetime) -> CaptureResult:
        run = self._sink.load_run(run_id)
        if run is None or run.mode is not RunMode.LIVE:
            raise ValueError(f"{run_id} is not a stored LIVE run")
        committed_at = self._sink.load_committed_at(run_id)
        if committed_at is None:
            return CaptureResult(CaptureState.DEFERRED, detail="run has no commitment")
        symbols = self._required(run)
        missing = self._missing_references(run_id, symbols)
        if not missing:
            return CaptureResult(CaptureState.COMPLETE)
        try:
            session, ref_time = self._reference_day(run, now)
        except CalendarCoverageError:
            return self._uncovered(run, RefMode.LIVE, missing, committed_at, now)
        if now < ref_time:
            return CaptureResult(CaptureState.DEFERRED, detail="reference time not reached")
        expired = now > ref_time + timedelta(minutes=self._cfg.live_capture_grace_minutes)
        rows: list[ExecutionReference] = []
        for symbol in missing:
            if expired:
                # The §9 rule cannot be observed at D0 open + delay any more, and a quote taken
                # now is not that observation: the window's absence is the durable evidence.
                obs = _unresolved(symbol, ref_time, RefReason.NO_REFERENCE_PRICE)
            else:
                obs = resolve_live(self._quotes, symbol, now)
                if obs.status is RefStatus.UNRESOLVED:
                    continue  # the rule may still resolve within its window
            rows.append(
                ExecutionReference(
                    run_id=run_id,
                    symbol_ref=symbol,
                    ref_time=ref_time,
                    mode=RefMode.LIVE,
                    session_date=session.session_date,
                    available_at=now,
                    source_version=LIVE_VERSION,
                    **_observed_fields(obs),  # type: ignore[arg-type]
                )
            )
        n = self._sink.record_execution_references(rows) if rows else 0
        return CaptureResult(CaptureState.WRITTEN if n else CaptureState.DEFERRED, n)

    def build_backtest(self, run_id: UUID, now: datetime) -> CaptureResult:
        """Latest eligible historical SIP trade at or before D0 open + delay, per symbol."""
        run = self._sink.load_run(run_id)
        if run is None or run.mode is RunMode.LIVE:
            raise ValueError(f"{run_id} is not a stored BACKTEST/ABLATION run")
        committed_at = self._sink.load_committed_at(run_id)
        if committed_at is None:
            return CaptureResult(CaptureState.DEFERRED, detail="run has no commitment")
        symbols = self._required(run)
        missing = self._missing_references(run_id, symbols)
        if not missing:
            return CaptureResult(CaptureState.COMPLETE)
        try:
            session, ref_time = self._reference_day(run, now)
        except CalendarCoverageError:
            return self._uncovered(run, RefMode.BACKTEST, missing, committed_at, now)
        if now < ref_time:
            return CaptureResult(CaptureState.DEFERRED, detail="reference time not reached")
        observations = self._backtest_observations(missing, session, ref_time)
        rows = [
            ExecutionReference(
                run_id=run_id,
                symbol_ref=obs.symbol_ref,
                ref_time=ref_time,
                mode=RefMode.BACKTEST,
                session_date=session.session_date,
                available_at=now,
                source_version=BACKTEST_VERSION,
                **_observed_fields(obs),  # type: ignore[arg-type]
            )
            for obs in observations
        ]
        n = self._sink.record_execution_references(rows)
        return CaptureResult(CaptureState.WRITTEN, n)

    def _backtest_observations(
        self, symbols: Sequence[str], session: TradingSession, ref_time: datetime
    ) -> list[ReferenceObservation]:
        out: list[ReferenceObservation] = []
        blocked: RefReason | None = None
        if not self._map.any_validated():
            blocked = RefReason.PROVIDER_MAPPING_UNVALIDATED
        for symbol in symbols:
            if blocked is not None:  # a systemic refusal decides every remaining symbol alike
                out.append(_unresolved(symbol, ref_time, blocked))
                continue
            try:
                trades = self._trades.historical_trades(symbol, session.open_at, ref_time)
            except SipEntitlementError:
                blocked = RefReason.SIP_ENTITLEMENT
                out.append(_unresolved(symbol, ref_time, blocked))
                continue
            except IncompleteTradesError:
                out.append(_unresolved(symbol, ref_time, RefReason.INCOMPLETE_TRADES))
                continue
            out.append(resolve_backtest(symbol, trades, session, ref_time, self._map))
        return out

    def capture_due(self, now: datetime, *, on_error: Callable[[UUID, Exception], None]) -> int:
        """Poll every recent committed LIVE run once. One failing run never hides the others."""
        runs = self._sink.list_runs(mode=RunMode.LIVE, statuses=_LIVE_STATUSES)
        written = 0
        for run in runs:
            if now - run.as_of > _RECENT:
                continue
            try:
                with self._sink.run_lock(run.run_id, "references") as got:
                    if got:  # another poller holds it: it will write the same rows
                        written += self.capture_live(run.run_id, now).written
            except Exception as exc:
                on_error(run.run_id, exc)
        return written

    # --- halt references -----------------------------------------------------------------------

    def _halt_symbol_set(self, req: HaltReferenceRequest, now: datetime) -> HaltSymbolSet | None:
        with self._engine.connect() as conn:
            existing = point_in_time.halt_symbol_set(conn, req.run_id, req.trigger.value)
        if existing is not None:
            return existing
        run = self._sink.load_run(req.run_id)
        if run is None:
            log.error("halt request for unknown run %s stays pending", req.run_id)
            return None
        book, universe, instruments, version = self._symbol_groups(run)
        symbol_set = reconstruct_symbol_set(
            req.run_id,
            req.trigger,
            book=book,
            universe=universe,
            reference_instruments=instruments,
            version=version,
            resolved_at=now,
        )
        if symbol_set is None:
            log.error(
                "halt symbol reconstruction for %s is empty; request stays pending", req.run_id
            )
            return None
        self._sink.record_halt_symbol_set(symbol_set)
        return symbol_set

    def sweep_halt_requests(
        self, now: datetime, *, on_error: Callable[[UUID, Exception], None]
    ) -> int:
        with self._engine.connect() as conn:
            pending = point_in_time.pending_halt_requests(conn)
        written = 0
        for req in pending:
            try:
                with self._sink.run_lock(req.run_id, f"halt_references:{req.trigger.value}") as got:
                    if got:
                        written += self._resolve_halt(req, now)
            except Exception as exc:
                on_error(req.run_id, exc)
        return written

    def _halt_row(
        self,
        req: HaltReferenceRequest,
        symbol_set: HaltSymbolSet,
        obs: ReferenceObservation,
        now: datetime,
        *,
        observed_at: datetime | None = None,
    ) -> HaltReference:
        seen = observed_at or now
        return HaltReference(
            run_id=req.run_id,
            trigger=req.trigger,
            symbol_ref=obs.symbol_ref,
            observed_at=seen,
            lag_seconds=(seen - req.tau).total_seconds(),
            symbol_set_source=symbol_set.source,
            symbol_set_version=symbol_set.source_version,
            available_at=now,
            source_version=HALT_VERSION,
            **_observed_fields(obs),  # type: ignore[arg-type]
        )

    def _resolve_halt(self, req: HaltReferenceRequest, now: datetime) -> int:
        symbol_set = self._halt_symbol_set(req, now)
        if symbol_set is None:
            return 0
        with self._engine.connect() as conn:
            have = {
                r.symbol_ref
                for r in point_in_time.halt_references(conn, req.run_id, req.trigger.value)
            }
        missing = [s for s in symbol_set.symbols if s not in have]
        if not missing:
            return 0
        rows: list[HaltReference] = []
        if now - req.tau <= timedelta(minutes=self._cfg.halt_live_grace_minutes):
            for symbol in missing:  # closest observation to the trigger: the shared live rule
                obs = resolve_live(self._quotes, symbol, now)
                if obs.status is RefStatus.RESOLVED:
                    rows.append(self._halt_row(req, symbol_set, obs, now))
            return self._sink.record_halt_references(rows) if rows else 0
        rows = self._halt_by_reconstruction(req, symbol_set, missing, now)
        return self._sink.record_halt_references(rows) if rows else 0

    def _halt_by_reconstruction(
        self,
        req: HaltReferenceRequest,
        symbol_set: HaltSymbolSet,
        missing: Sequence[str],
        now: datetime,
    ) -> list[HaltReference]:
        """First eligible SIP trade after the trigger; deferred until the deterministic deadline."""
        try:
            cal = self._calendar(now)
            session = cal.session_closing_after(req.tau, req.tau.astimezone(ET).date())
        except CalendarCoverageError:
            if now < req.requested_at + timedelta(minutes=self._cfg.calendar_coverage_wait_minutes):
                return []
            return [
                self._halt_row(
                    req, symbol_set, _unresolved(s, now, RefReason.CALENDAR_UNCOVERED), now
                )
                for s in missing
            ]
        deadline = session.close_at + SIP_DELAY
        final = now >= deadline
        if now < req.tau + SIP_DELAY:
            return []  # SIP history is not readable yet
        if not self._map.any_validated():
            if not final:
                return []
            reason = RefReason.PROVIDER_MAPPING_UNVALIDATED
            return [
                self._halt_row(req, symbol_set, _unresolved(s, now, reason), now) for s in missing
            ]
        end = min(session.close_at, now - SIP_DELAY)
        rows: list[HaltReference] = []
        for symbol in missing:
            try:
                trades = self._trades.historical_trades(symbol, session.open_at, end)
                obs = reconstruct_halt(symbol, trades, req.tau, [session], self._map)
            except SipEntitlementError:
                obs = _unresolved(symbol, now, RefReason.SIP_ENTITLEMENT)
            except IncompleteTradesError:
                obs = _unresolved(symbol, now, RefReason.INCOMPLETE_TRADES)
            if obs.status is RefStatus.UNRESOLVED and not final:
                continue
            seen = obs.trade_time if obs.status is RefStatus.RESOLVED else None
            rows.append(self._halt_row(req, symbol_set, obs, now, observed_at=seen))
        return rows


__all__ = [
    "BACKTEST_VERSION",
    "HALT_VERSION",
    "LIVE_VERSION",
    "CaptureResult",
    "CaptureState",
    "ReferenceCapture",
    "TradesSource",
]
