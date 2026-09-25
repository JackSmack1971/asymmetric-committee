"""Builders for fact rows used across store/ingest tests."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Connection

from contracts.data import FundamentalFact, InsiderTxn, NewsItem, PriceBar
from contracts.enums import InsiderRole, InsiderTxnCode, NewsProviderName, PriceFeed
from store.write import ensure_security

T0 = datetime(2024, 1, 2, tzinfo=UTC)


def security(conn: Connection, ticker: str = "AAA", cik: int = 1001, sector: str = "Tech") -> int:
    return ensure_security(conn, ticker=ticker, cik=cik, name=f"{ticker} Inc", sector=sector)


def accession(n: int, year: int = 24) -> str:
    return f"0000000001-{year:02d}-{n:06d}"


def fact(
    sid: int,
    *,
    available_at: datetime,
    accn: str,
    value: float = 1.0,
    concept: str = "us-gaap:Revenues",
    period_end: date = date(2023, 12, 31),
    period_start: date | None = date(2023, 10, 1),
) -> FundamentalFact:
    return FundamentalFact(
        security_id=sid,
        concept=concept,
        unit="USD",
        period_start=period_start,
        period_end=period_end,
        fiscal_period="Q4",
        form="10-K",
        value=value,
        event_time=datetime.combine(period_end, datetime.min.time(), UTC),
        available_at=available_at,
        source_version=accn,
    )


def bar(
    sid: int,
    day: date,
    *,
    feed: PriceFeed = PriceFeed.SIP,
    close: float = 10.0,
    available_at: datetime | None = None,
) -> PriceBar:
    ev = datetime.combine(day, datetime.min.time(), UTC)
    return PriceBar(
        security_id=sid,
        event_time=ev,
        available_at=available_at or ev + timedelta(hours=21, minutes=15),
        source_version=feed.value,
        open=close,
        high=close * 1.01,
        low=close * 0.99,
        close=close,
        volume=1e6,
        feed=feed,
    )


def news_item(
    sids: tuple[int, ...],
    item_id: str,
    *,
    published_at: datetime,
    available_at: datetime,
    revision: str,
    headline: str = "Headline",
) -> NewsItem:
    return NewsItem(
        item_id=item_id,
        security_ids=sids,
        published_at=published_at,
        headline=headline,
        summary="",
        body_hash=hashlib.sha256(headline.encode()).hexdigest(),
        source=NewsProviderName.ALPACA,
        publisher="wire",
        url="https://example.test/a",
        event_time=published_at,
        available_at=available_at,
        source_version=revision,
    )


def insider(
    sid: int, *, txn_date: date, accepted_at: datetime, accn: str, seq: int = 0
) -> InsiderTxn:
    return InsiderTxn(
        security_id=sid,
        accession=accn,
        seq=seq,
        filer="Owner",
        role=InsiderRole.OFFICER,
        officer_title="CFO",
        txn_date=txn_date,
        code=InsiderTxnCode.P,
        acquired=True,
        shares=100,
        price=10.0,
        post_holdings=1000,
        is_10b5_1=False,
        event_time=datetime.combine(txn_date, datetime.min.time(), UTC),
        available_at=accepted_at,
        source_version=accn,
    )
