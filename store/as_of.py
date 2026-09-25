"""The only read path for fact tables (§1.2, §4.2, invariant 2).

Every function returns, per natural key, the latest version knowable at ``as_of``: among rows with
``available_at <= as_of`` the winner is the max ``(available_at, source_version)``. A row with
``available_at > as_of`` can never be returned (invariant 3).

Callers pass an open ``Conn``; they never import SQLAlchemy themselves (an import-linter contract
forbids it outside ``store/`` and ``ingest/``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Column, ColumnElement, Connection, Table, and_, func, select
from sqlalchemy.dialects.postgresql import distinct_on

from contracts.data import (
    FeatureRow,
    FeedHealth,
    FeedStaleness,
    FundamentalFact,
    InsiderTxn,
    NewsItem,
    PriceBar,
    Security,
    UniverseMember,
)
from contracts.enums import FeedName
from store import _tables as t

Conn = Connection

__all__ = [
    "Conn",
    "feature_rows",
    "feed_health",
    "feed_staleness",
    "fundamentals",
    "insider_txns",
    "news",
    "prices",
    "securities",
    "universe",
]


def _check_ts(as_of: datetime) -> None:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")


def _latest(
    conn: Conn,
    table: Table,
    key: Sequence[Column[Any]],
    as_of: datetime,
    *where: ColumnElement[bool],
) -> list[dict[str, Any]]:
    """Latest version per ``key`` among rows knowable at ``as_of``."""
    _check_ts(as_of)
    stmt = (
        select(table)
        .where(table.c.available_at <= as_of, *where)
        .ext(distinct_on(*key))
        .order_by(*key, table.c.available_at.desc(), table.c.source_version.collate("C").desc())
    )
    rows = [dict(r._mapping) for r in conn.execute(stmt)]
    for r in rows:
        r.pop("ingested_at", None)
    return rows


def prices(
    conn: Conn, security_ids: Iterable[int], as_of: datetime, lookback: timedelta
) -> list[PriceBar]:
    """Daily bars with ``event_time`` in ``(as_of - lookback, as_of]``, oldest first."""
    ids = list(security_ids)
    c = t.price_bars.c
    rows = _latest(
        conn,
        t.price_bars,
        [c.security_id, c.event_time],
        as_of,
        c.security_id.in_(ids),
        c.event_time > as_of - lookback,
        c.event_time <= as_of,
    )
    return [PriceBar.model_validate(r) for r in rows]


def fundamentals(
    conn: Conn, security_id: int, as_of: datetime, concepts: Iterable[str] | None = None
) -> list[FundamentalFact]:
    """As-filed facts; for a restated period, the version filed most recently before ``as_of``."""
    c = t.fundamentals_asfiled.c
    where = [c.security_id == security_id]
    if concepts is not None:
        where.append(c.concept.in_(list(concepts)))
    rows = _latest(
        conn,
        t.fundamentals_asfiled,
        [c.security_id, c.concept, c.unit, c.period_start, c.period_end],
        as_of,
        *where,
    )
    return [FundamentalFact.model_validate(r) for r in rows]


def insider_txns(
    conn: Conn, security_ids: Iterable[int], as_of: datetime, lookback: timedelta
) -> list[InsiderTxn]:
    """Form 4 lines whose filing became public in ``(as_of - lookback, as_of]``."""
    c = t.insider_txns.c
    rows = _latest(
        conn,
        t.insider_txns,
        [c.accession, c.seq],
        as_of,
        c.security_id.in_(list(security_ids)),
        c.available_at > as_of - lookback,
    )
    return [InsiderTxn.model_validate(r) for r in rows]


def news(
    conn: Conn, security_ids: Iterable[int], as_of: datetime, lookback: timedelta
) -> list[NewsItem]:
    """Latest known revision of items published in ``(as_of - lookback, as_of]``."""
    c = t.news_items.c
    rows = _latest(
        conn,
        t.news_items,
        [c.item_id],
        as_of,
        c.security_ids.overlap(list(security_ids)),
        c.event_time > as_of - lookback,
    )
    return [NewsItem.model_validate(r) for r in rows]


def feature_rows(
    conn: Conn, security_ids: Iterable[int], as_of: datetime, feature_set_version: str
) -> list[FeatureRow]:
    """Most recent feature vector per security."""
    c = t.features.c
    rows = _latest(
        conn,
        t.features,
        [c.security_id, c.event_time, c.feature_set_version],
        as_of,
        c.security_id.in_(list(security_ids)),
        c.feature_set_version == feature_set_version,
    )
    newest: dict[int, dict[str, Any]] = {}
    for r in rows:
        if (
            r["security_id"] not in newest
            or r["event_time"] > newest[r["security_id"]]["event_time"]
        ):
            newest[r["security_id"]] = r
    return [FeatureRow.model_validate(r) for r in newest.values()]


def universe(conn: Conn, as_of: datetime, *, included_only: bool = True) -> list[UniverseMember]:
    """The most recent monthly snapshot knowable at ``as_of`` (§4.4), ranked."""
    _check_ts(as_of)
    c = t.universe_snapshots.c
    latest_date = conn.execute(
        select(func.max(c.snapshot_date)).where(c.available_at <= as_of)
    ).scalar_one_or_none()
    if latest_date is None:
        return []
    where = [c.snapshot_date == latest_date]
    if included_only:
        where.append(c.included.is_(True))
    rows = _latest(conn, t.universe_snapshots, [c.snapshot_date, c.security_id], as_of, *where)
    members = [UniverseMember.model_validate(r) for r in rows]
    return sorted(members, key=lambda m: (m.rank is None, m.rank or 0, m.security_id))


def securities(
    conn: Conn, security_ids: Iterable[int] | None = None, *, listed_on: date | None = None
) -> list[Security]:
    """Reference data. ``listed_on`` keeps names listed on that date (delisted ones drop out)."""
    c = t.securities.c
    stmt = select(t.securities).order_by(c.security_id)
    if security_ids is not None:
        stmt = stmt.where(c.security_id.in_(list(security_ids)))
    if listed_on is not None:
        stmt = stmt.where(
            and_(
                (c.listed_from.is_(None)) | (c.listed_from <= listed_on),
                (c.listed_to.is_(None)) | (c.listed_to >= listed_on),
            )
        )
    return [Security.model_validate(dict(r._mapping)) for r in conn.execute(stmt)]


def feed_health(conn: Conn) -> list[FeedHealth]:
    return [
        FeedHealth.model_validate(dict(r._mapping))
        for r in conn.execute(select(t.feed_health).order_by(t.feed_health.c.feed))
    ]


def feed_staleness(
    conn: Conn, now: datetime, sla_hours: Mapping[FeedName, float]
) -> list[FeedStaleness]:
    """Age of each feed's last successful ingest vs its SLA (§13). Never ran = stale."""
    _check_ts(now)
    health = {h.feed: h for h in feed_health(conn)}
    out = []
    for feed, sla in sorted(sla_hours.items()):
        last = health[feed].last_success_at if feed in health else None
        age = None if last is None else max((now - last).total_seconds() / 3600, 0.0)
        out.append(
            FeedStaleness(
                feed=feed,
                sla_hours=sla,
                last_success_at=last,
                age_hours=age,
                stale=age is None or age > sla,
                checked_at=now,
            )
        )
    return out
