"""Ingest-side writes. Every fact insert is ``ON CONFLICT DO NOTHING`` on natural key +
``source_version``, so re-running an ingestor is a no-op and existing versions are never changed.
New information (a restatement, a revised article, a SIP bar) is always a new row."""

from __future__ import annotations

import hashlib
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
from contracts.models import (
    CommitmentAnchor,
    CommitteeDecisionRecord,
    DecisionCommitment,
    DlqRecord,
    GateDecision,
    KillSwitchEvent,
    PortfolioSnapshot,
    ProposedBook,
    RunRecord,
    VerdictRecord,
)
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


def insert_run(conn: Connection, run: RunRecord) -> int:
    return _insert(conn, t.runs, [run])


def insert_gate_decisions(conn: Connection, rows: Sequence[GateDecision]) -> int:
    """Persist passed and dropped candidates: the gate's complete shadow log."""
    return _insert(conn, t.gate_decisions, rows)


def insert_proposed_book(conn: Connection, book: ProposedBook) -> int:
    stmt = (
        insert(t.proposed_books)
        .values(run_id=book.run_id, as_of=book.as_of, book=book.model_dump(mode="json"))
        .on_conflict_do_nothing()
        .returning(literal(1))
    )
    return len(conn.execute(stmt).all())


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


# --- run outputs (called only from orchestration/sink.py) -------------------------------------


class CommitmentMismatchError(ValueError):
    """A commitment for this run already exists with a different hash (invariant 5)."""


class AnchorMismatchError(ValueError):
    """An anchor for this run already exists with a different commitment hash."""


def _dedupe_key(model: BaseModel) -> str:
    return hashlib.sha256(model.model_dump_json().encode()).hexdigest()


def upsert_run(conn: Connection, run: RunRecord) -> None:
    """Create the run or move it forward: status, end time, reason and cost are the mutable part."""
    stmt = insert(t.runs).values(_row(run, t.runs))
    ex = stmt.excluded
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=[t.runs.c.run_id],
            set_={
                "status": ex.status,
                "ended_at": ex.ended_at,
                "status_reason": ex.status_reason,
                "total_cost_usd": ex.total_cost_usd,
            },
        )
    )


def _insert_rows(conn: Connection, table: Table, rows: Sequence[dict[str, Any]]) -> int:
    if not rows:
        return 0
    stmt = insert(table).values(list(rows)).on_conflict_do_nothing().returning(literal(1))
    return len(conn.execute(stmt).all())


def insert_agent_verdicts(conn: Connection, records: Sequence[VerdictRecord]) -> int:
    return _insert_rows(
        conn,
        t.agent_verdicts,
        [
            {
                "run_id": r.verdict.run_id,
                "security_id": r.security_id,
                "agent": r.verdict.agent.value,
                "verdict": r.verdict.model_dump(mode="json"),
                "tokens_in": r.tokens_in,
                "tokens_out": r.tokens_out,
                "cost_usd": r.cost_usd,
                "latency_ms": r.latency_ms,
            }
            for r in records
        ],
    )


def insert_committee_decisions(conn: Connection, records: Sequence[CommitteeDecisionRecord]) -> int:
    rows = []
    for r in records:
        d = r.decision
        rows.append(
            {
                "run_id": d.run_id,
                "security_id": d.security_id,
                "horizon": int(d.horizon_days),
                "pooled_p": d.pooled_p,
                "pooled_logit": r.pooled_logit,
                "dispersion": d.dispersion,
                "sizing_mode": r.sizing_mode.value if r.sizing_mode else None,
                "bear_severity": d.bear_severity.value if d.bear_severity else None,
                "weights": [w.model_dump(mode="json") for w in d.agent_weights],
                "target_weight": d.target_weight,
                "cio_action": r.cio_action.value if r.cio_action else None,
                "rationale": r.rationale,
            }
        )
    return _insert_rows(conn, t.committee_decisions, rows)


def insert_portfolio_snapshot(conn: Connection, snap: PortfolioSnapshot) -> int:
    return _insert_rows(
        conn,
        t.portfolio_snapshots,
        [
            {
                "run_id": snap.run_id,
                "as_of": snap.as_of,
                "cash_weight": snap.cash_weight,
                "book": snap.book.model_dump(mode="json"),
                "cio": snap.cio.model_dump(mode="json") if snap.cio else None,
            }
        ],
    )


def insert_dlq_records(conn: Connection, records: Sequence[DlqRecord]) -> int:
    return _insert_rows(
        conn,
        t.dlq_records,
        [
            {
                "run_id": r.run_id,
                "as_of": r.as_of,
                "agent": r.agent,
                "error_type": r.error_type,
                "payload": r.payload,
                "dedupe_key": _dedupe_key(r),
            }
            for r in records
        ],
    )


def insert_kill_switch_events(conn: Connection, events: Sequence[KillSwitchEvent]) -> int:
    return _insert_rows(
        conn,
        t.kill_switch_events,
        [
            {
                "run_id": e.run_id,
                "triggered_at": e.triggered_at,
                "trigger": e.trigger.value,
                "daily_loss": e.daily_loss,
                "peak_drawdown": e.peak_drawdown,
                "cancelled_order_ids": list(e.cancelled_order_ids),
                "flattened": e.flattened,
                "dedupe_key": _dedupe_key(e),
            }
            for e in events
        ],
    )


def insert_commitment(conn: Connection, commitment: DecisionCommitment) -> int:
    """Append-only (trigger). Replaying the same hash is a no-op; a different hash raises."""
    n = _insert(conn, t.decision_commitments, [commitment])
    if n == 0:
        c = t.decision_commitments.c
        stored: str = conn.execute(
            select(c.sha256).where(c.run_id == commitment.run_id)
        ).scalar_one()
        if stored != commitment.sha256:
            raise CommitmentMismatchError(f"run {commitment.run_id} is already committed")
    return n


def upsert_anchor(conn: Connection, anchor: CommitmentAnchor) -> None:
    """Record or upgrade an anchor. The hash is fixed; a different hash for the run raises."""
    base = insert(t.commitment_anchors).values(_row(anchor, t.commitment_anchors))
    ex, c = base.excluded, t.commitment_anchors.c
    stmt = base.on_conflict_do_update(
        index_elements=[c.run_id],
        set_={
            "ots_proof": func.coalesce(ex.ots_proof, c.ots_proof),
            "git_commit": func.coalesce(ex.git_commit, c.git_commit),
            "verified_at": func.coalesce(ex.verified_at, c.verified_at),
        },
        where=c.sha256 == ex.sha256,
    ).returning(literal(1))
    if not conn.execute(stmt).all():
        raise AnchorMismatchError(f"run {anchor.run_id} is already anchored to a different hash")
