"""Market-data foundations ingestion (P6.3): calendar, benchmark bars, DGS3MO vintages.

Writes go through ``store.write`` (the ingest-side path, like `FeedIngestor`), one nested
transaction per unit so one failure never hides the others. New tables use the conflict-checking
writers: exact replay is a no-op and a contradicting replay raises.

Benchmark bars reuse the existing raw SIP daily-bar path (`AlpacaBars`, ``adjustment=raw``); there
is no second price semantics for SPY and the sector ETFs.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from sqlalchemy import Engine

from contracts.enums import SecurityKind
from contracts.market_data import CalendarCoverage, TradingSession, calendar_hash
from contracts.models import MarketSession
from ingest.alpaca import AlpacaBars
from ingest.fred import SERIES, AlfredClient, establish_vintage_coverage
from store import write

CALENDAR_SOURCE = "alpaca_calendar"
BENCHMARK_NAMES = {"SPY": "SPDR S&P 500 ETF Trust"}


class SessionSource(Protocol):
    def sessions(self, start: date, end: date) -> list[MarketSession]: ...


def benchmark_tickers(sector_etfs: Sequence[str]) -> list[str]:
    """SPY plus the configured sector ETFs, sorted and de-duplicated."""
    return sorted({"SPY", *sector_etfs})


def to_trading_sessions(
    sessions: Sequence[MarketSession], *, fetched_at: datetime
) -> list[TradingSession]:
    """Broker sessions as versioned calendar rows; a changed session is a new ``source_version``."""
    out: list[TradingSession] = []
    for s in sessions:
        raw = f"{s.session_date.isoformat()}|{s.opens_at.astimezone(UTC).isoformat()}|"
        raw += s.closes_at.astimezone(UTC).isoformat()
        out.append(
            TradingSession(
                event_time=s.opens_at,
                available_at=fetched_at,
                source_version=f"{CALENDAR_SOURCE}:{hashlib.sha256(raw.encode()).hexdigest()[:8]}",
                session_date=s.session_date,
                open_at=s.opens_at,
                close_at=s.closes_at,
            )
        )
    return out


def calendar_coverage_for(
    sessions: Sequence[TradingSession], start: date, end: date, *, fetched_at: datetime
) -> CalendarCoverage:
    digest = calendar_hash(sessions)
    return CalendarCoverage(
        source=CALENDAR_SOURCE,
        range_start=start,
        range_end=end,
        available_at=fetched_at,
        session_count=len(sessions),
        sessions_sha256=digest,
        source_version=f"calendar_coverage:{digest[:16]}",
    )


@dataclass(frozen=True)
class ReferenceDataIngestor:
    engine: Engine
    calendar: SessionSource
    bars: AlpacaBars
    alfred: AlfredClient | None
    benchmarks: Sequence[str]
    forward_days: int = 120
    back_days: int = 800
    bar_lookback_days: int = 7

    def run_calendar(self, now: datetime) -> int:
        """Scheduled sync: a fixed date window around today (no weekday inference)."""
        today = now.astimezone(UTC).date()
        return self.sync_calendar(
            today - timedelta(days=self.back_days),
            today + timedelta(days=self.forward_days),
            now=now,
        )

    def run_benchmarks(self, now: datetime) -> int:
        return self.ingest_benchmarks(now=now, lookback_days=self.bar_lookback_days)

    def run_dgs3mo(self, now: datetime) -> int:
        return self.sync_dgs3mo(now=now)

    def sync_calendar(self, start: date, end: date, *, now: datetime) -> int:
        """Fetch ``[start, end]`` completely, then write its sessions and coverage atomically.

        A failed or partial fetch raises before anything is written, so no range is ever marked
        covered that was not read in full. A day with no session inside the range is a closed day.
        """
        if end < start:
            raise ValueError("calendar range ends before it starts")
        fetched = self.calendar.sessions(start, end)
        rows = to_trading_sessions(fetched, fetched_at=now)
        if any(not start <= r.session_date <= end for r in rows):
            raise ValueError("the broker returned a session outside the requested range")
        coverage = calendar_coverage_for(rows, start, end, fetched_at=now)
        with self.engine.begin() as conn:
            return write.insert_calendar_range(conn, rows, coverage)

    def ensure_instruments(self) -> dict[str, int]:
        with self.engine.begin() as conn:
            return {
                ticker: write.ensure_reference_instrument(
                    conn,
                    ticker=ticker,
                    name=BENCHMARK_NAMES.get(ticker, f"{ticker} sector ETF"),
                    kind=SecurityKind.ETF,
                )
                for ticker in self.benchmarks
            }

    def ingest_benchmarks(self, *, now: datetime, lookback_days: int) -> int:
        """Raw SIP daily bars for SPY and the sector ETFs through the existing bar path."""
        ids = self.ensure_instruments()
        end = now.astimezone(UTC).date()
        start = end - timedelta(days=lookback_days)
        errors: list[str] = []
        rows = 0
        for ticker, sid in sorted(ids.items()):
            try:
                bars = self.bars.history(ticker, sid, start, end, now=now)
                with self.engine.begin() as conn:
                    rows += write.insert_price_bars(conn, bars)
            except Exception as exc:  # one benchmark failing must not hide the rest
                errors.append(f"{ticker}: {exc!r}")
        if errors:
            raise RuntimeError("; ".join(errors))
        return rows

    def sync_dgs3mo(self, *, now: datetime) -> int:
        """Every ALFRED vintage of DGS3MO plus the coverage derived from the vintage list."""
        if self.alfred is None:
            raise RuntimeError("no FRED_API_KEY configured for the DGS3MO job")
        dates = self.alfred.vintage_dates(SERIES)
        observations = self.alfred.observations(SERIES)
        coverage = establish_vintage_coverage(SERIES, dates, now=now)
        with self.engine.begin() as conn:
            n = write.insert_tbill_rates(conn, observations)
            write.insert_tbill_vintage_coverage(conn, coverage)
        return n
