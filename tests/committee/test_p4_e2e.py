"""P4 end to end: verdicts, pooling, stacker, sizing, CIO (§7, §8.1, §8.2; invariants 6, 7)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from agents.base import ChatResponse, Message
from agents.cio import CioMiscalibrationAlert, apply_cio_decision, run_cio
from committee.pooling import pool_agent_verdicts
from committee.stacker import Observation, fit_stacker, sigmoid
from config.loader import RiskConfig, load_config
from contracts.enums import (
    VOTING_AGENTS,
    BearSeverity,
    DataSufficiency,
    FeedName,
    Horizon,
    ModelTier,
    Stance,
)
from contracts.models import (
    AgentVerdict,
    CalibrationFit,
    CioDecisionLLM,
    EvidenceRef,
    PooledForecast,
    ProposedBook,
)
from risk import size_committee_book

CONFIG = load_config(allow_placeholders=True, env={})
RISK: RiskConfig = CONFIG.risk
COMMITTEE = CONFIG.pipeline.committee
MODELS = CONFIG.models
STRONG = MODELS.tiers[ModelTier.STRONG].primary
AGENTS = sorted(VOTING_AGENTS, key=lambda a: a.value)
RUN: UUID = uuid4()
AS_OF = datetime(2026, 1, 30, tzinfo=UTC)
H = Horizon.D21
BASE = {Horizon.D5: 0.5, Horizon.D21: 0.5, Horizon.D63: 0.5}
N = 12


def _token(sid: int) -> str:
    return f"TICKER_{sid:02d}"


def _verdicts(sid: int, p: float) -> list[AgentVerdict]:
    """Five agents that disagree slightly around ``p`` for every horizon."""
    return [
        AgentVerdict(
            stance=Stance.BUY,
            p_outperform_5=p + 0.01 * i,
            p_outperform_21=p + 0.01 * i,
            p_outperform_63=p + 0.01 * i,
            key_evidence=(EvidenceRef(source=FeedName.FEATURES, row_id="f1", note="x"),),
            risks=(),
            data_sufficiency=DataSufficiency.FULL,
            run_id=RUN,
            agent=agent,
            entity_token=_token(sid),
            as_of=AS_OF,
            prompt_version="v1-test",
            model_served="test/model",
            valid=True,
        )
        for i, agent in enumerate(AGENTS)
    ]


def _pooled(probabilities: dict[int, float]) -> dict[int, PooledForecast]:
    return {
        sid: pool_agent_verdicts(_verdicts(sid, p), base_rates=BASE, config=COMMITTEE)
        for sid, p in probabilities.items()
    }


def _observations(n: int) -> list[Observation]:
    return [Observation(logit=(i - n / 2) / n, outcome=i % 2, weight=1.0) for i in range(n)]


def _size(
    pooled: dict[int, PooledForecast],
    fit: CalibrationFit,
    vols: dict[int, float] | None = None,
    bear: dict[int, BearSeverity | None] | None = None,
    sectors: dict[int, str] | None = None,
) -> ProposedBook:
    return size_committee_book(
        run_id=RUN,
        as_of=AS_OF,
        pooled=pooled,
        bear=bear or {},
        sectors=sectors or {sid: f"S{sid % 4}" for sid in pooled},
        volatilities=vols or {sid: 0.25 for sid in pooled},
        fit=fit,
        config=RISK,
    )


SPREAD = {sid: 0.36 + 0.03 * sid for sid in range(1, N + 1)}  # 0.39 .. 0.72


def _calibrated_book() -> tuple[ProposedBook, CalibrationFit]:
    fit = fit_stacker(_observations(12), horizon=H, independent_periods=12, config=COMMITTEE)
    return _size(_pooled(SPREAD), fit), fit


def _decision(book: ProposedBook, vetoes: Sequence[str] = ()) -> CioDecisionLLM:
    return CioDecisionLLM.model_validate(
        {
            "decisions": [
                {
                    "entity_token": p.entity_token,
                    "action": "veto" if p.entity_token in vetoes else "approve",
                    "reason": "bear case confirmed" if p.entity_token in vetoes else "",
                }
                for p in book.positions
            ],
            "rationale": "book reviewed",
        }
    )


class _Client:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    def complete(self, messages: Sequence[Message]) -> ChatResponse:
        return ChatResponse(
            STRONG.slug, self.reply, {"prompt_tokens": 1000, "completion_tokens": 200}
        )


class _Dlq:
    def __init__(self) -> None:
        self.records: list[dict[str, str]] = []

    def push(self, record: dict[str, str]) -> None:
        self.records.append(record)


def _assert_budget(book: ProposedBook) -> None:
    assert book.gross_exposure + book.cash_weight == pytest.approx(1.0, abs=1e-12)
    assert all(p.target_weight <= RISK.max_position + 1e-12 for p in book.positions)
    by_sector: dict[str, float] = {}
    for p in book.positions:
        by_sector[p.sector] = by_sector.get(p.sector, 0.0) + p.target_weight
    assert all(w <= RISK.max_sector + 1e-12 for w in by_sector.values())


def test_p4_e2e_rank_mode_pipeline() -> None:
    fit = fit_stacker(_observations(4), horizon=H, independent_periods=4, config=COMMITTEE)
    assert not fit.active and (fit.alpha, fit.beta) == (0.0, 1.0)  # p_cal = sigmoid(L)
    pooled = _pooled(SPREAD)
    book = _size(pooled, fit)
    top8 = sorted(pooled, key=lambda s: -pooled[s].pool(H).logit)[: RISK.rank_top_m]
    ids = {p.security_id for p in book.positions}
    assert ids and ids <= set(top8)
    for p in book.positions:
        assert p.pooled_p == pytest.approx(sigmoid(pooled[p.security_id].pool(H).logit))
    _assert_budget(book)


def test_p4_e2e_calibrated_mode_pipeline() -> None:
    book, fit = _calibrated_book()
    assert fit.active and fit.base_rate is not None
    pooled = _pooled(SPREAD)
    for sid, forecast in pooled.items():
        p_cal = sigmoid(fit.alpha + fit.beta * forecast.pool(H).logit)
        held = next((p for p in book.positions if p.security_id == sid), None)
        if p_cal - fit.base_rate < RISK.edge_hurdle:
            assert held is None
        elif held is not None:
            assert held.pooled_p == pytest.approx(p_cal)
    assert book.positions
    _assert_budget(book)
    # Same volatility, so a larger edge never gets a smaller weight.
    ordered = sorted(book.positions, key=lambda p: p.pooled_p)
    weights = [p.target_weight for p in ordered]
    assert weights == sorted(weights)


def test_p4_e2e_cio_single_veto_cash_routing() -> None:
    book, _ = _calibrated_book()
    victim = max(book.positions, key=lambda p: p.target_weight)
    final = apply_cio_decision(
        book, _decision(book, [victim.entity_token]), max_veto_pct=RISK.cio_veto_budget
    )
    assert victim.entity_token not in {p.entity_token for p in final.positions}
    assert final.cash_weight == pytest.approx(book.cash_weight + victim.target_weight, abs=1e-12)
    assert final.gross_exposure == pytest.approx(book.gross_exposure - victim.target_weight)
    before = {p.entity_token: p.target_weight for p in book.positions}
    assert all(before[p.entity_token] == p.target_weight for p in final.positions)  # bit-identical

    served = run_cio(
        _Client(_decision(book, [victim.entity_token]).model_dump_json()),
        book,
        models=MODELS,
        run_id=RUN,
        max_veto_pct=RISK.cio_veto_budget,
    )
    assert served.decision is not None and served.decision.model_served == STRONG.slug


def test_p4_e2e_cio_budget_breach_dlq_fallback() -> None:
    book, _ = _calibrated_book()
    tokens = [p.entity_token for p in book.positions]
    limit = int(len(tokens) * RISK.cio_veto_budget + 1e-9)
    reply = _decision(book, tokens[: limit + 1]).model_dump_json()
    with pytest.raises(CioMiscalibrationAlert):
        apply_cio_decision(
            book, CioDecisionLLM.model_validate_json(reply), max_veto_pct=RISK.cio_veto_budget
        )
    dlq = _Dlq()
    res: Any = run_cio(
        _Client(reply),
        book,
        models=MODELS,
        run_id=RUN,
        max_veto_pct=RISK.cio_veto_budget,
        dlq=dlq,
    )
    assert isinstance(res.alert, CioMiscalibrationAlert)
    assert res.book is book  # vetoes discarded, proposed book untouched
    assert [r["error"] for r in dlq.records] == ["cio_miscalibration"]


def test_p4_e2e_red_team_severity_propagation() -> None:
    # Unit-scale volatility keeps raw weights below every cap, so severity is the only difference.
    fit = CalibrationFit(
        horizon=H, alpha=0.0, beta=1.0, active=True, independent_periods=20,
        observations=100, base_rate=0.5,
    )  # fmt: skip
    pooled = _pooled({1: 0.62, 2: 0.62})
    vols = {1: 1.0, 2: 1.0}
    book = _size(pooled, fit, vols, bear={1: BearSeverity.HIGH, 2: BearSeverity.LOW})
    w = {p.security_id: p.target_weight for p in book.positions}
    assert w[1] == pytest.approx(w[2] * float(RISK.bear_multiplier[BearSeverity.HIGH]))
    assert w[1] == pytest.approx(w[2] / 2)
