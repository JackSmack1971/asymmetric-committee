"""Minimal no-LLM weekly walk-forward coordinator (§12.2)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid5

from agents.quant_baseline import decisions as baseline_decisions
from config.loader import RiskConfig
from contracts.data import FeatureRow
from contracts.enums import RunMode, RunStatus
from contracts.models import GateDecision, ProposedBook, RunRecord
from gate.model import GateLabel, decide, eligible_training_set, fit_logistic
from risk import size_book
from store.as_of import Conn
from store.write import insert_features, insert_gate_decisions, insert_proposed_book, insert_run

RUN_NAMESPACE = UUID("f0b92b18-8ea9-489e-8c37-256b7315eb73")


@dataclass(frozen=True)
class WalkForwardInputs:
    rows: tuple[FeatureRow, ...]
    training_features: tuple[FeatureRow, ...]
    labels: tuple[GateLabel, ...]
    sectors: Mapping[int, str]
    volatilities: Mapping[int, float]
    entity_tokens: Mapping[int, str]


@dataclass(frozen=True)
class WalkForwardResult:
    book: ProposedBook
    gate_decisions: tuple[GateDecision, ...]
    feature_rows: tuple[FeatureRow, ...]
    config_hash: str


def deterministic_run_id(as_of: datetime, config_hash: str) -> UUID:
    return uuid5(RUN_NAMESPACE, f"{as_of.isoformat()}:{config_hash}")


def run_date(
    *,
    as_of: datetime,
    inputs: WalkForwardInputs,
    risk_config: RiskConfig,
    gate_k: int,
    config_hash: str = "fixture",
) -> WalkForwardResult:
    """Run one independent date; all supplied rows must originate from as-of reads."""
    run_id = deterministic_run_id(as_of, config_hash)
    training = eligible_training_set(inputs.training_features, inputs.labels, as_of)
    gate = decide(
        model=fit_logistic(training), rows=inputs.rows, run_id=run_id, as_of=as_of, top_k=gate_k
    )
    passed = {decision.security_id for decision in gate if decision.passed}
    candidates = tuple(row for row in inputs.rows if row.security_id in passed)
    decisions = baseline_decisions(
        rows=candidates, run_id=run_id, as_of=as_of, entity_tokens=inputs.entity_tokens
    )
    book = (
        size_book(
            decisions=decisions,
            sectors=inputs.sectors,
            volatilities=inputs.volatilities,
            config=risk_config,
        )
        if decisions
        else ProposedBook(run_id=run_id, as_of=as_of, positions=())
    )
    return WalkForwardResult(book, gate, inputs.rows, config_hash)


def persist_result(conn: Conn, result: WalkForwardResult) -> None:
    """Atomically persist a run's features, full shadow log, and proposed book."""
    run = RunRecord(
        run_id=result.book.run_id,
        mode=RunMode.BACKTEST,
        as_of=result.book.as_of,
        config_hash=result.config_hash,
        status=RunStatus.GATED,
        started_at=result.book.as_of,
        ended_at=result.book.as_of,
    )
    insert_run(conn, run)
    insert_features(conn, result.feature_rows)
    insert_gate_decisions(conn, result.gate_decisions)
    insert_proposed_book(conn, result.book)


def run_walkforward(
    *,
    dates: Sequence[datetime],
    load: Callable[[datetime], WalkForwardInputs],
    persist: Callable[[WalkForwardResult], None],
    risk_config: RiskConfig,
    gate_k: int,
    config_material: bytes = b"fixture",
) -> tuple[WalkForwardResult, ...]:
    """Run and persist exactly one book and complete shadow log per weekly date."""
    config_hash = hashlib.sha256(config_material).hexdigest()
    results = []
    for as_of in dates:
        result = run_date(
            as_of=as_of,
            inputs=load(as_of),
            risk_config=risk_config,
            gate_k=gate_k,
            config_hash=config_hash,
        )
        persist(result)
        results.append(result)
    return tuple(results)
