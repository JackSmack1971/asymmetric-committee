"""Incremental ingestion for Celery beat (§4.1, §11): one poll of one feed over the stored universe.

This adds no fetching or parsing. It calls the same source clients and parsers as the backfill and
inserts through the same idempotent ``store.write`` helpers, over a short look-back window.

``feed_health`` is the freshness evidence the stale-feed kill switch reads (§9, §13), so it is
written only from what really happened:

* ``last_success_at`` advances only when every unit of the poll (each name, or each news batch)
  succeeded. A partial or failed poll records ``last_error`` and leaves the last success untouched.
* Facts from the units that did succeed are kept (inserts are ``ON CONFLICT DO NOTHING``, so a
  re-poll or a duplicate delivery adds nothing); only the freshness claim is withheld.
* An empty universe is an error, not a success: there was nothing to poll, so nothing is fresh.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine

from contracts.data import Security
from contracts.enums import FeedName
from ingest import edgar_form4, edgar_submissions, edgar_xbrl
from ingest.backfill import Sources
from ingest.timeutil import ET
from store import as_of as point_in_time
from store.write import (
    insert_fundamentals,
    insert_insider_txns,
    insert_news,
    insert_price_bars,
    record_feed_run,
)

NEWS_BATCH = 20  # symbols per news request (keeps the URL and the provider page count bounded)
_ERROR_CHARS = 2000

Unit = tuple[str, Callable[[Connection], tuple[int, datetime | None]]]


class IngestFailedError(RuntimeError):
    """A poll did not fully succeed. Freshness was not advanced; retrying is safe (idempotent)."""

    def __init__(self, feed: FeedName, errors: Sequence[str]) -> None:
        super().__init__(f"{feed.value}: {len(errors)} failure(s): {'; '.join(errors)[:500]}")
        self.feed = feed
        self.errors = tuple(errors)


@dataclass(frozen=True)
class FeedRunResult:
    feed: FeedName
    ok: bool
    rows: int
    errors: tuple[str, ...] = field(default=())


def _latest(rows: Sequence[Any]) -> datetime | None:
    return max((r.available_at for r in rows), default=None)


class FeedIngestor:
    def __init__(self, engine: Engine, sources: Sources, *, lookback: timedelta) -> None:
        self._engine = engine
        self._src = sources
        self._lookback = lookback

    def run_feed(self, feed: FeedName, *, now: datetime) -> FeedRunResult:
        """Poll ``feed`` once and record the outcome in ``feed_health`` (committed either way)."""
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        rows, latest = 0, None
        errors: list[str] = []
        with self._engine.connect() as conn:
            secs = point_in_time.securities(conn, listed_on=now.astimezone(UTC).date())
            conn.commit()
            if not secs:
                errors.append("no listed securities to ingest")
            for label, load in self._units(feed, secs, now):
                try:
                    with conn.begin_nested():
                        n, last = load(conn)
                except Exception as exc:  # one bad name must not hide the others' facts
                    errors.append(f"{label}: {type(exc).__name__}: {exc}"[:300])
                    continue
                rows += n
                if last is not None and (latest is None or last > latest):
                    latest = last
            record_feed_run(
                conn,
                feed,
                at=now,
                rows=rows,
                last_available_at=latest,
                error="; ".join(errors)[:_ERROR_CHARS] if errors else None,
            )
            conn.commit()
        return FeedRunResult(feed, not errors, rows, tuple(errors))

    # -- what one poll consists of ----------------------------------------------------------------

    def _units(self, feed: FeedName, secs: Sequence[Security], now: datetime) -> list[Unit]:
        start_dt = now - self._lookback
        start, today = start_dt.astimezone(ET).date(), now.astimezone(ET).date()
        src = self._src
        units: list[Unit] = []

        if feed is FeedName.PRICE_BARS:
            for s in secs:

                def bars(c: Connection, s: Security = s) -> tuple[int, datetime | None]:
                    rows = src.bars.history(s.ticker, s.security_id, start, today, now=now)
                    return insert_price_bars(c, rows), _latest(rows)

                units.append((s.ticker, bars))

        elif feed is FeedName.NEWS:
            sids = {s.ticker: s.security_id for s in secs}
            tickers = sorted(sids)
            for i in range(0, len(tickers), NEWS_BATCH):
                batch = tickers[i : i + NEWS_BATCH]

                def news(c: Connection, batch: list[str] = batch) -> tuple[int, datetime | None]:
                    rows = src.news.fetch(batch, start_dt, now, sids, received_at=now)
                    return insert_news(c, rows), _latest(rows)

                units.append((",".join(batch), news))

        elif feed is FeedName.FUNDAMENTALS:
            for s in secs:

                def fundamentals(c: Connection, s: Security = s) -> tuple[int, datetime | None]:
                    company = edgar_submissions.fetch_company(src.edgar, s.cik, since=start)
                    acceptance = {f.accession: f.accepted_at for f in company.filings}
                    rows = edgar_xbrl.fetch_fundamentals(
                        src.edgar, s.cik, s.security_id, acceptance
                    )
                    return insert_fundamentals(c, rows), _latest(rows)

                units.append((s.ticker, fundamentals))

        elif feed is FeedName.INSIDER_TRADES:
            for s in secs:

                def insiders(c: Connection, s: Security = s) -> tuple[int, datetime | None]:
                    company = edgar_submissions.fetch_company(src.edgar, s.cik, since=start)
                    filings = edgar_form4.form4_filings(company.filings, start_dt, now)
                    rows = edgar_form4.fetch_insider_txns(src.edgar, s.cik, s.security_id, filings)
                    return insert_insider_txns(c, rows), _latest(rows)

                units.append((s.ticker, insiders))

        else:
            raise ValueError(f"{feed.value} has no ingest job")
        return units
