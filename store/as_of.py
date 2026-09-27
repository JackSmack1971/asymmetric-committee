"""The only read path for fact tables (§1.2, §4.2, invariant 2).

Every function returns, per natural key, the latest version knowable at ``as_of``: among rows with
``available_at <= as_of`` the winner is the max ``(available_at, source_version)``. A row with
``available_at > as_of`` can never be returned (invariant 3).

Callers pass an open ``Conn``; they never import SQLAlchemy themselves (an import-linter contract
forbids it outside ``store/`` and ``ingest/``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy import Column, ColumnElement, Connection, Table, and_, func, select
from sqlalchemy.dialects.postgresql import distinct_on

from contracts.corporate_actions import (
    AssetStatusObservation,
    CorporateAction,
    CorporateActionCoverage,
    Delisting,
    DelistingFiling,
    IdentityConflict,
    SecuritySymbol,
)
from contracts.data import (
    AliasList,
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
from contracts.enums import FeedName, KnowledgeBasis, SecurityKind
from contracts.market_data import (
    CalendarCoverage,
    ExecutionReference,
    HaltReference,
    HaltReferenceRequest,
    HaltSymbolSet,
    ReferenceInstrument,
    TBillObservation,
    TBillVintageCoverage,
    TradingSession,
)
from evaluation.calendar_rules import TradingCalendar, build_calendar
from store import _tables as t
from store.aliases import build_alias_list

Conn = Connection

__all__ = [
    "Conn",
    "action_coverage",
    "alias_list",
    "asset_status",
    "bar_dates",
    "calendar_coverage",
    "calendar_sessions",
    "corporate_actions",
    "delisting",
    "delisting_filings",
    "delisting_history",
    "execution_references",
    "feature_rows",
    "feed_health",
    "feed_staleness",
    "fundamentals",
    "halt_reference_requests",
    "halt_references",
    "halt_symbol_set",
    "identity_conflicts",
    "insider_txns",
    "news",
    "pending_halt_requests",
    "prices",
    "reference_instruments",
    "securities",
    "security_cik",
    "security_symbols",
    "symbol_history",
    "tbill_rates",
    "tbill_vintage_coverage",
    "trading_calendar",
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
    """Equity reference data (common stock with a real CIK); ETFs are `reference_instruments`.

    ``listed_on`` keeps names listed on that date (delisted ones drop out).
    """
    c = t.securities.c
    stmt = (
        select(*(col for col in t.securities.c if col.name != "kind"))
        .where(c.kind == SecurityKind.EQUITY.value)
        .order_by(c.security_id)
    )
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


def alias_list(
    conn: Conn,
    as_of: datetime,
    brands: Mapping[int, Sequence[str]] | None = None,
    security_ids: Iterable[int] | None = None,
) -> AliasList:
    """Anonymizer aliases (name, ticker, CIK, brands) of securities listed at ``as_of`` (UTC date).

    Identity comes from ``securities`` (stored reference data); ``brands`` maps CIK -> brand
    aliases from ``config.aliases``. Pass ``security_ids`` to bound the list (the masker's cost
    grows with its size).
    """
    _check_ts(as_of)
    listed = securities(conn, security_ids, listed_on=as_of.astimezone(UTC).date())
    # Every ticker a listed security has traded under stays masked (a rename keeps its id). Masking
    # a symbol recorded after ``as_of`` hides more, never less, and the list never reaches an LLM.
    sym = t.security_symbols.c
    symbols: dict[int, list[str]] = {}
    rows = conn.execute(
        select(sym.security_id, sym.symbol)
        .where(sym.security_id.in_([s.security_id for s in listed]))
        .order_by(sym.security_id, sym.valid_from, sym.symbol)
    ).all()
    for sid, symbol in rows:
        symbols.setdefault(int(sid), []).append(str(symbol))
    return build_alias_list(listed, as_of, brands, symbols)


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


# --- market-data foundations (P6.3) ------------------------------------------------------------


def reference_instruments(conn: Conn) -> list[ReferenceInstrument]:
    """SPY and the sector ETFs (kind ``etf``, no CIK). Never part of the equity universe."""
    c = t.securities.c
    stmt = (
        select(c.security_id, c.ticker, c.name, c.kind)
        .where(c.kind != SecurityKind.EQUITY.value)
        .order_by(c.ticker)
    )
    return [ReferenceInstrument.model_validate(dict(r._mapping)) for r in conn.execute(stmt)]


def calendar_sessions(conn: Conn, as_of: datetime) -> list[TradingSession]:
    """Every session version knowable at ``as_of`` (the calendar view needs them all)."""
    _check_ts(as_of)
    c = t.trading_calendar.c
    stmt = (
        select(t.trading_calendar)
        .where(c.available_at <= as_of)
        .order_by(c.session_date, c.available_at, c.source_version)
    )
    rows = [dict(r._mapping) for r in conn.execute(stmt)]
    return [_model(TradingSession, r) for r in rows]


def calendar_coverage(conn: Conn, as_of: datetime) -> list[CalendarCoverage]:
    _check_ts(as_of)
    c = t.calendar_coverage.c
    stmt = (
        select(t.calendar_coverage)
        .where(c.available_at <= as_of)
        .order_by(c.range_start, c.range_end, c.available_at, c.source_version)
    )
    rows = [dict(r._mapping) for r in conn.execute(stmt)]
    return [_model(CalendarCoverage, r) for r in rows]


def trading_calendar(conn: Conn, as_of: datetime) -> TradingCalendar:
    """The calendar as known at ``as_of``: sessions and coverage from one point-in-time view.

    Raises `CalendarCoverageError` if a coverage record does not match exactly the session versions
    visible at ``as_of`` (fail closed). Pass a connection inside one snapshot transaction.
    """
    return build_calendar(calendar_sessions(conn, as_of), calendar_coverage(conn, as_of))


def tbill_rates(conn: Conn, series: str, accrual_date: date) -> list[TBillObservation]:
    """Every observation/vintage usable for accrual session ``accrual_date`` (vintage before it).

    Date-granular by design (§4.3 exception): a same-day vintage is excluded, so a later vintage
    can never change what an earlier accrual date sees.
    """
    c = t.tbill_rates.c
    stmt = (
        select(t.tbill_rates)
        .where(c.series == series, c.vintage_date < accrual_date)
        .order_by(c.observation_date, c.vintage_date)
    )
    return [_model(TBillObservation, r._mapping) for r in conn.execute(stmt)]


def tbill_vintage_coverage(conn: Conn, series: str, as_of: datetime) -> TBillVintageCoverage | None:
    """The latest vintage-coverage record established at or before ``as_of``."""
    _check_ts(as_of)
    c = t.tbill_vintage_coverage.c
    stmt = (
        select(t.tbill_vintage_coverage)
        .where(c.series == series, c.established_at <= as_of)
        .order_by(c.established_at.desc(), c.source_version.collate("C").desc())
        .limit(1)
    )
    row = conn.execute(stmt).mappings().first()
    if row is None:
        return None
    return _model(TBillVintageCoverage, row)


def _model[M: BaseModel](model: type[M], row: Any) -> M:
    """Validate a stored row as ``model``, dropping the database-only ``ingested_at``."""
    return model.model_validate({k: v for k, v in dict(row).items() if k != "ingested_at"})


def _reference_rows(conn: Conn, table: Table, *where: ColumnElement[bool]) -> list[dict[str, Any]]:
    rows = [dict(r._mapping) for r in conn.execute(select(table).where(*where))]
    for r in rows:
        r.pop("ingested_at", None)
        r["trade_conditions"] = tuple(r["trade_conditions"] or ())
    return rows


def execution_references(
    conn: Conn, run_id: Any, *, as_of: datetime | None = None
) -> list[ExecutionReference]:
    """The run's scoring references (insert-only). ``as_of`` enforces ``available_at <= as_of``."""
    c = t.execution_references.c
    where = [c.run_id == run_id]
    if as_of is not None:
        _check_ts(as_of)
        where.append(c.available_at <= as_of)
    rows = _reference_rows(conn, t.execution_references, *where)
    return sorted((ExecutionReference.model_validate(r) for r in rows), key=lambda x: x.symbol_ref)


def halt_reference_requests(conn: Conn, run_id: Any) -> list[HaltReferenceRequest]:
    c = t.halt_reference_requests.c
    stmt = select(t.halt_reference_requests).where(c.run_id == run_id).order_by(c.trigger)
    return [_model(HaltReferenceRequest, r._mapping) for r in conn.execute(stmt)]


def pending_halt_requests(conn: Conn) -> list[HaltReferenceRequest]:
    """Requests whose reference rows are not yet complete: no symbol set, or a symbol not marked."""
    req, sets, refs = t.halt_reference_requests, t.halt_reference_symbol_sets, t.halt_references
    stmt = (
        select(req)
        .outerjoin(sets, and_(sets.c.run_id == req.c.run_id, sets.c.trigger == req.c.trigger))
        .where(
            (sets.c.run_id.is_(None))
            | (
                select(func.count())
                .select_from(refs)
                .where(refs.c.run_id == req.c.run_id, refs.c.trigger == req.c.trigger)
                .scalar_subquery()
                < sets.c.symbol_count
            )
        )
        .order_by(req.c.requested_at, req.c.run_id)
    )
    return [_model(HaltReferenceRequest, r._mapping) for r in conn.execute(stmt)]


def halt_symbol_set(conn: Conn, run_id: Any, trigger: str) -> HaltSymbolSet | None:
    c = t.halt_reference_symbol_sets.c
    row = (
        conn.execute(
            select(t.halt_reference_symbol_sets).where(c.run_id == run_id, c.trigger == trigger)
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return _model(HaltSymbolSet, row)


def halt_references(conn: Conn, run_id: Any, trigger: str) -> list[HaltReference]:
    c = t.halt_references.c
    rows = _reference_rows(conn, t.halt_references, c.run_id == run_id, c.trigger == trigger)
    return sorted((HaltReference.model_validate(r) for r in rows), key=lambda x: x.symbol_ref)


# --- corporate actions + delistings (P6.4) ------------------------------------------------------
# Headline readers see ``prospective`` rows only. Backfill evidence is reachable solely through
# ``store.as_of_sensitivity`` (sensitivity/debug), which production decision and scoring code may
# not import (import-linter).

_PROSPECTIVE = (KnowledgeBasis.PROSPECTIVE.value,)


def security_symbols(conn: Conn, security_id: int, as_of: datetime) -> list[SecuritySymbol]:
    """Symbol history knowable at ``as_of``, oldest ``valid_from`` first."""
    _check_ts(as_of)
    c = t.security_symbols.c
    stmt = (
        select(t.security_symbols)
        .where(c.security_id == security_id, c.available_at <= as_of)
        .order_by(c.valid_from, c.available_at, c.symbol)
    )
    return [_model(SecuritySymbol, r._mapping) for r in conn.execute(stmt)]


def symbol_history(conn: Conn, as_of: datetime) -> list[SecuritySymbol]:
    """Every security's symbol history knowable at ``as_of`` (dated identity resolution)."""
    _check_ts(as_of)
    c = t.security_symbols.c
    stmt = (
        select(t.security_symbols)
        .where(c.available_at <= as_of)
        .order_by(c.security_id, c.valid_from, c.symbol)
    )
    return [_model(SecuritySymbol, r._mapping) for r in conn.execute(stmt)]


def identity_conflicts(conn: Conn, as_of: datetime) -> list[IdentityConflict]:
    _check_ts(as_of)
    c = t.identity_conflicts.c
    stmt = select(t.identity_conflicts).where(c.detected_at <= as_of).order_by(c.detected_at)
    return [_model(IdentityConflict, r._mapping) for r in conn.execute(stmt)]


def security_cik(conn: Conn, security_id: int) -> tuple[int | None, int]:
    """The security's CIK and how many securities share it (Form 25 is filed per issuer)."""
    c = t.securities.c
    cik: int | None = conn.execute(select(c.cik).where(c.security_id == security_id)).scalar_one()
    if cik is None:
        return None, 0
    n = conn.execute(select(func.count()).where(c.cik == cik)).scalar_one()
    return int(cik), int(n)


def _corporate_actions(
    conn: Conn,
    security_ids: Iterable[int],
    as_of: datetime,
    bases: Sequence[str],
    process_from: date | None,
    process_to: date | None,
) -> list[CorporateAction]:
    c = t.corporate_actions.c
    where: list[ColumnElement[bool]] = [
        c.security_id.in_(list(security_ids)),
        c.knowledge_basis.in_(list(bases)),
    ]
    if process_from is not None:
        where.append(c.process_date >= process_from)
    if process_to is not None:
        where.append(c.process_date <= process_to)
    rows = _latest(conn, t.corporate_actions, [c.provider, c.provider_action_id], as_of, *where)
    out = [CorporateAction.model_validate(r) for r in rows if not r["withdrawn"]]
    return sorted(out, key=lambda a: (a.process_date, a.provider_action_id))


def corporate_actions(
    conn: Conn,
    security_ids: Iterable[int],
    as_of: datetime,
    *,
    process_from: date | None = None,
    process_to: date | None = None,
) -> list[CorporateAction]:
    """Live prospective actions knowable at ``as_of``: per provider id the latest version first
    observed at or before ``as_of``; an action whose latest version is a withdrawal is omitted."""
    return _corporate_actions(conn, security_ids, as_of, _PROSPECTIVE, process_from, process_to)


def _action_coverage(
    conn: Conn, security_id: int, as_of: datetime, bases: Sequence[str]
) -> list[CorporateActionCoverage]:
    _check_ts(as_of)
    c = t.corporate_action_coverage.c
    stmt = (
        select(t.corporate_action_coverage)
        .where(
            c.security_id == security_id,
            c.established_at <= as_of,
            c.knowledge_basis.in_(list(bases)),
        )
        .order_by(c.range_start, c.established_at)
    )
    return [_model(CorporateActionCoverage, r._mapping) for r in conn.execute(stmt)]


def action_coverage(conn: Conn, security_id: int, as_of: datetime) -> list[CorporateActionCoverage]:
    """Prospective coverage rows established at or before ``as_of`` (``process_date`` ranges)."""
    return _action_coverage(conn, security_id, as_of, _PROSPECTIVE)


def asset_status(conn: Conn, security_id: int, as_of: datetime) -> list[AssetStatusObservation]:
    _check_ts(as_of)
    c = t.asset_status_observations.c
    stmt = (
        select(t.asset_status_observations)
        .where(c.security_id == security_id, c.observed_at <= as_of)
        .order_by(c.observed_at)
    )
    return [_model(AssetStatusObservation, r._mapping) for r in conn.execute(stmt)]


def delisting_filings(conn: Conn, cik: int, as_of: datetime) -> list[DelistingFiling]:
    _check_ts(as_of)
    c = t.delisting_filings.c
    stmt = (
        select(t.delisting_filings)
        .where(c.cik == cik, c.available_at <= as_of)
        .order_by(c.filing_date, c.accession)
    )
    return [_model(DelistingFiling, r._mapping) for r in conn.execute(stmt)]


def bar_dates(conn: Conn, security_id: int, as_of: datetime) -> list[date]:
    """Eastern session dates of every bar knowable at ``as_of`` (any feed), ascending."""
    _check_ts(as_of)
    c = t.price_bars.c
    stmt = (
        select(c.event_time)
        .where(c.security_id == security_id, c.available_at <= as_of)
        .distinct()
        .order_by(c.event_time)
    )
    et = ZoneInfo("America/New_York")
    stamps: list[datetime] = list(conn.execute(stmt).scalars())
    return sorted({ts.astimezone(et).date() for ts in stamps})


def _delisting_history(
    conn: Conn, security_id: int, as_of: datetime, bases: Sequence[str]
) -> list[Delisting]:
    _check_ts(as_of)
    c = t.delistings.c
    stmt = (
        select(t.delistings)
        .where(
            c.security_id == security_id,
            c.available_at <= as_of,
            c.knowledge_basis.in_(list(bases)),
        )
        .order_by(c.available_at)
    )
    rows = []
    for r in conn.execute(stmt):
        d = {k: v for k, v in dict(r._mapping).items() if k != "ingested_at"}
        d["evidence"] = tuple(d["evidence"])
        rows.append(Delisting.model_validate(d))
    return rows


def delisting(conn: Conn, security_id: int, as_of: datetime) -> Delisting | None:
    """The latest prospective listing conclusion derived at or before ``as_of``."""
    hist = delisting_history(conn, security_id, as_of)
    return hist[-1] if hist else None


def delisting_history(conn: Conn, security_id: int, as_of: datetime) -> list[Delisting]:
    """Every prospective conclusion knowable at ``as_of``, oldest first (superseded defaults
    stay visible)."""
    return _delisting_history(conn, security_id, as_of, _PROSPECTIVE)
