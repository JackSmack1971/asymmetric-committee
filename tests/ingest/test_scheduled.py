"""Beat-driven ingestion (§4.1, §11): what a poll writes to the fact tables and to ``feed_health``.

The rule under test: freshness (``last_success_at``) is claimed only for a poll that fully
succeeded, so the stale-feed kill switch (§9) reads real evidence. Sources are fakes; the two EDGAR
feeds replay the recorded fixtures.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, text

from contracts.data import NewsItem, PriceBar
from contracts.enums import FeedName, NewsProviderName, PriceFeed
from ingest.edgar_client import EdgarClient, LocalSlidingWindowLimiter
from ingest.http import ReplayTransport
from ingest.scheduled import FeedIngestor
from risk.kill_switch import stale_feeds
from store import as_of
from store.write import ensure_security
from tests.conftest import reset
from tests.fixtures.synth import HTTP_DIR

T0 = datetime(2024, 6, 28, 21, 0, tzinfo=UTC)
SLA = {
    FeedName.PRICE_BARS: 72.0,
    FeedName.FUNDAMENTALS: 36.0,
    FeedName.INSIDER_TRADES: 36.0,
    FeedName.NEWS: 1.0,
}


class FakeBars:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.fail = fail or set()
        self.calls: list[str] = []

    def history(
        self, symbol: str, security_id: int, start: Any, end: Any, *, now: datetime
    ) -> list[PriceBar]:
        self.calls.append(symbol)
        if symbol in self.fail:
            raise RuntimeError(f"{symbol}: upstream 503")
        ts = datetime(2024, 6, 27, 20, 0, tzinfo=UTC)
        return [
            PriceBar(
                security_id=security_id,
                open=10,
                high=11,
                low=9,
                close=10.5,
                volume=1000,
                feed=PriceFeed.SIP,
                event_time=ts,
                available_at=ts + timedelta(minutes=15),
                source_version="sip",
            )
        ]


class FakeNews:
    name = NewsProviderName.ALPACA

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def fetch(
        self,
        symbols: Sequence[str],
        start: datetime,
        end: datetime,
        sids: Any,
        *,
        received_at: datetime | None = None,
    ) -> list[NewsItem]:
        if self.fail:
            raise TimeoutError("news provider timed out")
        published = end - timedelta(minutes=5)
        return [
            NewsItem(
                item_id="alpaca:1",
                security_ids=(sids[symbols[0]],),
                published_at=published,
                headline="h",
                summary="",
                body_hash="a" * 64,
                source=NewsProviderName.ALPACA,
                publisher="p",
                url="u",
                event_time=published,
                available_at=received_at or published,
                source_version="v1",
            )
        ]


@pytest.fixture
def engine(pg_engine: Engine) -> Iterator[Engine]:
    with pg_engine.begin() as conn:
        reset(conn)
        for i, tk in enumerate(("ALFA", "BRVO"), start=900001):
            ensure_security(conn, ticker=tk, cik=i, name=f"{tk} Corp")
    yield pg_engine


def ingestor(
    engine: Engine, *, bars: Any = None, news: Any = None, lookback: timedelta = timedelta(days=7)
) -> FeedIngestor:
    edgar = EdgarClient(
        LocalSlidingWindowLimiter(),
        user_agent="Tests (tests@localhost)",
        transport=ReplayTransport(HTTP_DIR),
    )
    src = SimpleNamespace(edgar=edgar, bars=bars or FakeBars(), news=news or FakeNews())
    return FeedIngestor(engine, src, lookback=lookback)  # type: ignore[arg-type]


def health(engine: Engine) -> dict[FeedName, Any]:
    with engine.connect() as conn:
        return {h.feed: h for h in as_of.feed_health(conn)}


def count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


def test_a_successful_poll_populates_freshness_and_facts(engine: Engine) -> None:
    res = ingestor(engine).run_feed(FeedName.PRICE_BARS, now=T0)
    assert res.ok and res.rows == 2 and res.errors == ()
    h = health(engine)[FeedName.PRICE_BARS]
    assert h.last_success_at == T0 and h.last_error is None and h.rows == 2
    assert count(engine, "price_bars") == 2
    assert (
        stale_feeds({FeedName.PRICE_BARS: h.last_success_at}, {FeedName.PRICE_BARS: 72}, T0) == []
    )


def test_a_duplicate_poll_adds_no_facts_and_stays_healthy(engine: Engine) -> None:
    job = ingestor(engine)
    job.run_feed(FeedName.NEWS, now=T0)
    again = job.run_feed(FeedName.NEWS, now=T0 + timedelta(minutes=15))
    assert again.ok and again.rows == 0  # same item, same revision: nothing new
    assert count(engine, "news_items") == 1
    assert health(engine)[FeedName.NEWS].last_success_at == T0 + timedelta(minutes=15)


def test_a_failed_poll_never_advances_freshness(engine: Engine) -> None:
    job = ingestor(engine)
    assert job.run_feed(FeedName.PRICE_BARS, now=T0).ok
    later = T0 + timedelta(hours=30)
    bad = ingestor(engine, bars=FakeBars(fail={"BRVO"})).run_feed(FeedName.PRICE_BARS, now=later)
    assert not bad.ok and "BRVO" in bad.errors[0]
    h = health(engine)[FeedName.PRICE_BARS]
    assert h.last_success_at == T0  # the success time is the last real one
    assert h.last_error is not None and "upstream 503" in h.last_error
    # after the SLA the feed reads stale to the kill switch
    assert stale_feeds({FeedName.PRICE_BARS: h.last_success_at}, SLA, T0 + timedelta(hours=73))


def test_a_feed_that_never_succeeded_is_never_fresh(engine: Engine) -> None:
    res = ingestor(engine, news=FakeNews(fail=True)).run_feed(FeedName.NEWS, now=T0)
    assert not res.ok
    h = health(engine)[FeedName.NEWS]
    assert h.last_success_at is None and "timed out" in (h.last_error or "")
    assert FeedName.NEWS in stale_feeds({FeedName.NEWS: h.last_success_at}, SLA, T0)


def test_a_partial_poll_keeps_the_good_facts_but_claims_no_freshness(engine: Engine) -> None:
    res = ingestor(engine, bars=FakeBars(fail={"BRVO"})).run_feed(FeedName.PRICE_BARS, now=T0)
    assert not res.ok and res.rows == 1
    assert count(engine, "price_bars") == 1  # ALFA's bar is kept, and a retry will not double it
    assert health(engine)[FeedName.PRICE_BARS].last_success_at is None


def test_an_empty_universe_is_an_error_not_a_success(pg_engine: Engine) -> None:
    with pg_engine.begin() as conn:
        reset(conn)
    res = ingestor(pg_engine).run_feed(FeedName.PRICE_BARS, now=T0)
    assert not res.ok
    assert health(pg_engine)[FeedName.PRICE_BARS].last_success_at is None


def test_edgar_feeds_ingest_from_recorded_filings_and_are_idempotent(engine: Engine) -> None:
    job = ingestor(engine, lookback=timedelta(days=900))
    for feed, table in (
        (FeedName.FUNDAMENTALS, "fundamentals_asfiled"),
        (FeedName.INSIDER_TRADES, "insider_txns"),
    ):
        first = job.run_feed(feed, now=T0)
        assert first.ok and first.rows > 0, first.errors
        n = count(engine, table)
        assert job.run_feed(feed, now=T0 + timedelta(hours=1)).rows == 0
        assert count(engine, table) == n
        assert health(engine)[feed].last_success_at == T0 + timedelta(hours=1)


def test_a_feed_without_a_job_is_refused(engine: Engine) -> None:
    with pytest.raises(ValueError, match="no ingest job"):
        ingestor(engine).run_feed(FeedName.REGIME, now=T0)
