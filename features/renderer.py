"""Rule set N renderer (§5 "Agent-facing rendering", §12.1 item 3).

Pure functions from contract rows to prompt text. Every value that leaves this module is a
dimensionless ratio, a cross-sectional z-score or percentile rounded to two significant figures, a
small count or a flag. Nothing here reads identity, dates or currency:

- N1/N2: only names in ``FEATURE_KINDS`` render, and that allowlist holds no currency, share-count
  or price-level feature. ``insider_net_open_market_buy_usd_90d`` is deliberately absent.
- N3: z-scores and percentiles are taken within a sector of the caller's point-in-time universe
  snapshot, and only for groups of at least ``MIN_GROUP`` names (a smaller group is not a
  cross-section, so it renders ``NA`` rather than a fabricated statistic).
- N4: ``relative_days`` (``t-37d``) and ``fiscal_index`` (``Q-1`` ... ``Q-20``) replace dates. The
  minus sign is ASCII.
- N5: ``mcap_tier_label`` is the only size information.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from contracts.data import FeatureRow
from contracts.enums import McapTier

MIN_GROUP = 3
MAX_FISCAL_INDEX = 20
NA = "NA"
HEADER = "feature\tvalue\tsector_z\tsector_pct"


class RenderError(ValueError):
    """A value that rule set N forbids, or cannot render safely, was requested."""


class Kind(StrEnum):
    RATIO = "ratio"  # dimensionless level: value + sector z-score + sector percentile
    PERCENTILE = "percentile"  # already a cross-sectional percentile
    COUNT = "count"  # small integer (e.g. distinct buyers)
    FLAG = "flag"  # 0 or 1


_PRICE_RATIOS = (
    "return_1m",
    "return_3m",
    "return_6m",
    "momentum_12_1",
    "sma_20d_distance",
    "sma_50d_distance",
    "sma_200d_distance",
    "realized_vol_20d",
    "atr_20d_pct",
    "max_drawdown_63d",
    "volume_zscore_20d",
    "distance_52w_high",
)
_FUNDAMENTAL_RATIOS = (
    "ev_sales",
    "ev_ebit",
    "fcf_yield",
    "gross_margin_trend",
    "revenue_growth_yoy",
    "revenue_growth_3y_cagr",
    "net_debt_ebitda",
    "share_count_change",
    "accruals_ratio",
)

FEATURE_KINDS: dict[str, Kind] = {
    **dict.fromkeys(_PRICE_RATIOS, Kind.RATIO),
    **dict.fromkeys(_FUNDAMENTAL_RATIOS, Kind.RATIO),
    **{f"{name}_sector_percentile": Kind.PERCENTILE for name in _FUNDAMENTAL_RATIOS},
    "insider_distinct_buyers_90d": Kind.COUNT,
    "insider_ceo_cfo_buy_flag_90d": Kind.FLAG,
    "news_count_zscore_7d": Kind.RATIO,
    "news_source_diversity_7d": Kind.COUNT,
}


def sig2(value: float) -> str:
    """Two significant figures as a plain decimal string (never scientific notation)."""
    if not math.isfinite(value):
        raise RenderError(f"cannot render non-finite value {value!r}")
    if value == 0:
        return "0"
    return format(Decimal(f"{value:.2g}"), "f")


def relative_days(ts: datetime, as_of: datetime) -> str:
    """N4: whole days before ``as_of``. A timestamp after ``as_of`` is look-ahead and is refused."""
    delta = as_of - ts
    if delta.total_seconds() < 0:
        raise RenderError("timestamp is after as_of")
    return f"t-{delta.days}d"


def _quarter(year: int, month: int) -> int:
    return year * 4 + (month - 1) // 3


def fiscal_index(period_end: date, as_of: datetime) -> str:
    """N4: ``Q-1`` is the latest completed quarter; a same-quarter period end is also ``Q-1``."""
    if period_end > as_of.date():
        raise RenderError("period end is after as_of")
    idx = max(1, _quarter(as_of.year, as_of.month) - _quarter(period_end.year, period_end.month))
    if idx > MAX_FISCAL_INDEX:
        raise RenderError(f"period is older than Q-{MAX_FISCAL_INDEX}")
    return f"Q-{idx}"


_TIER_LABELS = {
    McapTier.SMALL: "Small-Cap",
    McapTier.MID: "Mid-Cap",
    McapTier.LARGE: "Large-Cap",
}


def mcap_tier_label(tier: McapTier | None) -> str:
    """N5: the only size information an agent sees."""
    if tier is None:
        raise RenderError("market-cap tier is unknown")
    return _TIER_LABELS[tier]


def percentile_ranks(values: Mapping[int, float | None]) -> dict[int, float | None]:
    """Mid-rank percentile in [0, 1]; ``None`` for missing values or a single-name cross-section."""
    valid = {k: v for k, v in values.items() if v is not None and math.isfinite(v)}
    out: dict[int, float | None] = dict.fromkeys(values)
    if len(valid) < 2:
        return out
    for key, v in valid.items():
        less = sum(x < v for x in valid.values())
        equal = sum(x == v for x in valid.values())
        out[key] = (less + 0.5 * (equal - 1)) / (len(valid) - 1)
    return out


def _by_sector(
    values: Mapping[int, float | None], sectors: Mapping[int, str]
) -> dict[str, dict[int, float | None]]:
    groups: dict[str, dict[int, float | None]] = defaultdict(dict)
    for sid, v in values.items():
        groups[sectors[sid]][sid] = v
    return groups


def _finite(v: float | None) -> bool:
    return v is not None and math.isfinite(v)


def sector_zscores(
    values: Mapping[int, float | None], sectors: Mapping[int, str]
) -> dict[int, float | None]:
    out: dict[int, float | None] = dict.fromkeys(values)
    for group in _by_sector(values, sectors).values():
        valid = {k: v for k, v in group.items() if v is not None and math.isfinite(v)}
        if len(valid) < MIN_GROUP:
            continue
        mean, std = statistics.fmean(valid.values()), statistics.pstdev(valid.values())
        for sid, v in valid.items():
            out[sid] = 0.0 if std == 0 else (v - mean) / std
    return out


def _sector_percentiles(
    values: Mapping[int, float | None], sectors: Mapping[int, str]
) -> dict[int, float | None]:
    out: dict[int, float | None] = dict.fromkeys(values)
    for group in _by_sector(values, sectors).values():
        if sum(_finite(v) for v in group.values()) >= MIN_GROUP:
            out.update(percentile_ranks(group))
    return out


def _number(row: FeatureRow, name: str) -> float | None:
    v = row.values.get(name)
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, int | float) and math.isfinite(v):
        return float(v)
    return None


def render_features(
    row: FeatureRow,
    *,
    universe: Sequence[FeatureRow],
    sectors: Mapping[int, str],
    names: Sequence[str],
) -> str:
    """One security's features as TSV. ``universe`` is the point-in-time included snapshot."""
    for name in names:
        if name not in FEATURE_KINDS:
            raise RenderError(f"feature {name!r} may not be rendered (rule set N)")
    if row.security_id not in sectors:
        raise RenderError("security has no sector in the snapshot")
    lines = [HEADER]
    for name in names:
        kind = FEATURE_KINDS[name]
        value = _number(row, name)
        if value is None:
            lines.append(f"{name}\t{NA}\t{NA}\t{NA}" if kind is Kind.RATIO else f"{name}\t{NA}\t\t")
        elif kind in (Kind.COUNT, Kind.FLAG):
            lines.append(f"{name}\t{int(value)}\t\t")
        elif kind is Kind.PERCENTILE:
            lines.append(f"{name}\t{sig2(value)}\t\t")
        else:
            column = {r.security_id: _number(r, name) for r in universe if r.security_id in sectors}
            z = sector_zscores(column, sectors).get(row.security_id)
            pct = _sector_percentiles(column, sectors).get(row.security_id)
            lines.append(
                f"{name}\t{sig2(value)}\t{NA if z is None else sig2(z)}"
                f"\t{NA if pct is None else sig2(pct)}"
            )
    return "\n".join(lines)
