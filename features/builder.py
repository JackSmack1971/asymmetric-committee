"""Pure, point-in-time feature construction for feature set ``fs_v1`` (§5)."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta

from contracts.data import FeatureRow, FundamentalFact, InsiderTxn, NewsItem, PriceBar
from contracts.enums import InsiderRole, InsiderTxnCode

FEATURE_SET_VERSION = "fs_v1"

# XBRL concepts accepted for each economic series. The first present alias wins.
CONCEPTS: dict[str, tuple[str, ...]] = {
    "revenue": (
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "us-gaap:SalesRevenueNet",
        "us-gaap:Revenues",
    ),
    "gross_profit": ("us-gaap:GrossProfit",),
    "operating_income": ("us-gaap:OperatingIncomeLoss",),
    "net_income": ("us-gaap:NetIncomeLoss",),
    "cash": ("us-gaap:CashAndCashEquivalentsAtCarryingValue",),
    "debt": (
        "us-gaap:LongTermDebtAndFinanceLeaseObligationsCurrent",
        "us-gaap:LongTermDebtCurrent",
    ),
    "long_debt": (
        "us-gaap:LongTermDebtAndFinanceLeaseObligationsNoncurrent",
        "us-gaap:LongTermDebtNoncurrent",
    ),
    "shares": (
        "dei:EntityCommonStockSharesOutstanding",
        "us-gaap:CommonStocksIncludingAdditionalPaidInCapital",
    ),
    "assets": ("us-gaap:Assets",),
    "current_assets": ("us-gaap:AssetsCurrent",),
    "cash_flow": ("us-gaap:NetCashProvidedByUsedInOperatingActivities",),
    "capex": ("us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",),
    "da": ("us-gaap:DepreciationDepletionAndAmortization",),
}


def _safe_div(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None or b == 0 else a / b


def _return(closes: Sequence[float], periods: int, *, skip: int = 0) -> float | None:
    end = len(closes) - 1 - skip
    start = end - periods
    return None if start < 0 or closes[start] <= 0 else closes[end] / closes[start] - 1


def _std(values: Sequence[float]) -> float | None:
    return statistics.stdev(values) if len(values) >= 2 else None


def price_features(bars: Sequence[PriceBar]) -> dict[str, float | None]:
    """Calculate every §5 price feature from chronological daily bars."""
    ordered = sorted(bars, key=lambda b: (b.event_time, b.source_version))
    closes = [b.close for b in ordered]
    volumes = [b.volume for b in ordered]
    out: dict[str, float | None] = {
        "return_1m": _return(closes, 21),
        "return_3m": _return(closes, 63),
        "return_6m": _return(closes, 126),
        "momentum_12_1": _return(closes, 231, skip=21),
    }
    last = closes[-1] if closes else None
    for window in (20, 50, 200):
        sma = statistics.fmean(closes[-window:]) if len(closes) >= window else None
        out[f"sma_{window}d_distance"] = _safe_div(last - sma, sma) if last and sma else None
    returns = [closes[i] / closes[i - 1] - 1 for i in range(max(1, len(closes) - 20), len(closes))]
    daily_vol = _std(returns)
    out["realized_vol_20d"] = daily_vol * math.sqrt(252) if daily_vol is not None else None
    if len(ordered) >= 20 and last:
        trs = []
        for i, bar in enumerate(ordered[-20:]):
            prev = (
                ordered[len(ordered) - 20 + i - 1].close if len(ordered) - 20 + i > 0 else bar.close
            )
            trs.append(max(bar.high - bar.low, abs(bar.high - prev), abs(bar.low - prev)))
        out["atr_20d_pct"] = statistics.fmean(trs) / last
    else:
        out["atr_20d_pct"] = None
    tail = closes[-63:]
    peak = -math.inf
    drawdowns: list[float] = []
    for value in tail:
        peak = max(peak, value)
        drawdowns.append(value / peak - 1)
    out["max_drawdown_63d"] = min(drawdowns) if drawdowns else None
    vtail = volumes[-20:]
    vstd = _std(vtail)
    out["volume_zscore_20d"] = (
        (volumes[-1] - statistics.fmean(vtail)) / vstd if vstd and volumes else None
    )
    high = max(closes[-252:]) if closes else None
    out["distance_52w_high"] = _safe_div(last - high, high) if last and high else None
    return out


def _series(facts: Sequence[FundamentalFact], name: str) -> list[FundamentalFact]:
    aliases = CONCEPTS[name]
    for concept in aliases:
        found = [f for f in facts if f.concept == concept]
        if found:
            return sorted(found, key=lambda f: (f.period_end, f.available_at))
    return []


def _latest(facts: Sequence[FundamentalFact], name: str) -> float | None:
    rows = _series(facts, name)
    return rows[-1].value if rows else None


def fundamental_features(
    facts: Sequence[FundamentalFact], market_cap: float | None
) -> dict[str, float | None]:
    revenue = _series(facts, "revenue")
    gross = _series(facts, "gross_profit")
    op_income = _latest(facts, "operating_income")
    cash = _latest(facts, "cash") or 0.0
    debt = (_latest(facts, "debt") or 0.0) + (_latest(facts, "long_debt") or 0.0)
    enterprise_value = market_cap + debt - cash if market_cap is not None else None
    latest_revenue = revenue[-1].value if revenue else None
    cfo, capex = _latest(facts, "cash_flow"), _latest(facts, "capex")
    fcf = cfo - capex if cfo is not None and capex is not None else None
    ebitda = op_income + (_latest(facts, "da") or 0.0) if op_income is not None else None
    maybe_margins = [
        _safe_div(g.value, r.value) for g, r in zip(gross[-5:], revenue[-5:], strict=False)
    ]
    gross_margins: list[float] = [x for x in maybe_margins if x is not None]
    yoy = _safe_div(latest_revenue, revenue[-5].value) if len(revenue) >= 5 else None
    cagr = _safe_div(latest_revenue, revenue[-13].value) if len(revenue) >= 13 else None
    shares = _series(facts, "shares")
    net_income = _latest(facts, "net_income")
    assets = _latest(facts, "assets")
    accruals = net_income - cfo if net_income is not None and cfo is not None else None
    return {
        "ev_sales": _safe_div(enterprise_value, latest_revenue),
        "ev_ebit": _safe_div(enterprise_value, op_income),
        "fcf_yield": _safe_div(fcf, market_cap),
        "gross_margin_trend": (gross_margins[-1] - gross_margins[0])
        if len(gross_margins) >= 2
        else None,
        "revenue_growth_yoy": yoy - 1 if yoy is not None else None,
        "revenue_growth_3y_cagr": cagr ** (1 / 3) - 1 if cagr is not None and cagr >= 0 else None,
        "net_debt_ebitda": _safe_div(debt - cash, ebitda),
        "share_count_change": (shares[-1].value / shares[-5].value - 1)
        if len(shares) >= 5 and shares[-5].value
        else None,
        "accruals_ratio": _safe_div(accruals, assets),
    }


def insider_features(rows: Sequence[InsiderTxn], as_of: datetime) -> dict[str, float | int]:
    buys = [
        r
        for r in rows
        if r.available_at > as_of - timedelta(days=90)
        and r.code is InsiderTxnCode.P
        and r.acquired
        and not r.is_10b5_1
    ]
    return {
        "insider_net_open_market_buy_usd_90d": sum(r.shares * (r.price or 0.0) for r in buys),
        "insider_distinct_buyers_90d": len({r.filer for r in buys}),
        "insider_ceo_cfo_buy_flag_90d": int(
            any(
                r.role is InsiderRole.OFFICER
                and r.officer_title
                and any(
                    x in r.officer_title.upper()
                    for x in ("CEO", "CFO", "CHIEF EXECUTIVE", "CHIEF FINANCIAL")
                )
                for r in buys
            )
        ),
    }


def news_features(
    rows: Sequence[NewsItem], security_id: int, as_of: datetime
) -> dict[str, float | int]:
    relevant = [r for r in rows if security_id in r.security_ids]
    counts = [
        sum(
            as_of - timedelta(days=7 * (i + 1)) < r.event_time <= as_of - timedelta(days=7 * i)
            for r in relevant
        )
        for i in range(8)
    ]
    hist_std = _std([float(x) for x in counts[1:]])
    return {
        "news_count_zscore_7d": (counts[0] - statistics.fmean(counts[1:])) / hist_std
        if hist_std
        else 0.0,
        "news_source_diversity_7d": len(
            {r.publisher for r in relevant if r.event_time > as_of - timedelta(days=7)}
        ),
    }


def add_sector_percentiles(
    values: Mapping[int, Mapping[str, object]],
    sectors: Mapping[int, str],
    fundamental_names: Iterable[str],
) -> dict[int, dict[str, object]]:
    """Percentiles use only names present in ``values`` (the current included snapshot)."""
    out = {sid: dict(row) for sid, row in values.items()}
    for name in fundamental_names:
        for output_row in out.values():
            output_row[f"{name}_sector_percentile"] = None
        by_sector: dict[str, list[tuple[int, float]]] = defaultdict(list)
        for sid, row in values.items():
            value = row.get(name)
            if isinstance(value, (int, float)) and math.isfinite(value):
                by_sector[sectors[sid]].append((sid, float(value)))
        for group in by_sector.values():
            ordered = sorted(group, key=lambda item: (item[1], item[0]))
            denominator = max(len(ordered) - 1, 1)
            for rank, (sid, _) in enumerate(ordered):
                out[sid][f"{name}_sector_percentile"] = rank / denominator
    return out


def build_feature_rows(
    *,
    as_of: datetime,
    security_ids: Sequence[int],
    sectors: Mapping[int, str],
    market_caps: Mapping[int, float | None],
    prices: Mapping[int, Sequence[PriceBar]],
    fundamentals: Mapping[int, Sequence[FundamentalFact]],
    insiders: Sequence[InsiderTxn],
    news: Sequence[NewsItem],
) -> tuple[FeatureRow, ...]:
    """Pure builder: callers obtain every input via ``store.as_of`` and persist the result."""
    raw: dict[int, dict[str, object]] = {}
    for sid in security_ids:
        row: dict[str, object] = {}
        row.update(price_features(prices.get(sid, ())))
        row.update(fundamental_features(fundamentals.get(sid, ()), market_caps.get(sid)))
        row.update(insider_features([x for x in insiders if x.security_id == sid], as_of))
        row.update(news_features(news, sid, as_of))
        raw[sid] = row
    normalized = add_sector_percentiles(raw, sectors, fundamental_features((), None))
    return tuple(
        FeatureRow(
            security_id=sid,
            event_time=as_of,
            available_at=as_of,
            source_version=FEATURE_SET_VERSION,
            feature_set_version=FEATURE_SET_VERSION,
            values=normalized[sid],
        )
        for sid in security_ids
    )
