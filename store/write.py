"""Ingest-side writes. Every fact insert is ``ON CONFLICT DO NOTHING`` on natural key +
``source_version``, so re-running an ingestor is a no-op and existing versions are never changed.
New information (a restatement, a revised article, a SIP bar) is always a new row."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Connection, Table, func, literal, select
from sqlalchemy.dialects.postgresql import insert

from contracts.data import (
    FeatureRow,
    FundamentalFact,
    InsiderTxn,
    NewsItem,
    PriceBar,
    UniverseMember,
)
from contracts.enums import FeedName
from store import _tables as t

_CHUNK = 1000


def _row(model: BaseModel, table: Table) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in model.model_dump().items():
        if k not in table.c:
            continue
        if isinstance(v, Enum):
            v = v.value
        elif isinstance(v, tuple):
            v = list(v)
        out[k] = v
    return out


def _insert(conn: Connection, table: Table, models: Sequence[BaseModel]) -> int:
    """Insert new versions; returns how many rows were actually new."""
    inserted = 0
    for i in range(0, len(models), _CHUNK):
        chunk = [_row(m, table) for m in models[i : i + _CHUNK]]
        if not chunk:
            continue
        stmt = insert(table).values(chunk).on_conflict_do_nothing().returning(literal(1))
        inserted += len(conn.execute(stmt).all())
    return inserted


def insert_price_bars(conn: Connection, rows: Sequence[PriceBar]) -> int:
    return _insert(conn, t.price_bars, rows)


def insert_fundamentals(conn: Connection, rows: Sequence[FundamentalFact]) -> int:
    return _insert(conn, t.fundamentals_asfiled, rows)


def insert_insider_txns(conn: Connection, rows: Sequence[InsiderTxn]) -> int:
    return _insert(conn, t.insider_txns, rows)


def insert_news(conn: Connection, rows: Sequence[NewsItem]) -> int:
    return _insert(conn, t.news_items, rows)


def insert_features(conn: Connection, rows: Sequence[FeatureRow]) -> int:
    return _insert(conn, t.features, rows)


def insert_universe_snapshot(conn: Connection, rows: Sequence[UniverseMember]) -> int:
    return _insert(conn, t.universe_snapshots, rows)


# --- reference data --------------------------------------------------------------------------


def ensure_security(
    conn: Connection,
    *,
    ticker: str,
    cik: int,
    name: str,
    sector: str | None = None,
    industry: str | None = None,
) -> int:
    """Create or refresh a security (reference data, not a fact) and return its id."""
    stmt = insert(t.securities).values(
        ticker=ticker, cik=cik, name=name, sector=sector, industry=industry
    )
    upsert = stmt.on_conflict_do_update(
        constraint="uq_securities_cik_ticker",
        set_={
            "name": stmt.excluded.name,
            "sector": stmt.excluded.sector,
            "industry": stmt.excluded.industry,
        },
    ).returning(t.securities.c.security_id)
    return int(conn.execute(upsert).scalar_one())


def set_listing(
    conn: Connection, security_id: int, *, listed_from: date | None, listed_to: date | None
) -> None:
    """Widen ``listed_from`` to the earliest date seen; set ``listed_to`` when a name delists."""
    c = t.securities.c
    values: dict[str, Any] = {}
    if listed_from is not None:
        values["listed_from"] = func.least(func.coalesce(c.listed_from, listed_from), listed_from)
    values["listed_to"] = listed_to
    conn.execute(t.securities.update().where(c.security_id == security_id).values(**values))


def security_ids_by_ticker(
    conn: Connection, tickers: Iterable[str] | None = None
) -> dict[str, int]:
    c = t.securities.c
    stmt = select(c.ticker, c.security_id)
    if tickers is not None:
        stmt = stmt.where(c.ticker.in_(list(tickers)))
    return {tk: sid for tk, sid in conn.execute(stmt).tuples()}


def security_ids_by_cik(conn: Connection) -> Mapping[int, list[int]]:
    out: dict[int, list[int]] = {}
    for cik, sid in conn.execute(select(t.securities.c.cik, t.securities.c.security_id)).tuples():
        out.setdefault(cik, []).append(sid)
    return out


# --- freshness -------------------------------------------------------------------------------


def record_feed_run(
    conn: Connection,
    feed: FeedName,
    *,
    at: datetime,
    rows: int = 0,
    last_available_at: datetime | None = None,
    error: str | None = None,
) -> None:
    """Record one ingest attempt in ``feed_health``. Failures keep the last success time."""
    c = t.feed_health.c
    ok = error is None
    stmt = insert(t.feed_health).values(
        feed=feed.value,
        last_success_at=at if ok else None,
        last_available_at=last_available_at,
        rows=rows,
        last_error=error,
        updated_at=at,
    )
    ex = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=[c.feed],
        set_={
            "last_success_at": ex.last_success_at if ok else c.last_success_at,
            "last_available_at": func.greatest(c.last_available_at, ex.last_available_at),
            "rows": c.rows + ex.rows,
            "last_error": ex.last_error,
            "updated_at": ex.updated_at,
        },
    )
    conn.execute(stmt)
