"""as_of() is the point-in-time read path (§1.2, §4.2, invariant 3, §13 restatement row)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Connection

from contracts.enums import FeedName, PriceFeed
from store import as_of
from store.write import (
    insert_fundamentals,
    insert_insider_txns,
    insert_news,
    insert_price_bars,
    record_feed_run,
)
from tests.store import factories as f

MIN = timedelta(minutes=1)
PROPERTY = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)

# (key, available_at offset in minutes, version) with unique (key, version)
facts_strategy = st.lists(
    st.tuples(st.integers(0, 3), st.integers(0, 5000), st.integers(1, 40)),
    min_size=1,
    max_size=25,
    unique_by=lambda x: (x[0], x[2]),
)


def _oracle(rows: list[tuple[int, int, str]], t: int) -> dict[int, tuple[int, str]]:
    """Per key, the max (available_at, version) among rows with available_at <= t."""
    best: dict[int, tuple[int, str]] = {}
    for key, avail, ver in rows:
        if avail <= t and (key not in best or (avail, ver) > best[key]):
            best[key] = (avail, ver)
    return best


@PROPERTY
@given(rows=facts_strategy, t=st.integers(-10, 5100))
def test_fundamentals_point_in_time_property(
    db: Connection, rows: list[tuple[int, int, int]], t: int
) -> None:
    sp = db.begin_nested()
    sid = f.security(db)
    periods = [date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31)]
    facts = [
        f.fact(
            sid,
            available_at=f.T0 + a * MIN,
            accn=f.accession(v),
            value=float(v),
            period_end=periods[k],
            period_start=None,
        )
        for k, a, v in rows
    ]
    insert_fundamentals(db, facts)
    ts = f.T0 + t * MIN
    got = as_of.fundamentals(db, sid, ts)
    sp.rollback()

    assert all(r.available_at <= ts for r in got)
    expected = _oracle([(k, a, f.accession(v)) for k, a, v in rows], t)
    assert {periods.index(r.period_end): (r.available_at, r.source_version) for r in got} == {
        k: (f.T0 + a * MIN, ver) for k, (a, ver) in expected.items()
    }


@PROPERTY
@given(
    rows=st.lists(
        st.tuples(st.integers(0, 3), st.integers(0, 5000), st.sampled_from(list(PriceFeed))),
        min_size=1,
        max_size=20,
        unique_by=lambda x: (x[0], x[2]),
    ),
    t=st.integers(-10, 5100),
)
def test_prices_point_in_time_property(
    db: Connection, rows: list[tuple[int, int, PriceFeed]], t: int
) -> None:
    sp = db.begin_nested()
    sid = f.security(db)
    days = [date(2023, 12, 26) + timedelta(days=k) for k in range(4)]
    insert_price_bars(
        db,
        [
            f.bar(sid, days[k], feed=feed, close=10 + a, available_at=f.T0 + a * MIN)
            for k, a, feed in rows
        ],
    )
    ts = f.T0 + t * MIN
    got = as_of.prices(db, [sid], ts, lookback=timedelta(days=30))
    sp.rollback()

    assert all(b.available_at <= ts for b in got)
    expected = _oracle([(k, a, feed.value) for k, a, feed in rows], t)
    assert {days.index(b.event_time.date()): (b.available_at, b.source_version) for b in got} == {
        k: (f.T0 + a * MIN, ver) for k, (a, ver) in expected.items()
    }


@PROPERTY
@given(rows=facts_strategy, t=st.integers(-10, 5100))
def test_news_point_in_time_property(
    db: Connection, rows: list[tuple[int, int, int]], t: int
) -> None:
    sp = db.begin_nested()
    sid = f.security(db)
    insert_news(
        db,
        [
            f.news_item(
                (sid,),
                f"n{k}",
                published_at=f.T0 - timedelta(days=1),
                available_at=f.T0 + a * MIN,
                revision=f"r{v:03d}",
            )
            for k, a, v in rows
        ],
    )
    ts = f.T0 + t * MIN
    got = as_of.news(db, [sid], ts, lookback=timedelta(days=7))
    sp.rollback()

    assert all(n.available_at <= ts for n in got)
    expected = _oracle([(k, a, f"r{v:03d}") for k, a, v in rows], t)
    assert {int(n.item_id[1:]): (n.available_at, n.source_version) for n in got} == {
        k: (f.T0 + a * MIN, ver) for k, (a, ver) in expected.items()
    }


def test_restatement_as_of_before_second_accession_returns_first(db: Connection) -> None:
    sid = f.security(db)
    first_at = datetime(2024, 2, 1, 21, 5, tzinfo=UTC)
    restated_at = datetime(2024, 5, 1, 20, 30, tzinfo=UTC)
    insert_fundamentals(
        db,
        [
            f.fact(sid, available_at=first_at, accn=f.accession(1), value=100.0),
            f.fact(sid, available_at=restated_at, accn=f.accession(2), value=90.0),
        ],
    )
    assert as_of.fundamentals(db, sid, first_at - MIN) == []
    (before,) = as_of.fundamentals(db, sid, restated_at - MIN)
    assert (before.value, before.source_version) == (100.0, f.accession(1))
    (after,) = as_of.fundamentals(db, sid, restated_at)
    assert (after.value, after.source_version) == (90.0, f.accession(2))


def test_form4_is_invisible_until_filed(db: Connection) -> None:
    """Transaction on day D, filed D+2: invisible at D+1 (§4.1: txn date is look-ahead)."""
    sid = f.security(db)
    d = date(2024, 3, 4)
    accepted = datetime(2024, 3, 6, 21, 15, tzinfo=UTC)  # D+2, 16:15 ET
    insert_insider_txns(db, [f.insider(sid, txn_date=d, accepted_at=accepted, accn=f.accession(7))])
    lookback = timedelta(days=30)
    end_of_d1 = datetime(2024, 3, 5, 23, 59, tzinfo=UTC)
    assert as_of.insider_txns(db, [sid], end_of_d1, lookback) == []
    (seen,) = as_of.insider_txns(db, [sid], accepted, lookback)
    assert seen.txn_date == d


def test_sip_bar_supersedes_live_iex_once_available(db: Connection) -> None:
    sid = f.security(db)
    day = date(2024, 3, 4)
    close = datetime(2024, 3, 4, 21, 0, tzinfo=UTC)
    insert_price_bars(
        db,
        [
            f.bar(sid, day, feed=PriceFeed.IEX, close=10.0, available_at=close + MIN),
            f.bar(sid, day, feed=PriceFeed.SIP, close=10.2, available_at=close + 15 * MIN),
        ],
    )
    lb = timedelta(days=5)
    assert [b.feed for b in as_of.prices(db, [sid], close + 5 * MIN, lb)] == [PriceFeed.IEX]
    assert [b.feed for b in as_of.prices(db, [sid], close + 15 * MIN, lb)] == [PriceFeed.SIP]


def test_naive_as_of_is_rejected(db: Connection) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        as_of.fundamentals(db, 1, datetime(2024, 1, 1))


def test_feed_staleness(db: Connection) -> None:
    now = datetime(2024, 3, 4, 12, tzinfo=UTC)
    record_feed_run(db, FeedName.PRICE_BARS, at=now - timedelta(hours=2), rows=5)
    record_feed_run(db, FeedName.NEWS, at=now - timedelta(hours=3), rows=5)
    record_feed_run(db, FeedName.NEWS, at=now, error="HTTP 500")  # failure keeps last success
    report = {
        s.feed: s
        for s in as_of.feed_staleness(
            db, now, {FeedName.PRICE_BARS: 30, FeedName.NEWS: 1, FeedName.FUNDAMENTALS: 48}
        )
    }
    assert not report[FeedName.PRICE_BARS].stale
    assert report[FeedName.NEWS].stale and report[FeedName.NEWS].age_hours == pytest.approx(3)
    assert report[FeedName.FUNDAMENTALS].stale  # never ran
    (news_health,) = [h for h in as_of.feed_health(db) if h.feed is FeedName.NEWS]
    assert news_health.last_error == "HTTP 500" and news_health.rows == 5
