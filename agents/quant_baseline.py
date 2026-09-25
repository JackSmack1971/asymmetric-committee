"""Deterministic cross-sectional value + momentum + quality control (§3)."""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import UUID

from contracts.data import FeatureRow
from contracts.enums import AgentName, Horizon
from contracts.models import AgentWeight, CommitteeDecision

VALUE = ("ev_sales_sector_percentile", "ev_ebit_sector_percentile", "fcf_yield_sector_percentile")
QUALITY = (
    "gross_margin_trend_sector_percentile",
    "revenue_growth_yoy_sector_percentile",
    "revenue_growth_3y_cagr_sector_percentile",
    "net_debt_ebitda_sector_percentile",
    "share_count_change_sector_percentile",
    "accruals_ratio_sector_percentile",
)
LOW_IS_GOOD = frozenset(
    {
        "ev_sales_sector_percentile",
        "ev_ebit_sector_percentile",
        "net_debt_ebitda_sector_percentile",
        "share_count_change_sector_percentile",
        "accruals_ratio_sector_percentile",
    }
)


def _zscores(rows: Sequence[FeatureRow], name: str) -> dict[int, float]:
    observed = {
        r.security_id: float(r.values[name])
        for r in rows
        if isinstance(r.values.get(name), (int, float)) and math.isfinite(float(r.values[name]))
    }
    if not observed:
        return {r.security_id: 0.0 for r in rows}
    mean = statistics.fmean(observed.values())
    scale = statistics.pstdev(observed.values()) if len(observed) > 1 else 1.0
    sign = -1.0 if name in LOW_IS_GOOD else 1.0
    return {
        r.security_id: sign * (observed.get(r.security_id, mean) - mean) / (scale or 1.0)
        for r in rows
    }


def decisions(
    *, rows: Sequence[FeatureRow], run_id: UUID, as_of: datetime, entity_tokens: Mapping[int, str]
) -> tuple[CommitteeDecision, ...]:
    factors = {name: _zscores(rows, name) for name in (*VALUE, "momentum_12_1", *QUALITY)}
    out = []
    for row in rows:
        value = statistics.fmean(factors[name][row.security_id] for name in VALUE)
        momentum = factors["momentum_12_1"][row.security_id]
        quality = statistics.fmean(factors[name][row.security_id] for name in QUALITY)
        composite = statistics.fmean((value, momentum, quality))
        probability = 1 / (1 + math.exp(-max(-20.0, min(20.0, composite))))
        out.append(
            CommitteeDecision(
                run_id=run_id,
                security_id=row.security_id,
                entity_token=entity_tokens[row.security_id],
                as_of=as_of,
                horizon_days=Horizon.D21,
                pooled_p=probability,
                dispersion=0.0,
                agent_weights=(AgentWeight(agent=AgentName.QUANT_BASELINE, weight=1.0),),
                bear_severity=None,
                target_weight=0.0,
            )
        )
    return tuple(out)
