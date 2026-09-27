"""CTS / UTP last-sale eligibility (§4.6): tables, combination rule, fail-closed mapping."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from contracts.enums import ConditionEligibility as CE
from contracts.enums import RefReason, Tape
from execution.trade_conditions import (
    CTS_LAST,
    UTP_LAST,
    Effect,
    ProviderCodeMap,
    evaluate_conditions,
    load_provider_map,
    parse_provider_map,
)
from tests.market_data_support import validated_map_for_tests

MAP = validated_map_for_tests()


def codes(table: Mapping[str, Effect], effect: Effect) -> set[str]:
    return {c for c, e in table.items() if e is effect}


# --- the tables are the specification's, transcribed --------------------------------------------


def test_cts_table_matches_the_specification() -> None:
    """CTS Pillar Output Specification v2.11b, Sale Condition field: CONSOLIDATED / LAST column."""
    assert codes(CTS_LAST, Effect.YES) == {" ", "E", "F", "K", "O", "X", "5", "6", "9"}
    assert codes(CTS_LAST, Effect.FIRST_ONLY) == {"P", "Z", "4"}  # Note #2
    assert codes(CTS_LAST, Effect.NOTE3) == {"L"}  # Note #3
    assert codes(CTS_LAST, Effect.NO) == set("BCHIMNQRTUV78")
    assert codes(CTS_LAST, Effect.TBD) == set()


def test_utp_table_matches_the_specification() -> None:
    """UTP Data Feed Services Specification v4.1, 3.14.2: Consolidated / Update Last column."""
    assert codes(UTP_LAST, Effect.YES) == set("@ABDFKLOSXY1569")
    assert codes(UTP_LAST, Effect.FIRST_ONLY) == {"G", "P", "Z", "4"}  # footnote 1
    assert codes(UTP_LAST, Effect.TBD) == {"E", "8"}
    assert codes(UTP_LAST, Effect.NO) == set("CHIMNQRTUVW7")


def test_the_same_letter_means_different_things_on_different_tapes() -> None:
    assert evaluate_conditions(Tape.A, ["B"], MAP).status is CE.INELIGIBLE  # average price (CTS)
    assert evaluate_conditions(Tape.C, ["B"], MAP).status is CE.ELIGIBLE  # bunched trade (UTP)
    assert evaluate_conditions(Tape.A, ["E"], MAP).status is CE.ELIGIBLE  # automatic execution
    assert evaluate_conditions(Tape.C, ["E"], MAP).status is CE.UNKNOWN  # placeholder, TBD
    assert evaluate_conditions(Tape.B, ["W"], MAP).status is CE.UNKNOWN  # no W on CTS: unmapped
    assert evaluate_conditions(Tape.C, ["W"], MAP).status is CE.INELIGIBLE  # average price (UTP)


# --- eligible / ineligible / unknown -----------------------------------------------------------


@pytest.mark.parametrize("tape", list(Tape))
def test_regular_sale_is_eligible_on_every_tape(tape: Tape) -> None:
    assert evaluate_conditions(tape, ["@"], MAP).status is CE.ELIGIBLE


@pytest.mark.parametrize(
    ("tape", "code"),
    [(Tape.A, "T"), (Tape.B, "I"), (Tape.A, "M"), (Tape.C, "T"), (Tape.C, "U"), (Tape.C, "7")],
)
def test_ineligible_codes(tape: Tape, code: str) -> None:
    assert evaluate_conditions(tape, [code], MAP).status is CE.INELIGIBLE


@pytest.mark.parametrize("tape", list(Tape))
def test_unknown_code_is_unknown_never_eligible(tape: Tape) -> None:
    result = evaluate_conditions(tape, ["?"], MAP)
    assert result.status is CE.UNKNOWN and result.reason is RefReason.UNKNOWN_CONDITION


def test_no_conditions_at_all_is_unknown_not_a_regular_sale() -> None:
    assert evaluate_conditions(Tape.C, [], MAP).status is CE.UNKNOWN


def test_conditional_codes() -> None:
    assert evaluate_conditions(Tape.C, ["Z"], MAP).status is CE.CONDITIONAL
    l_cts = evaluate_conditions(Tape.A, ["L"], MAP)
    assert l_cts.status is CE.CONDITIONAL and l_cts.note3
    assert evaluate_conditions(Tape.C, ["L"], MAP).status is CE.ELIGIBLE  # before 16:00:10


# --- several conditions on one trade ------------------------------------------------------------


def test_every_condition_must_qualify() -> None:
    assert evaluate_conditions(Tape.C, ["@", "F"], MAP).status is CE.ELIGIBLE
    assert evaluate_conditions(Tape.C, ["@", "I"], MAP).status is CE.INELIGIBLE
    assert evaluate_conditions(Tape.A, ["@", "F", "T"], MAP).status is CE.INELIGIBLE


def test_one_no_disqualifies_even_beside_an_unknown_code() -> None:
    assert evaluate_conditions(Tape.C, ["?", "T"], MAP).status is CE.INELIGIBLE


def test_an_unknown_code_beside_qualifying_codes_is_unknown() -> None:
    assert evaluate_conditions(Tape.C, ["@", "?"], MAP).status is CE.UNKNOWN


def test_conditional_beside_eligible_stays_conditional() -> None:
    assert evaluate_conditions(Tape.C, ["@", "4"], MAP).status is CE.CONDITIONAL


# --- provider mapping is fail-closed ------------------------------------------------------------


def test_the_checked_in_map_ships_entirely_unvalidated() -> None:
    entries = load_provider_map().entries
    assert entries and all(not e.validated for e in entries)
    assert {e.tape for e in entries} == set(Tape)


def test_an_unvalidated_mapping_is_never_accepted() -> None:
    shipped = load_provider_map()
    for tape in Tape:
        result = evaluate_conditions(tape, ["@"], shipped)
        assert result.status is CE.UNKNOWN
        assert result.reason is RefReason.PROVIDER_MAPPING_UNVALIDATED
    assert not shipped.any_validated()


def test_there_is_no_flag_that_promotes_a_map() -> None:
    """Validation lives only in the checked-in file; the class has no switch to bypass it."""
    assert not hasattr(ProviderCodeMap, "allow_unvalidated")
    text = """
tapes:
  A:
    - {provider_code: "@", spec_code: " ", expected_provider_name: x, spec_name: x, validated: true}
"""
    assert parse_provider_map(text).any_validated()  # only the file can say so


def test_regular_sale_on_ab_translates_to_the_cts_space_code() -> None:
    entry = {(e.tape, e.provider_code): e for e in load_provider_map().entries}[(Tape.A, "@")]
    assert entry.spec_code == " "
