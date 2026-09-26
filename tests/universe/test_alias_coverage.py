"""Short-name coverage guard for the anonymizer (§12.1, invariant 4)."""

from __future__ import annotations

from datetime import date

import pytest

from contracts.data import Security
from universe.snapshot import (
    AliasCoverageError,
    alias_coverage_gaps,
    assert_alias_coverage,
    colloquial_short_name,
)


def sec(sid: int, name: str, cik: int) -> Security:
    return Security(
        security_id=sid,
        ticker=f"T{sid}",
        cik=cik,
        name=name,
        sector="Semiconductor equipment",
        industry=None,
        listed_from=date(2015, 1, 1),
        listed_to=None,
    )


@pytest.mark.parametrize(
    ("name", "short"),
    [
        ("Zephyr Dynamics Corp.", "Zephyr"),
        ("Borealis Systems Inc", "Borealis"),
        ("Acme Corp", None),  # stripped name is one word: already covered by identity aliases
        ("Zephyr", None),
        ("Al Fresco Holdings Inc", None),  # first word under 3 characters
        ("Texas Instruments Inc", None),  # stop word: never a standalone alias
        ("applied Materials Inc", None),  # case-insensitive
        ("Appliedx Systems Inc", "Appliedx"),  # only whole words are blocked
    ],
)
def test_colloquial_short_name(name: str, short: str | None) -> None:
    assert colloquial_short_name(name) == short


def test_gap_when_short_name_is_not_a_brand_alias() -> None:
    securities = [sec(1, "Zephyr Dynamics Corp.", 11), sec(2, "Borealis Systems Inc", 22)]
    assert alias_coverage_gaps(securities, {11: ["zephyr"]}) == {2: "Borealis"}
    with pytest.raises(AliasCoverageError, match="Borealis"):
        assert_alias_coverage(securities, {11: ["zephyr"]})


def test_brand_for_another_cik_does_not_cover() -> None:
    assert alias_coverage_gaps([sec(1, "Zephyr Dynamics Corp.", 11)], {99: ["Zephyr"]}) == {
        1: "Zephyr"
    }


def test_complete_coverage_passes() -> None:
    assert_alias_coverage([sec(1, "Zephyr Dynamics Corp.", 11)], {11: ["Zephyr", "Nimbus Edge"]})
