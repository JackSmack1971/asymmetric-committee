"""Pure deterministic sizing and portfolio constraints from §8.1."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from config.loader import RiskConfig
from contracts.enums import BearSeverity
from contracts.models import CommitteeDecision, ProposedBook, ProposedPosition


def _portfolio_vol(weights: Mapping[int, float], volatilities: Mapping[int, float]) -> float:
    """Annualized diagonal-covariance volatility estimate."""
    return math.sqrt(sum((weight * volatilities[sid]) ** 2 for sid, weight in weights.items()))


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
    for sector in sorted(set(sectors.values())):
        ids = [sid for sid in weights if sectors[sid] == sector]
        total = sum(weights[sid] for sid in ids)
        scale = min(1.0, config.max_sector / total) if total else 1.0
        for sid in ids:
            weights[sid] *= scale
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


def with_target_weights(
    decisions: Sequence[CommitteeDecision], book: ProposedBook
) -> tuple[CommitteeDecision, ...]:
    """Return the same committee-compatible contracts with risk weights populated."""
    weights = {p.security_id: p.target_weight for p in book.positions}
    return tuple(
        d.model_copy(update={"target_weight": weights.get(d.security_id, 0.0)}) for d in decisions
    )
