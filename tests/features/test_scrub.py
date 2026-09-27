"""Rule set N applied to free text (news) and evidence-ID columns on rendered tables."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from contracts.data import FeatureRow
from features.renderer import render_features, scrub_text
from tests.agents.leak import calendar_dates, numeric_leaks

AS_OF = datetime(2025, 3, 7, 21, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Revenue rose to $12.4 billion.", "Revenue rose to [AMOUNT]."),
        ("A $3.2M grant and $1,250 fee", "A [AMOUNT] grant and [AMOUNT] fee"),
        ("USD 5 million and EUR 40bn", "[AMOUNT] and [AMOUNT]"),
        ("12.5 billion dollars of debt", "[AMOUNT] of debt"),
        ("bought 2.5 million shares", "bought [SHARES]"),
        ("holds 1,234,567 shares outright", "holds [SHARES] outright"),
        ("3 million customers", "[NUM] customers"),
        ("filed on 2024-03-31", "filed on [DATE]"),
        ("filed 03/31/2024 and Mar 31, 2024", "filed [DATE] and [DATE]"),
        ("in Q3 2024 and FY2023", "in [DATE] and [DATE]"),
        ("stock at 187.43 today", "stock at [NUM] today"),
        ("margin 42% and 5G rollout", "margin 42% and 5G rollout"),
    ],
)
def test_scrub_text(raw: str, expected: str) -> None:
    assert scrub_text(raw) == expected


def test_scrubbed_text_has_no_raw_amounts_or_dates() -> None:
    text = (
        "On March 3, 2024 the board approved a $1,234,567,890 buyback of 812,345,678 shares "
        "after revenue of $61.2 billion (2024-02-15). Shares closed at $187.4321."
    )
    out = scrub_text(text)
    assert calendar_dates(out) == []
    assert numeric_leaks(out, [1_234_567_890.0, 812_345_678.0, 61_234_567_000.0, 187.4321]) == []


def test_evidence_ids_are_sequential_and_prefixed() -> None:
    rows = [
        FeatureRow(
            security_id=i,
            event_time=AS_OF,
            available_at=AS_OF,
            source_version="fs_v1",
            feature_set_version="fs_v1",
            values={"ev_sales": float(i), "return_1m": 0.1 * i},
        )
        for i in (1, 2, 3)
    ]
    out = render_features(
        rows[0],
        universe=rows,
        sectors={1: "A", 2: "A", 3: "A"},
        names=["ev_sales", "return_1m"],
        evidence_prefix="TECH",
    )
    lines = out.splitlines()
    assert lines[0] == "evidence_id\tfeature\tvalue\tsector_z\tsector_pct"
    assert [ln.split("\t")[0] for ln in lines[1:]] == ["TECH_1", "TECH_2"]
