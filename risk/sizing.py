"""Pure deterministic sizing and portfolio constraints from §8.1."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import UUID

from committee.stacker import sigmoid
from config.loader import RiskConfig
from contracts.enums import BearSeverity
from contracts.models import (
    CalibrationFit,
    CommitteeDecision,
    PooledForecast,
    ProposedBook,
    ProposedPosition,
)


def _portfolio_vol(weights: Mapping[int, float], volatilities: Mapping[int, float]) -> float:
    """Annualized diagonal-covariance volatility estimate."""
    return math.sqrt(sum((weight * volatilities[sid]) ** 2 for sid, weight in weights.items()))


def _cap_sectors(weights: dict[int, float], sectors: Mapping[int, str], cap: float) -> None:
    """Scale every over-cap sector pro rata, in place. Trimmed weight becomes cash."""
    for sector in sorted(set(sectors[sid] for sid in weights)):
        ids = [sid for sid in weights if sectors[sid] == sector]
        total = sum(weights[sid] for sid in ids)
        scale = min(1.0, cap / total) if total else 1.0
        for sid in ids:
            weights[sid] *= scale


def size_book(
    *,
    decisions: Sequence[CommitteeDecision],
    sectors: Mapping[int, str],
    volatilities: Mapping[int, float],
    config: RiskConfig,
) -> ProposedBook:
    if not decisions:
        raise ValueError("at least one decision is required")
    weights: dict[int, float] = {}
    for decision in decisions:
        sigma = volatilities.get(decision.security_id, 0.0)
        edge = decision.pooled_p - 0.5
        if decision.pooled_p < config.entry_threshold or sigma <= 0 or edge <= 0:
            weight = 0.0
        else:
            bear = config.bear_multiplier[decision.bear_severity or BearSeverity.LOW]
            weight = config.k * edge / sigma
            weight *= max(0.0, 1 - config.dispersion_lambda * decision.dispersion) * bear
            weight = min(weight, config.max_position)
            if weight < config.min_position:
                weight = 0.0
        weights[decision.security_id] = weight
    # Sector caps scale rather than select names, preserving the baseline ordering.
    _cap_sectors(weights, sectors, config.max_sector)
    vol = _portfolio_vol(weights, volatilities)
    scale = min(1.0, config.vol_target_annual / vol) if vol else 1.0
    gross = sum(weights.values())
    scale = min(scale, 1 / gross) if gross else scale
    weights = {sid: weight * scale for sid, weight in weights.items()}
    # Scaling can cross the minimum; remove such dust and never redistribute it.
    weights = {
        sid: (weight if weight >= config.min_position else 0.0) for sid, weight in weights.items()
    }
    by_id = {d.security_id: d for d in decisions}
    positions = tuple(
        ProposedPosition(
            security_id=sid,
            entity_token=by_id[sid].entity_token,
            sector=sectors[sid],
            pooled_p=by_id[sid].pooled_p,
            target_weight=weight,
        )
        for sid, weight in sorted(weights.items())
        if weight > 0
    )
    return ProposedBook(run_id=decisions[0].run_id, as_of=decisions[0].as_of, positions=positions)


def size_committee_book(
    *,
    run_id: UUID,
    as_of: datetime,
    pooled: Mapping[int, PooledForecast],
    bear: Mapping[int, BearSeverity | None],
    sectors: Mapping[int, str],
    volatilities: Mapping[int, float],
    fit: CalibrationFit,
    config: RiskConfig,
) -> ProposedBook:
    """Committee sizing (§8.1). ``fit.horizon`` is the primary horizon.

    Rank mode while the stacker is inactive: top M by pooled logit, inverse-vol, times S_bear.
    Calibrated mode once active: edge = p_cal - b >= hurdle, raw_w = k * edge / sigma. Then, in
    order: single-position cap, sector cap, 1% floor, vol target (and gross <= 1), and a final
    floor pass because scaling can cross it. Nothing is redistributed; the residual is cash.
    """
    scored: dict[int, tuple[float, float]] = {}  # security_id -> (pooled logit, dispersion)
    for sid, forecast in pooled.items():
        pool = forecast.pool(fit.horizon)
        scored[sid] = (pool.logit, pool.dispersion)
    eligible = {sid: v for sid, v in scored.items() if volatilities.get(sid, 0.0) > 0}

    def s_bear(sid: int) -> float:
        return config.bear_multiplier[bear.get(sid) or BearSeverity.LOW]

    raw: dict[int, float] = {}
    if not fit.active:
        top = sorted(eligible, key=lambda sid: (-eligible[sid][0], sid))[: config.rank_top_m]
        inv = {sid: 1.0 / volatilities[sid] for sid in top}
        total = sum(inv.values())
        raw = {sid: inv[sid] / total * s_bear(sid) for sid in top}
    else:
        if fit.base_rate is None:
            raise ValueError("active calibration fit has no base rate")
        for sid, (logit, dispersion) in eligible.items():
            edge = sigmoid(fit.alpha + fit.beta * logit) - fit.base_rate
            if edge >= config.edge_hurdle:
                shrink = max(0.0, 1.0 - config.committee_dispersion_lambda * dispersion)
                raw[sid] = config.committee_k * edge / volatilities[sid] * shrink * s_bear(sid)
    weights = {sid: min(max(w, 0.0), config.max_position) for sid, w in raw.items()}
    _cap_sectors(weights, sectors, config.max_sector)
    weights = {sid: (w if w >= config.min_position else 0.0) for sid, w in weights.items()}
    vol = _portfolio_vol(weights, volatilities)
    gross = sum(weights.values())
    scale = min(1.0, config.vol_target_annual / vol) if vol else 1.0
    scale = min(scale, 1 / gross) if gross else scale
    weights = {
        sid: (w * scale if w * scale >= config.min_position else 0.0) for sid, w in weights.items()
    }
    positions = tuple(
        ProposedPosition(
            security_id=sid,
            entity_token=pooled[sid].entity_token,
            sector=sectors[sid],
            pooled_p=sigmoid(fit.alpha + fit.beta * scored[sid][0]),
            target_weight=w,
        )
        for sid, w in sorted(weights.items())
        if w > 0
    )
    return ProposedBook(run_id=run_id, as_of=as_of, positions=positions)


def with_target_weights(
    decisions: Sequence[CommitteeDecision], book: ProposedBook
) -> tuple[CommitteeDecision, ...]:
    """Return the same committee-compatible contracts with risk weights populated."""
    weights = {p.security_id: p.target_weight for p in book.positions}
    return tuple(
        d.model_copy(update={"target_weight": weights.get(d.security_id, 0.0)}) for d in decisions
    )
