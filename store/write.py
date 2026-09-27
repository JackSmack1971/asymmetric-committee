"""Ingest-side writes. Every fact insert is ``ON CONFLICT DO NOTHING`` on natural key +
``source_version``, so re-running an ingestor is a no-op and existing versions are never changed.
New information (a restatement, a revised article, a SIP bar) is always a new row."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import (
    Connection,
    Table,
    and_,
    delete,
    func,
    literal,
    not_,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import insert

from contracts.commitment import CommitmentIntegrityError, CommitmentMaterial
from contracts.data import (
    FeatureRow,
    FundamentalFact,
    InsiderTxn,
    NewsItem,
    PriceBar,
    UniverseMember,
)
from contracts.enums import (
    HALT_REASON_PREFIX,
    TERMINAL_ORDER_STATUSES,
    AgentName,
    FeedName,
    RunMode,
    RunStatus,
    SecurityKind,
    halt_reason,
)
from contracts.errors import (
    AnchorIncompleteError,
    ImmutableConflictError,
    ResetRefusedError,
    RunHaltedError,
)
from contracts.market_data import (
    CalendarCoverage,
    ExecutionReference,
    HaltReference,
    HaltReferenceRequest,
    HaltSymbolSet,
    TBillObservation,
    TBillVintageCoverage,
    TradingSession,
    calendar_hash,
)
from contracts.models import (
    AgentVerdict,
    CommitmentAnchor,
    CommitteeDecisionRecord,
    DecisionCommitment,
    DlqRecord,
    ExecutionRecord,
    GateDecision,
    KillSwitchEvent,
    PortfolioSnapshot,
    ProposedBook,
    RedTeamVerdict,
    RunRecord,
    VerdictRecord,
)
from store import _tables as t
from store._material import decisions_from_rows, snapshot_from_row

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


def ensure_reference_instrument(
    conn: Connection, *, ticker: str, name: str, kind: SecurityKind = SecurityKind.ETF
) -> int:
    """Create or refresh SPY / a sector ETF (kind ``etf``, no CIK) and return its id."""
    if kind is SecurityKind.EQUITY:
        raise ValueError("a reference instrument is not an equity; use ensure_security")
    stmt = insert(t.securities).values(ticker=ticker, cik=None, name=name, kind=kind.value)
    upsert = stmt.on_conflict_do_update(
        index_elements=[t.securities.c.ticker],
        index_where=t.securities.c.kind != SecurityKind.EQUITY.value,
        set_={"name": stmt.excluded.name},
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
    stmt = select(c.ticker, c.security_id).where(c.kind == SecurityKind.EQUITY.value)
    if tickers is not None:
        stmt = stmt.where(c.ticker.in_(list(tickers)))
    return {tk: sid for tk, sid in conn.execute(stmt).tuples()}


def security_ids_by_cik(conn: Connection) -> Mapping[int, list[int]]:
    out: dict[int, list[int]] = {}
    stmt = select(t.securities.c.cik, t.securities.c.security_id).where(
        t.securities.c.kind == SecurityKind.EQUITY.value
    )
    for cik, sid in conn.execute(stmt).tuples():
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


_ADVANCED = (
    RunStatus.COMMITTED.value,
    RunStatus.ANCHORED.value,
    RunStatus.EXECUTED.value,
    RunStatus.SCORED.value,
)
_REGRESSIVE = (RunStatus.PENDING.value, RunStatus.PARTIAL.value, RunStatus.FAILED.value)
# The only states the audited halt transition may leave (§9, §11).
_HALTABLE = (RunStatus.COMMITTED.value, RunStatus.ANCHORED.value, RunStatus.EXECUTED.value)


def upsert_run(conn: Connection, run: RunRecord) -> None:
    """Create the run or move it forward: status, end time, reason and cost are the mutable part.

    A run that already reached ``COMMITTED`` or later is never demoted to ``PENDING``, ``PARTIAL``
    or ``FAILED``: a late failure report (or a duplicate worker) cannot un-commit a book. A run a
    kill-switch halt moved to ``PARTIAL`` is terminal: nothing overwrites it (recovery = new run).
    """
    stmt = insert(t.runs).values(_row(run, t.runs))
    ex, c = stmt.excluded, t.runs.c
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=[c.run_id],
            set_={
                "status": ex.status,
                "ended_at": ex.ended_at,
                "status_reason": ex.status_reason,
                "total_cost_usd": ex.total_cost_usd,
            },
            where=not_(
                or_(
                    and_(c.status.in_(_ADVANCED), ex.status.in_(_REGRESSIVE)),
                    and_(
                        c.status == RunStatus.PARTIAL.value,
                        c.status_reason.startswith(HALT_REASON_PREFIX, autoescape=True),
                    ),
                )
            ),
        )
    )


def load_run(conn: Connection, run_id: UUID) -> RunRecord | None:
    row = conn.execute(select(t.runs).where(t.runs.c.run_id == run_id)).one_or_none()
    return None if row is None else RunRecord.model_validate(dict(row._mapping))


def load_verdicts(conn: Connection, run_id: UUID) -> list[VerdictRecord]:
    """Stored verdicts of a run. A row exists only for work that COMPLETED (invariant 8)."""
    c = t.agent_verdicts.c
    out: list[VerdictRecord] = []
    for r in conn.execute(select(t.agent_verdicts).where(c.run_id == run_id).order_by(c.agent)):
        m = r._mapping
        model = RedTeamVerdict if m["agent"] == AgentName.RED_TEAM.value else AgentVerdict
        out.append(
            VerdictRecord(
                security_id=m["security_id"],
                verdict=model.model_validate(m["verdict"]),
                tokens_in=m["tokens_in"],
                tokens_out=m["tokens_out"],
                cost_usd=m["cost_usd"],
                latency_ms=m["latency_ms"],
            )
        )
    return out


def load_book(conn: Connection, run_id: UUID) -> ProposedBook | None:
    """The committed final book of a run, if one was stored."""
    c = t.portfolio_snapshots.c
    raw = conn.execute(select(c.book).where(c.run_id == run_id)).scalar_one_or_none()
    return None if raw is None else ProposedBook.model_validate(raw)


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
                "entity_token": d.entity_token,
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


def load_kill_switch_events(conn: Connection, run_id: UUID) -> list[KillSwitchEvent]:
    """Every stored halt of a run, oldest first: the durable source of the kill-switch state."""
    c = t.kill_switch_events.c
    rows = conn.execute(
        select(t.kill_switch_events).where(c.run_id == run_id).order_by(c.triggered_at, c.event_id)
    ).mappings()
    return [
        KillSwitchEvent.model_validate(
            {**{k: r[k] for k in KillSwitchEvent.model_fields if k in r}}
            | {"cancelled_order_ids": tuple(r["cancelled_order_ids"])}
        )
        for r in rows
    ]


def load_commitment(conn: Connection, run_id: UUID) -> DecisionCommitment | None:
    c = t.decision_commitments
    row = conn.execute(select(c).where(c.c.run_id == run_id)).mappings().first()
    return None if row is None else DecisionCommitment.model_validate(dict(row))


def load_commitment_hash(conn: Connection, run_id: UUID) -> str | None:
    c = t.decision_commitments.c
    return conn.execute(select(c.sha256).where(c.run_id == run_id)).scalar_one_or_none()


def load_committed_at(conn: Connection, run_id: UUID) -> datetime | None:
    """When the run's commitment was recorded (immutable; the start of the reference waits)."""
    c = t.decision_commitments.c
    return conn.execute(select(c.committed_at).where(c.run_id == run_id)).scalar_one_or_none()


def load_snapshot(conn: Connection, run_id: UUID) -> PortfolioSnapshot | None:
    """The committed final book with its CIO decision, if one was stored."""
    c = t.portfolio_snapshots.c
    row = conn.execute(select(t.portfolio_snapshots).where(c.run_id == run_id)).mappings().first()
    return None if row is None else snapshot_from_row(dict(row))


def advance_to_executed(conn: Connection, run_id: UUID, ended_at: datetime) -> bool:
    """``ANCHORED -> EXECUTED`` as one conditional update. True only for the call that moved it,
    so a replay or a second worker cannot advance (or report advancing) the run twice."""
    c = t.runs.c
    result = conn.execute(
        update(t.runs)
        .where(c.run_id == run_id, c.status == RunStatus.ANCHORED.value)
        .values(status=RunStatus.EXECUTED.value, ended_at=ended_at, status_reason=None)
    )
    return result.rowcount == 1


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


# --- integrity, anchoring, halts, claims, resets (P5 step 5) -----------------------------------


def load_anchor(conn: Connection, run_id: UUID) -> CommitmentAnchor | None:
    c = t.commitment_anchors.c
    row = conn.execute(select(t.commitment_anchors).where(c.run_id == run_id)).mappings().first()
    return None if row is None else CommitmentAnchor.model_validate(dict(row))


def load_decisions(conn: Connection, run: RunRecord) -> list[CommitteeDecisionRecord]:
    """Stored committee decisions rebuilt as the records that were hashed, or raise."""
    c = t.committee_decisions.c
    query = (
        select(t.committee_decisions)
        .where(c.run_id == run.run_id)
        .order_by(c.security_id, c.horizon)
    )
    return decisions_from_rows(run, [dict(r) for r in conn.execute(query).mappings()])


def load_commitment_material(conn: Connection, run_id: UUID) -> CommitmentMaterial | None:
    """Rows needed to recompute a commitment; ``None`` when the run has no commitment row.

    Call inside one REPEATABLE READ transaction so run, decisions, book and commitment are one
    consistent view.
    """
    run = load_run(conn, run_id)
    commitment = load_commitment(conn, run_id)
    if run is None or commitment is None:
        return None
    try:  # rows that no longer validate are as untrustworthy as rows that hash differently
        snapshot = load_snapshot(conn, run_id)
        if snapshot is None:
            raise CommitmentIntegrityError(f"run {run_id} is committed but has no stored book")
        return CommitmentMaterial(
            run=run,
            decisions=tuple(load_decisions(conn, run)),
            snapshot=snapshot,
            stored_sha256=commitment.sha256,
            committed_at=commitment.committed_at,
            anchor=load_anchor(conn, run_id),
        )
    except CommitmentIntegrityError:
        raise
    except ValueError as exc:
        raise CommitmentIntegrityError(f"run {run_id}: stored rows are invalid: {exc}") from exc


def advance_to_anchored(conn: Connection, anchor: CommitmentAnchor) -> bool:
    """Store the anchor and move ``COMMITTED -> ANCHORED`` in the caller's transaction.

    True only for the call that made the transition. The composite foreign key guarantees the
    anchor names this run's own commitment hash.
    """
    if anchor.ots_proof is None or anchor.git_commit is None:
        raise AnchorIncompleteError("ANCHORED needs an OpenTimestamps proof and a git commit")
    upsert_anchor(conn, anchor)
    c = t.runs.c
    result = conn.execute(
        update(t.runs)
        .where(c.run_id == anchor.run_id, c.status == RunStatus.COMMITTED.value)
        .values(status=RunStatus.ANCHORED.value)
    )
    return result.rowcount == 1


def record_halt(conn: Connection, event: KillSwitchEvent) -> bool:
    """The audited halt transition (§9): event row and ``-> PARTIAL`` in one transaction.

    Only ``COMMITTED``, ``ANCHORED`` and ``EXECUTED`` runs move; every evidence row is kept.
    True when this call moved the run.
    """
    insert_kill_switch_events(conn, (event,))
    insert_halt_reference_requests(
        conn,
        (
            HaltReferenceRequest(
                run_id=event.run_id,
                trigger=event.trigger,
                tau=event.triggered_at,
                requested_at=event.triggered_at,
                source_version=HALT_REQUEST_VERSION,
            ),
        ),
    )
    c = t.runs.c
    result = conn.execute(
        update(t.runs)
        .where(c.run_id == event.run_id, c.status.in_(_HALTABLE))
        .values(
            status=RunStatus.PARTIAL.value,
            status_reason=halt_reason(event.trigger),
            ended_at=event.triggered_at,
        )
    )
    return result.rowcount == 1


_ACTIVE = (
    RunStatus.PENDING.value,
    RunStatus.INGEST_OK.value,
    RunStatus.FEATURES_OK.value,
    RunStatus.GATED.value,
    RunStatus.AGENTS_OK.value,
    RunStatus.COMMITTED.value,
    RunStatus.ANCHORED.value,
    RunStatus.EXECUTED.value,
    RunStatus.SCORED.value,
)


def claim_run(
    conn: Connection,
    *,
    mode: RunMode,
    as_of: datetime,
    config_hash: str,
    run_id: UUID,
    started_at: datetime,
    fresh: bool = False,
) -> tuple[RunRecord, bool]:
    """One run per ``(mode, as_of, config)`` however many workers ask (Postgres advisory lock).

    Returns the newest run that is in progress or finished, else creates a ``PENDING`` run with
    ``run_id``. A failed or budget-``PARTIAL`` run never blocks a new one. A run halted by the
    kill switch does: a new run for that ``as_of`` needs ``fresh=True`` (an operator decision).
    """
    key = f"{mode.value}|{as_of.astimezone(UTC).isoformat()}|{config_hash}"
    conn.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))
    c = t.runs.c
    rows = (
        conn.execute(
            select(t.runs)
            .where(c.mode == mode.value, c.as_of == as_of, c.config_hash == config_hash)
            .order_by(c.started_at.desc())
        )
        .mappings()
        .all()
    )
    runs = [RunRecord.model_validate(dict(r)) for r in rows]
    for r in runs:
        if r.status.value in _ACTIVE:
            return r, False
    if not fresh and any((r.status_reason or "").startswith(HALT_REASON_PREFIX) for r in runs):
        raise RunHaltedError(f"a run for {as_of.isoformat()} was halted by the kill switch")
    run = RunRecord(
        run_id=run_id,
        mode=mode,
        as_of=as_of,
        config_hash=config_hash,
        status=RunStatus.PENDING,
        started_at=started_at,
    )
    insert_run(conn, run)
    return run, True


def list_runs(conn: Connection, *, mode: RunMode, statuses: Sequence[RunStatus]) -> list[RunRecord]:
    c = t.runs.c
    rows = conn.execute(
        select(t.runs)
        .where(c.mode == mode.value, c.status.in_([s.value for s in statuses]))
        .order_by(c.as_of, c.started_at)
    ).mappings()
    return [RunRecord.model_validate(dict(r)) for r in rows]


def anchors_awaiting_confirmation(conn: Connection) -> list[CommitmentAnchor]:
    """Anchors with an OTS proof that no Bitcoin attestation has confirmed yet."""
    c = t.commitment_anchors.c
    rows = conn.execute(
        select(t.commitment_anchors)
        .where(c.verified_at.is_(None), c.ots_proof.is_not(None))
        .order_by(c.anchored_at)
    ).mappings()
    return [CommitmentAnchor.model_validate(dict(r)) for r in rows]


def _audit_json(rows: Any) -> list[dict[str, Any]]:
    return [json.loads(json.dumps(dict(r), default=str)) for r in rows]


def reset_run(
    conn: Connection, run_id: UUID, *, actor: str, reason: str, now: datetime
) -> dict[str, int]:
    """Operator reset of a run that never committed: keep what was cleared, then start over.

    Refused (``ResetRefusedError``) once the run has a commitment, an anchor, an order or a halt
    event: those are audit evidence and are never cleared. Verdicts, decisions and the snapshot
    move into the append-only ``run_resets`` row; DLQ rows and the accumulated cost stay.
    """
    r = t.runs.c
    row = (
        conn.execute(select(t.runs).where(r.run_id == run_id).with_for_update()).mappings().first()
    )
    if row is None:
        raise ResetRefusedError(f"run {run_id} does not exist")
    for label, table in (
        ("a commitment", t.decision_commitments),
        ("an anchor", t.commitment_anchors),
        ("orders", t.orders),
        ("a kill-switch event", t.kill_switch_events),
    ):
        if conn.execute(select(literal(1)).where(table.c.run_id == run_id).limit(1)).first():
            raise ResetRefusedError(f"run {run_id} has {label}; a committed run is immutable")
    cleared = {
        table.name: _audit_json(
            conn.execute(select(table).where(table.c.run_id == run_id)).mappings()
        )
        for table in (t.agent_verdicts, t.committee_decisions, t.portfolio_snapshots)
    }
    conn.execute(
        insert(t.run_resets).values(
            run_id=run_id,
            reset_at=now,
            actor=actor,
            reason=reason,
            prior_status=row["status"],
            cleared=cleared,
        )
    )
    for table in (t.agent_verdicts, t.committee_decisions, t.portfolio_snapshots):
        conn.execute(delete(table).where(table.c.run_id == run_id))
    conn.execute(
        update(t.runs)
        .where(r.run_id == run_id)
        .values(status=RunStatus.PENDING.value, status_reason=None, ended_at=None)
    )
    return {k: len(v) for k, v in cleared.items()}


def load_open_executions(conn: Connection, run_id: UUID) -> list[ExecutionRecord]:
    """Stored orders of a run that are not yet known to be terminal at the broker."""
    o = t.orders.c
    terminal = [s.value for s in TERMINAL_ORDER_STATUSES]
    rows = conn.execute(
        select(t.orders)
        .where(o.run_id == run_id, o.status.not_in(terminal))
        .order_by(o.submitted_at)
    ).mappings()
    return [
        ExecutionRecord.model_validate({k: r[k] for k in ExecutionRecord.model_fields if k in r})
        for r in rows
    ]


def run_ids_with_open_orders(conn: Connection) -> list[UUID]:
    """Live runs whose stored orders still look working: candidates for broker reconciliation."""
    o = t.orders.c
    terminal = [s.value for s in TERMINAL_ORDER_STATUSES]
    rows = conn.execute(
        select(o.run_id).where(o.status.not_in(terminal)).distinct().order_by(o.run_id)
    )
    return [r[0] for r in rows]


# --- execution evidence (P5 step 4) -----------------------------------------------------------

_EXECUTION_MUTABLE = (
    "filled_qty", "status", "fill_price", "slippage_bps", "filled_at", "submitted_at",
)  # fmt: skip


def upsert_execution_records(conn: Connection, records: Sequence[ExecutionRecord]) -> None:
    """One ``orders`` row per broker order. Broker-reported state moves forward on replay; the
    reference price and source recorded at first sight are never overwritten."""
    for r in records:
        row = {
            "run_id": r.run_id,
            "broker_order_id": r.broker_order_id,
            "security_id": r.security_id,
            "side": r.side.value,
            "qty": r.qty,
            "limit_price": r.limit_price,
            "status": r.status.value,
            "submitted_at": r.submitted_at,
            "client_order_id": r.client_order_id,
            "kind": r.kind.value,
            "filled_qty": r.filled_qty,
            "decision_price": r.decision_price,
            "reference_price": r.reference_price,
            "reference_source": r.reference_source.value,
            "fill_price": r.fill_price,
            "slippage_bps": r.slippage_bps,
            "filled_at": r.filled_at,
        }
        stmt = insert(t.orders).values(row)
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[t.orders.c.broker_order_id],
                set_={c: getattr(stmt.excluded, c) for c in _EXECUTION_MUTABLE},
            )
        )


def load_execution_record(conn: Connection, client_order_id: str) -> ExecutionRecord | None:
    o = t.orders.c
    query = select(t.orders).where(o.client_order_id == client_order_id)
    row = conn.execute(query).mappings().one_or_none()
    if row is None:
        return None
    return ExecutionRecord.model_validate(
        {k: row[k] for k in ExecutionRecord.model_fields if k in row}
    )


# --- market-data foundations (P6.3): immutable, conflict-checked writers ---------------------

HALT_REQUEST_VERSION = "halt_request_v1"

# Columns that record when *we* wrote or re-fetched a row, not what the row says. A replay of the
# same evidence carries a new value here, which is not a contradiction; the first value stands.
_WRITE_TIME = frozenset({"ingested_at", "available_at", "established_at", "resolved_at"})


def _canonical(value: Any) -> Any:
    """One comparable form for a value, whether it came from Python or from the database."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetime in an immutable payload")
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


def _insert_immutable(
    conn: Connection,
    table: Table,
    models: Sequence[BaseModel],
    key_cols: Sequence[str],
) -> int:
    """Insert-only writer: exact replay is a no-op, a contradicting replay raises.

    ``ON CONFLICT DO NOTHING`` alone would hide two different payloads under one natural key. Every
    presented row is therefore read back by key and compared, after canonicalising both sides,
    on all columns except the ones that only record when it was written.
    """
    rows = [_row(m, table) for m in models]
    if not rows:
        return 0
    payload_cols = [c.name for c in table.c if c.name not in key_cols and c.name not in _WRITE_TIME]
    inserted = 0
    for i in range(0, len(rows), _CHUNK):
        chunk = rows[i : i + _CHUNK]
        stmt = insert(table).values(chunk).on_conflict_do_nothing().returning(literal(1))
        inserted += len(conn.execute(stmt).all())
        keys = [tuple(r[k] for k in key_cols) for r in chunk]
        stored = {
            tuple(row[k] for k in key_cols): row
            for row in conn.execute(
                select(table).where(tuple_(*[table.c[k] for k in key_cols]).in_(keys))
            )
            .mappings()
            .all()
        }
        for r in chunk:
            key = tuple(r[k] for k in key_cols)
            existing = stored[key]
            for col in payload_cols:
                if col not in r:
                    continue
                if _canonical(existing[col]) != _canonical(r[col]):
                    raise ImmutableConflictError(
                        f"{table.name} {dict(zip(key_cols, key, strict=True))}: {col} is "
                        f"{existing[col]!r} but {r[col]!r} was presented"
                    )
    return inserted


def insert_calendar_range(
    conn: Connection, sessions: Sequence[TradingSession], coverage: CalendarCoverage
) -> int:
    """Sessions and the coverage record that vouches for them, in one savepoint.

    ``coverage.sessions_sha256`` must be `calendar_hash` of exactly ``sessions``, so a revised
    session set cannot be stored without a new coverage record.
    """
    if coverage.session_count != len(sessions) or coverage.sessions_sha256 != calendar_hash(
        sessions
    ):
        raise ValueError("coverage does not describe the sessions written with it")
    for s in sessions:
        if not coverage.range_start <= s.session_date <= coverage.range_end:
            raise ValueError(f"session {s.session_date} is outside the covered range")
        if s.available_at != coverage.available_at:
            raise ValueError("sessions and their coverage share one available_at")
    with conn.begin_nested():
        n = _insert_immutable(
            conn, t.trading_calendar, sessions, ("session_date", "source_version")
        )
        _insert_immutable(
            conn,
            t.calendar_coverage,
            [coverage],
            ("source", "range_start", "range_end", "source_version"),
        )
    return n


def insert_tbill_rates(conn: Connection, rows: Sequence[TBillObservation]) -> int:
    return _insert_immutable(
        conn, t.tbill_rates, rows, ("series", "observation_date", "vintage_date")
    )


def insert_tbill_vintage_coverage(conn: Connection, row: TBillVintageCoverage) -> int:
    return _insert_immutable(conn, t.tbill_vintage_coverage, [row], ("series", "source_version"))


def insert_execution_references(conn: Connection, rows: Sequence[ExecutionReference]) -> int:
    return _insert_immutable(conn, t.execution_references, rows, ("run_id", "symbol_ref"))


def insert_halt_reference_requests(conn: Connection, rows: Sequence[HaltReferenceRequest]) -> int:
    return _insert_immutable(conn, t.halt_reference_requests, rows, ("run_id", "trigger"))


def insert_halt_symbol_set(conn: Connection, row: HaltSymbolSet) -> int:
    return _insert_immutable(conn, t.halt_reference_symbol_sets, [row], ("run_id", "trigger"))


def insert_halt_references(conn: Connection, rows: Sequence[HaltReference]) -> int:
    return _insert_immutable(conn, t.halt_references, rows, ("run_id", "trigger", "symbol_ref"))
