"""Rule set N (§12.1 item 3): everything that reaches a prompt is dimensionless and date-free."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from contracts.data import FeatureRow
from contracts.enums import McapTier
from features.renderer import (
    FEATURE_KINDS,
    Kind,
    RenderError,
    fiscal_index,
    mcap_tier_label,
    percentile_ranks,
    relative_days,
    render_features,
    sector_zscores,
    sig2,
)

AS_OF = datetime(2025, 3, 7, 21, 0, tzinfo=UTC)


def _row(sid: int, **values: object) -> FeatureRow:
    return FeatureRow(
        security_id=sid,
        event_time=AS_OF,
        available_at=AS_OF,
        source_version="fs_v1",
        feature_set_version="fs_v1",
        values=values,
    )


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        (0.0, "0"),
        (0.123456, "0.12"),
        (-3.456, "-3.5"),
        (15.234, "15"),
        (1234.0, "1200"),
        (123456789.0, "120000000"),
        (0.000123456, "0.00012"),
    ],
)
def test_sig2_is_plain_two_significant_figures(raw: float, text: str) -> None:
    assert sig2(raw) == text


@given(st.floats(min_value=-1e12, max_value=1e12, allow_nan=False, allow_infinity=False))
def test_sig2_never_emits_more_than_two_significant_digits(x: float) -> None:
    out = sig2(x)
    assert "e" not in out.lower()
    digits = out.lstrip("-").replace(".", "").strip("0")
    assert len(digits) <= 2
    if abs(x) > 1e-9:
        assert math.isclose(float(out), x, rel_tol=0.06)


def test_sig2_rejects_non_finite() -> None:
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(RenderError):
            sig2(bad)


def test_relative_days_replaces_calendar_dates() -> None:
    assert relative_days(datetime(2025, 1, 29, 12, tzinfo=UTC), AS_OF) == "t-37d"
    assert relative_days(AS_OF, AS_OF) == "t-0d"
    with pytest.raises(RenderError):  # a future timestamp is look-ahead, never rendered
        relative_days(datetime(2025, 3, 9, tzinfo=UTC), AS_OF)


@pytest.mark.parametrize(
    ("period_end", "label"),
    [
        (date(2024, 12, 31), "Q-1"),  # as_of is in 2025Q1: latest completed quarter
        (date(2024, 9, 30), "Q-2"),
        (date(2020, 3, 31), "Q-20"),
        (date(2025, 3, 7), "Q-1"),  # same quarter never becomes Q-0
    ],
)
def test_fiscal_index(period_end: date, label: str) -> None:
    assert fiscal_index(period_end, AS_OF) == label


def test_fiscal_index_rejects_out_of_window_or_future() -> None:
    with pytest.raises(RenderError):
        fiscal_index(date(2019, 12, 31), AS_OF)
    with pytest.raises(RenderError):
        fiscal_index(date(2025, 6, 30), AS_OF)


def test_mcap_tier_is_the_only_size_information() -> None:
    assert {t: mcap_tier_label(t) for t in McapTier} == {
        McapTier.SMALL: "Small-Cap",
        McapTier.MID: "Mid-Cap",
        McapTier.LARGE: "Large-Cap",
    }
    with pytest.raises(RenderError):
        mcap_tier_label(None)


def test_percentile_ranks_handle_ties_and_missing() -> None:
    assert percentile_ranks({1: 1.0, 2: 2.0, 3: 3.0}) == {1: 0.0, 2: 0.5, 3: 1.0}
    tied = percentile_ranks({1: 5.0, 2: 5.0, 3: 9.0})
    assert tied[1] == tied[2]
    assert tied[1] is not None and tied[3] is not None and tied[1] < tied[3]
    assert percentile_ranks({1: 1.0, 2: None, 3: 3.0}) == {1: 0.0, 2: None, 3: 1.0}
    assert percentile_ranks({1: 1.0}) == {1: None}  # one name has no cross-section


def test_sector_zscores_are_within_sector_and_need_three_names() -> None:
    values = {1: 1.0, 2: 2.0, 3: 3.0, 4: 100.0, 5: 200.0}
    sectors = {1: "A", 2: "A", 3: "A", 4: "B", 5: "B"}
    z = sector_zscores(values, sectors)
    assert z[2] == pytest.approx(0.0)
    assert z[1] is not None and z[3] is not None and z[1] == pytest.approx(-z[3])
    assert z[4] is None and z[5] is None  # sector B has two names only


def test_render_features_is_tsv_with_dimensionless_columns() -> None:
    rows = [
        _row(1, ev_sales=1.0, insider_distinct_buyers_90d=2, insider_ceo_cfo_buy_flag_90d=1),
        _row(2, ev_sales=2.5, insider_distinct_buyers_90d=0, insider_ceo_cfo_buy_flag_90d=0),
        _row(3, ev_sales=4.0, insider_distinct_buyers_90d=1, insider_ceo_cfo_buy_flag_90d=0),
    ]
    out = render_features(
        rows[0],
        universe=rows,
        sectors={1: "A", 2: "A", 3: "A"},
        names=["ev_sales", "insider_distinct_buyers_90d", "insider_ceo_cfo_buy_flag_90d"],
    )
    lines = out.splitlines()
    assert lines[0] == "feature\tvalue\tsector_z\tsector_pct"
    assert lines[1].split("\t")[:2] == ["ev_sales", "1"]
    assert lines[1].split("\t")[3] == "0"  # lowest of its sector
    assert lines[2] == "insider_distinct_buyers_90d\t2\t\t"  # counts carry no cross-section
    assert lines[3] == "insider_ceo_cfo_buy_flag_90d\t1\t\t"


def test_missing_values_render_as_na() -> None:
    rows = [_row(i, ev_sales=None) for i in (1, 2, 3)]
    sectors = {1: "A", 2: "A", 3: "A"}
    out = render_features(rows[0], universe=rows, sectors=sectors, names=["ev_sales"])
    assert out.splitlines()[1] == "ev_sales\tNA\tNA\tNA"


def test_currency_and_unknown_features_are_refused() -> None:
    """N1: the only fs_v1 feature in dollars is deliberately not renderable."""
    row = _row(1, insider_net_open_market_buy_usd_90d=1_234_567.0, mystery=3.14159)
    for name in ("insider_net_open_market_buy_usd_90d", "mystery"):
        with pytest.raises(RenderError, match=name):
            render_features(row, universe=[row], sectors={1: "A"}, names=[name])
    assert "insider_net_open_market_buy_usd_90d" not in FEATURE_KINDS


def test_every_allowed_feature_has_a_kind() -> None:
    assert set(FEATURE_KINDS.values()) <= set(Kind)
    assert FEATURE_KINDS["news_source_diversity_7d"] is Kind.COUNT
