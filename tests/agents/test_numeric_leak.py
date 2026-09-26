"""§13: no prompt contains a raw XBRL value, share count or price at >= 4 significant digits, and
no calendar date survives (rule set N). Runs on contract rows; no database needed."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from contracts.data import FeatureRow, FundamentalFact, PriceBar
from contracts.enums import PriceFeed
from features import build_feature_rows
from features.renderer import FEATURE_KINDS, render_features
from tests.agents.leak import calendar_dates, numeric_leaks, significant

AS_OF = datetime(2025, 3, 7, 21, 0, tzinfo=UTC)
SECURITIES = (1, 2, 3, 4)
World = tuple[list[FeatureRow], list[float]]


def _bars(sid: int) -> list[PriceBar]:
    out = []
    for i in range(260):
        px = 187.4321 * (1 + 0.0007 * i) * (1 + 0.013 * sid) + (i % 5) * 0.3137
        out.append(
            PriceBar(
                security_id=sid,
                event_time=AS_OF - timedelta(days=260 - i),
                available_at=AS_OF - timedelta(days=259 - i),
                source_version="sip",
                open=px,
                high=px * 1.011,
                low=px * 0.989,
                close=px,
                volume=12_345_678.0 + 9_871 * i + sid * 4_321,
                feed=PriceFeed.SIP,
            )
        )
    return out


def _facts(sid: int) -> list[FundamentalFact]:
    out = []
    for q in range(14):
        end = date(2021 + (q + 2) // 4, ((q + 2) % 4) * 3 + 3, 28)
        filed = datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=30)
        for concept, base in (
            ("us-gaap:Revenues", 61_234_567_000.0),
            ("us-gaap:GrossProfit", 27_654_321_000.0),
            ("us-gaap:OperatingIncomeLoss", 9_876_543_000.0),
            ("dei:EntityCommonStockSharesOutstanding", 812_345_678.0),
        ):
            out.append(
                FundamentalFact(
                    security_id=sid,
                    concept=concept,
                    unit="USD",
                    period_start=None,
                    period_end=end,
                    fiscal_period=None,
                    form="10-Q",
                    value=base * (1 + 0.02 * q) * (1 + 0.05 * sid),
                    event_time=datetime(end.year, end.month, end.day, tzinfo=UTC),
                    available_at=filed,
                    source_version=f"0000{sid}-{q:02d}",
                )
            )
    return [f for f in out if f.available_at <= AS_OF]


@pytest.fixture(scope="module")
def world() -> World:
    prices = {sid: _bars(sid) for sid in SECURITIES}
    facts = {sid: _facts(sid) for sid in SECURITIES}
    caps = {sid: 3.4567e10 * sid for sid in SECURITIES}
    rows = build_feature_rows(
        as_of=AS_OF,
        security_ids=list(SECURITIES),
        sectors=dict.fromkeys(SECURITIES, "Tech"),
        market_caps=caps,
        prices=prices,
        fundamentals=facts,
        insiders=(),
        news=(),
    )
    raw: list[float] = list(caps.values())
    for sid in SECURITIES:
        for bar in prices[sid]:
            raw += [bar.open, bar.high, bar.low, bar.close, bar.volume]
        raw += [f.value for f in facts[sid]]
    return list(rows), raw


def _render_all(rows: list[FeatureRow]) -> str:
    sectors = dict.fromkeys(SECURITIES, "Tech")
    names = [n for n in FEATURE_KINDS if any(n in r.values for r in rows)]
    return "\n".join(render_features(r, universe=rows, sectors=sectors, names=names) for r in rows)


def test_harness_has_real_raw_values_to_guard(world: World) -> None:
    _, raw = world
    assert len(raw) > 1000 and sum(bool(significant(v)) for v in raw) > 1000


def test_rendered_partition_has_no_raw_scalar_or_date(world: World) -> None:
    rows, raw = world
    text = _render_all(rows)
    assert "ev_sales" in text and "momentum_12_1" in text  # it rendered something real
    assert numeric_leaks(text, raw) == []
    assert calendar_dates(text) == []


def test_harness_catches_a_raw_scalar_leak(world: World) -> None:
    """Negative control: a prompt that includes a price, share count or market cap is flagged."""
    _, raw = world
    price, shares, cap = raw[3], 812_345_678.0, 3.4567e10
    assert numeric_leaks(f"close {price:.4f}", [price]) != []
    assert numeric_leaks(f"shares {shares:,.0f}", [shares]) != []
    assert numeric_leaks(f"cap {cap:.4e}", [cap]) != []
    assert numeric_leaks(f"cap {cap:.0f}", [cap]) != []
    assert numeric_leaks("ratio 1.2 pct 0.5", [price, shares, cap]) == []


@pytest.mark.parametrize(
    "text",
    ["2024-03-31", "filed 03/31/2024", "Mar 31, 2024", "FY2024", "in 2023"],
)
def test_harness_catches_calendar_dates(text: str) -> None:
    assert calendar_dates(text) != []


@pytest.mark.parametrize("text", ["t-37d", "Q-4", "sector_pct 0.75", "z -1.2"])
def test_harness_allows_relative_time_and_ratios(text: str) -> None:
    assert calendar_dates(text) == []
