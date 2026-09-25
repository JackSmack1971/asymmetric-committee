"""Point-in-time data rows (§4.2, §4.3).

Ingestors produce these, ``store.write`` persists them and ``store.as_of`` returns them. Every fact
row carries the bitemporal columns: ``event_time`` (when it happened), ``available_at`` (when it was
knowable) and ``source_version`` (filing accession, article revision, bar feed).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Self

from pydantic import AwareDatetime, Field, model_validator

from contracts.enums import (
    FeedName,
    InsiderRole,
    InsiderTxnCode,
    McapTier,
    NewsProviderName,
    PriceFeed,
)
from contracts.models import Contract, Finite, NonNegative, Positive, SecurityId, Sha256Hex

Accession = Annotated[str, Field(pattern=r"^\d{10}-\d{2}-\d{6}$")]
Version = Annotated[str, Field(min_length=1, max_length=128)]
Ticker = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9.\-]{0,9}$")]


class Fact(Contract):
    """Bitemporal columns shared by every fact row."""

    event_time: AwareDatetime
    available_at: AwareDatetime
    source_version: Version


class PriceBar(Fact):
    security_id: SecurityId
    open: Positive
    high: Positive
    low: Positive
    close: Positive
    volume: NonNegative
    feed: PriceFeed

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("bar violates low <= open, close <= high")
        if self.source_version != self.feed.value:
            raise ValueError("price bar source_version must equal its feed")
        return self


class FundamentalFact(Fact):
    """One as-filed XBRL value. ``source_version`` is the accession; rows are never updated."""

    security_id: SecurityId
    concept: Annotated[str, Field(pattern=r"^[a-z\-]+:[A-Za-z0-9_]+$")]
    unit: Annotated[str, Field(min_length=1, max_length=32)]
    period_start: date | None
    period_end: date
    fiscal_period: Annotated[str, Field(max_length=8)] | None
    form: Annotated[str, Field(max_length=16)] | None
    value: Finite

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.period_start is not None and self.period_start > self.period_end:
            raise ValueError("period_start after period_end")
        return self


class InsiderTxn(Fact):
    """One non-derivative Form 4 line. ``event_time`` is the transaction date; ``available_at`` is
    the filing acceptance time (using the transaction date would be look-ahead, §4.1)."""

    security_id: SecurityId
    accession: Accession
    seq: int = Field(ge=0)
    filer: Annotated[str, Field(min_length=1, max_length=256)]
    role: InsiderRole
    officer_title: Annotated[str, Field(max_length=256)] | None
    txn_date: date
    code: InsiderTxnCode
    acquired: bool
    shares: NonNegative
    price: NonNegative | None
    post_holdings: NonNegative | None
    is_10b5_1: bool

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.source_version != self.accession:
            raise ValueError("insider txn source_version must be its accession")
        return self


class NewsItem(Fact):
    """One revision of a news item. ``event_time`` is the publish time."""

    item_id: Annotated[str, Field(min_length=1, max_length=128)]
    security_ids: tuple[SecurityId, ...] = Field(min_length=1)
    published_at: AwareDatetime
    headline: Annotated[str, Field(min_length=1, max_length=1000)]
    summary: Annotated[str, Field(max_length=20000)]
    body_hash: Sha256Hex
    source: NewsProviderName
    publisher: Annotated[str, Field(max_length=256)]
    url: Annotated[str, Field(max_length=2048)]


class FeatureRow(Fact):
    security_id: SecurityId
    feature_set_version: Version
    values: dict[str, Any]


class Security(Contract):
    """Reference data (not a fact table). Identity fields never reach an LLM (invariant 4)."""

    security_id: SecurityId
    ticker: Ticker
    cik: int = Field(ge=1)
    name: Annotated[str, Field(min_length=1, max_length=256)]
    sector: Annotated[str, Field(min_length=1, max_length=64)] | None
    industry: Annotated[str, Field(max_length=256)] | None
    listed_from: date | None
    listed_to: date | None


class UniverseMember(Fact):
    """One evaluated candidate in a monthly snapshot (§4.4); ``source_version`` = config hash."""

    snapshot_date: date
    security_id: SecurityId
    mcap_usd: NonNegative | None
    mcap_tier: McapTier | None
    adv_usd: NonNegative | None
    price: NonNegative | None
    score: Finite | None
    rank: int | None = Field(default=None, ge=1)
    included: bool
    reason: Annotated[str, Field(max_length=64)]


class FeedHealth(Contract):
    feed: FeedName
    last_success_at: AwareDatetime | None
    last_available_at: AwareDatetime | None
    rows: int = Field(ge=0)
    last_error: Annotated[str, Field(max_length=2000)] | None
    updated_at: AwareDatetime


class FeedStaleness(Contract):
    """Freshness of one feed against its SLA (§13 "stale feed trades")."""

    feed: FeedName
    sla_hours: Positive
    last_success_at: AwareDatetime | None
    age_hours: NonNegative | None
    stale: bool
    checked_at: AwareDatetime
