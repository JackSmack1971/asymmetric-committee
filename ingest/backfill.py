"""Backfill history, then build month-end universe snapshots.

    python -m ingest.backfill --years 2 --universe config/universe.yaml
    python -m ingest.backfill --days 90 --tickers ALFA,BRVO --end 2024-06-28 \\
        --replay tests/fixtures/http --universe tests/fixtures/universe_smoke.yaml

``--replay DIR`` serves recorded responses (no network); ``--record DIR`` saves real responses for
later replay. Without ``--tickers`` every NYSE/Nasdaq name whose SIC sector is in the universe's
``sectors`` is seeded. Every insert is idempotent, so a backfill can be re-run or resumed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import redis
from sqlalchemy import Connection

from config.loader import UniverseConfig, load_universe
from contracts.enums import FeedName
from ingest import edgar_form4, edgar_submissions, edgar_xbrl
from ingest.alpaca import AlpacaBars
from ingest.edgar_client import (
    EdgarClient,
    Limiter,
    LocalSlidingWindowLimiter,
    RedisSlidingWindowLimiter,
    user_agent_from_env,
)
from ingest.http import RecordingTransport, ReplayTransport
from ingest.news import NewsProvider, provider_from_env
from ingest.timeutil import ET, at_et, session_close
from store.db import engine
from store.write import (
    ensure_security,
    insert_fundamentals,
    insert_insider_txns,
    insert_news,
    insert_price_bars,
    record_feed_run,
    set_listing,
)
from universe.sectors import sector_for_sic
from universe.snapshot import snapshot_month_ends

EXCHANGES = frozenset({"NYSE", "Nasdaq", "NYSE American", "NYSE Arca", "CBOE"})
REPLAY_USER_AGENT = "AsymmetricCommittee/1.0 (replay@localhost)"


@dataclass
class Summary:
    securities: int = 0
    rows: dict[str, int] = field(default_factory=dict)
    snapshot_rows: int = 0
    errors: list[str] = field(default_factory=list)

    def add(self, feed: FeedName, n: int) -> None:
        self.rows[feed.value] = self.rows.get(feed.value, 0) + n


@dataclass
class Sources:
    edgar: EdgarClient
    bars: AlpacaBars
    news: NewsProvider


def build_sources(
    transport: httpx.BaseTransport | None, *, replay: bool, env: dict[str, str]
) -> Sources:
    limiter: Limiter
    if env.get("REDIS_URL"):
        limiter = RedisSlidingWindowLimiter(redis.Redis.from_url(env["REDIS_URL"]))
    elif replay:
        limiter = LocalSlidingWindowLimiter()
    else:
        raise SystemExit("REDIS_URL is required: the EDGAR limit is global across workers (§4.1)")
    ua = env.get("SEC_USER_AGENT") or ("" if not replay else REPLAY_USER_AGENT)
    return Sources(
        edgar=EdgarClient(limiter, user_agent=ua or user_agent_from_env(env), transport=transport),
        bars=AlpacaBars(transport),
        news=provider_from_env(transport, env),
    )


def _feed(
    conn: Connection,
    summary: Summary,
    feed: FeedName,
    ticker: str,
    now: datetime,
    load: Callable[[], tuple[int, datetime | None]],
) -> None:
    """Run one feed for one name inside a savepoint; record the outcome in feed_health."""
    try:
        with conn.begin_nested():
            n, last = load()
        summary.add(feed, n)
        record_feed_run(conn, feed, at=now, rows=n, last_available_at=last)
    except Exception as e:  # keep going; the error is reported and fails the exit code
        msg = f"{ticker} {feed.value}: {type(e).__name__}: {e}"
        summary.errors.append(msg)
        record_feed_run(conn, feed, at=now, error=msg[:2000])
        if os.environ.get("BACKFILL_TRACEBACK"):
            traceback.print_exc()


def _latest(rows: Sequence[Any]) -> datetime | None:
    return max((r.available_at for r in rows), default=None)


def run(
    *,
    start: date,
    end: date,
    cfg: UniverseConfig,
    tickers: Sequence[str] | None,
    conn: Connection,
    sources: Sources,
    now: datetime,
) -> Summary:
    summary = Summary()
    start_dt, end_dt = at_et(start, datetime.min.time()), session_close(end)

    # 1. Seed securities (all first, so news naming several of our names maps to all of them).
    listed = {t.ticker: t for t in edgar_submissions.fetch_tickers(sources.edgar)}
    if tickers:
        missing = sorted(set(tickers) - set(listed))
        if missing:
            raise SystemExit(f"tickers not in SEC's list: {', '.join(missing)}")
        chosen = [listed[t] for t in tickers]
    else:
        chosen = [t for t in listed.values() if t.exchange in EXCHANGES]
    names: list[tuple[str, int, edgar_submissions.Company]] = []
    for t in chosen:
        company = edgar_submissions.fetch_company(sources.edgar, t.cik, since=start)
        sector = sector_for_sic(company.sic)
        if not tickers and sector not in cfg.sectors:
            continue
        sid = ensure_security(
            conn,
            ticker=t.ticker,
            cik=t.cik,
            name=t.name,
            sector=sector,
            industry=company.sic_description,
        )
        names.append((t.ticker, sid, company))
    conn.commit()
    summary.securities = len(names)
    sids = {ticker: sid for ticker, sid, _ in names}

    # 2. Per-name history; commit after each name so a long run can resume.
    for ticker, sid, company in names:
        acceptance = {f.accession: f.accepted_at for f in company.filings}

        def fundamentals(
            cik: int = company.cik, sid: int = sid, acc: dict[str, datetime] = acceptance
        ) -> tuple[int, datetime | None]:
            rows = edgar_xbrl.fetch_fundamentals(sources.edgar, cik, sid, acc)
            return insert_fundamentals(conn, rows), _latest(rows)

        def insiders(
            company: edgar_submissions.Company = company, sid: int = sid
        ) -> tuple[int, datetime | None]:
            filings = edgar_form4.form4_filings(company.filings, start_dt, end_dt)
            rows = edgar_form4.fetch_insider_txns(sources.edgar, company.cik, sid, filings)
            return insert_insider_txns(conn, rows), _latest(rows)

        def bars(ticker: str = ticker, sid: int = sid) -> tuple[int, datetime | None]:
            rows = sources.bars.history(ticker, sid, start, end, now=now)
            if rows:
                first = min(b.event_time for b in rows).astimezone(ET).date()
                set_listing(conn, sid, listed_from=first, listed_to=None)
            return insert_price_bars(conn, rows), _latest(rows)

        def news(ticker: str = ticker) -> tuple[int, datetime | None]:
            rows = sources.news.fetch([ticker], start_dt, end_dt, sids)
            return insert_news(conn, rows), _latest(rows)

        _feed(conn, summary, FeedName.FUNDAMENTALS, ticker, now, fundamentals)
        _feed(conn, summary, FeedName.INSIDER_TRADES, ticker, now, insiders)
        _feed(conn, summary, FeedName.PRICE_BARS, ticker, now, bars)
        _feed(conn, summary, FeedName.NEWS, ticker, now, news)
        conn.commit()

    # 3. Month-end universe snapshots.
    if cfg.sectors:
        summary.snapshot_rows = snapshot_month_ends(conn, start, end, cfg)
        conn.commit()
    else:
        summary.errors.append("universe sectors are empty: no snapshots built (§18.2)")
    return summary


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m ingest.backfill",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    span = p.add_mutually_exclusive_group()
    span.add_argument("--years", type=float)
    span.add_argument("--days", type=int)
    p.add_argument("--universe", type=Path, default=Path("config/universe.yaml"))
    p.add_argument("--tickers", type=lambda s: [t.strip().upper() for t in s.split(",") if t])
    p.add_argument("--end", type=date.fromisoformat, help="last day (default: yesterday, ET)")
    p.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    io = p.add_mutually_exclusive_group()
    io.add_argument("--replay", type=Path, help="serve recorded responses from DIR (no network)")
    io.add_argument("--record", type=Path, help="save real responses under DIR")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None, *, inner: httpx.BaseTransport | None = None) -> int:
    """``inner`` replaces the network under ``--record`` (used to generate synthetic fixtures)."""
    args = parse_args(argv)
    if not args.database_url:
        raise SystemExit("--database-url or DATABASE_URL is required")
    now = datetime.now(UTC)
    end = args.end or (now.astimezone(ET).date() - timedelta(days=1))
    days = args.days if args.days is not None else round(365.25 * (args.years or 2))
    start = end - timedelta(days=days)
    cfg = load_universe(args.universe)
    if not cfg.sectors and not args.tickers:
        raise SystemExit(
            f"{args.universe}: sectors are empty; freeze them (§18.2) or pass --tickers"
        )

    transport: httpx.BaseTransport | None = None
    if args.replay:
        transport = ReplayTransport(args.replay)
    elif args.record:
        transport = RecordingTransport(args.record, inner)
    sources = build_sources(transport, replay=args.replay is not None, env=dict(os.environ))

    eng = engine(args.database_url)
    try:
        with eng.connect() as conn:
            summary = run(
                start=start,
                end=end,
                cfg=cfg,
                tickers=args.tickers,
                conn=conn,
                sources=sources,
                now=now,
            )
    finally:
        eng.dispose()
    print(
        json.dumps(
            {"start": start.isoformat(), "end": end.isoformat(), **summary.__dict__}, indent=2
        )
    )
    return 1 if summary.errors else 0


if __name__ == "__main__":
    sys.exit(main())
