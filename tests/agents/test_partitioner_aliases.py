"""AliasMasker: invariant 4 replacement rules (case, boundaries, overlaps, tickers, Unicode)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agents.partitioner import SHARED_TOKEN, AliasMasker
from contracts.data import Alias, AliasList, SecurityAliases
from contracts.enums import AliasKind

N, T, C, B = AliasKind.NAME, AliasKind.TICKER, AliasKind.CIK, AliasKind.BRAND
AS_OF = datetime(2024, 6, 3, tzinfo=UTC)


def sec(sid: int, *aliases: tuple[str, AliasKind]) -> SecurityAliases:
    return SecurityAliases(
        security_id=sid, aliases=tuple(Alias(text=t, kind=k) for t, k in aliases)
    )


ACME = sec(1, ("Acme Corp.", N), ("Acme", N), ("ACME", T), ("320193", C), ("Widget Pro", B))
ACME_ROBOTICS = sec(2, ("Acme Robotics", N), ("ACRB", T), ("999", C))
BERKSHIRE = sec(3, ("Berkshire Hathaway", N), ("BRK.B", T), ("1067983", C))
NESTLE = sec(4, ("Nestlé", N), ("NESN", T), ("1234567", C), ("Straße", B))
TINY = sec(5, ("Ge Holdings", N), ("GE", T), ("42", C))


def masker(*securities: SecurityAliases) -> AliasMasker:
    tokens = {s.security_id: f"ENTITY_{s.security_id:02d}" for s in securities}
    return AliasMasker(AliasList(as_of=AS_OF, securities=securities), tokens)


@pytest.mark.parametrize("text", ["acme rose", "ACME rose", "AcMe rose"])
def test_case_insensitive(text: str) -> None:
    assert masker(ACME).mask(text) == "ENTITY_01 rose"


def test_case_insensitive_multiword_with_punctuation() -> None:
    assert masker(ACME).mask("ACME CORP. rose; acme corp. fell") == "ENTITY_01 rose; ENTITY_01 fell"


def test_word_boundaries() -> None:
    out = masker(ACME).mask("Acmeville, macme, acme_x and Acme.")
    assert out == "Acmeville, macme, acme_x and ENTITY_01."


def test_multiword_alias_spans_whitespace_and_newlines() -> None:
    assert masker(ACME).mask("new Widget\n  Pro line") == "new ENTITY_01 line"


def test_longest_alias_wins_on_overlap() -> None:
    m = masker(ACME, ACME_ROBOTICS)
    assert m.mask("Acme Robotics beat Acme") == "ENTITY_02 beat ENTITY_01"


def test_overlap_at_different_starts_is_leftmost() -> None:
    a = sec(1, ("Alpha Beta", N), ("ALPB", T), ("11", C))
    b = sec(2, ("Beta Gamma", N), ("BETG", T), ("22", C))
    assert masker(a, b).mask("Alpha Beta Gamma") == "ENTITY_01 Gamma"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("BRK.B rallied", "ENTITY_03 rallied"),
        ("brk-b rallied", "ENTITY_03 rallied"),
        ("BRK/B rallied", "ENTITY_03 rallied"),
        ("$BRK.B rallied", "ENTITY_03 rallied"),
        ("BRK.B.", "ENTITY_03."),
        ("BRKXB and BRK.BX", "BRKXB and BRK.BX"),
    ],
)
def test_punctuation_bearing_ticker(text: str, expected: str) -> None:
    assert masker(BERKSHIRE).mask(text) == expected


@pytest.mark.parametrize("apostrophe", ["'", "’"])  # noqa: RUF001
def test_possessives(apostrophe: str) -> None:
    m = masker(ACME)
    assert m.mask(f"Acme{apostrophe}s outlook") == "ENTITY_01's outlook"
    assert m.mask(f"the Acme{apostrophe} board") == "the ENTITY_01' board"


def test_unicode_names_and_normalization() -> None:
    m = masker(NESTLE)
    assert m.mask("NESTLÉ and nestlé") == "ENTITY_04 and ENTITY_04"
    assert m.mask("Nestlé (decomposed)") == "ENTITY_04 (decomposed)"
    assert m.mask("ＮＥＳＮ full-width") == "ENTITY_04 full-width"  # noqa: RUF001
    assert m.mask("Nestléville") == "Nestléville"


def test_casefold_expansion() -> None:
    m = masker(NESTLE)
    assert m.mask("STRASSE and Straße and STRAẞE") == "ENTITY_04 and ENTITY_04 and ENTITY_04"


def test_short_tickers_match_only_in_capitals() -> None:
    m = masker(TINY)
    assert m.mask("GE guided; ge a go; $GE up") == "ENTITY_05 guided; ge a go; ENTITY_05 up"


def test_cik_forms() -> None:
    m = masker(ACME)
    assert m.mask("CIK 0000320193 and CIK: 320193") == "ENTITY_01 and ENTITY_01"
    assert m.mask("filer 0000320193") == "filer ENTITY_01"
    assert m.mask("revenue of 320193 dollars") == "revenue of 320193 dollars"
    assert m.mask("id 10000320193") == "id 10000320193"


def test_brands_and_names_of_same_security_share_a_token() -> None:
    assert masker(ACME).mask("Widget Pro by Acme Corp.") == "ENTITY_01 by ENTITY_01"


def test_alias_shared_by_several_securities_uses_neutral_token() -> None:
    a = sec(1, ("Alphabet Inc", N), ("GOOGL", T), ("1652044", C))
    b = sec(2, ("Alphabet Inc", N), ("GOOG", T), ("1652044", C))
    m = masker(a, b)
    assert m.mask("Alphabet Inc beat; GOOGL up; GOOG flat") == (
        f"{SHARED_TOKEN} beat; ENTITY_01 up; ENTITY_02 flat"
    )
    assert m.mask("CIK 1652044") == SHARED_TOKEN


def test_shared_alias_across_kinds_and_case() -> None:
    a = sec(1, ("Orbit Systems", N), ("ORBT", T), ("11", C), ("Nova", B))
    b = sec(2, ("Nova Labs", N), ("NOVA", T), ("22", C))
    m = masker(a, b)
    assert m.mask("nova and NOVA and Nova Labs") == (
        f"{SHARED_TOKEN} and {SHARED_TOKEN} and ENTITY_02"
    )


def test_short_ticker_collision_is_shared_only_for_the_exact_text() -> None:
    a = sec(1, ("Ab Holdings", N), ("AB", T), ("11", C))
    b = sec(2, ("Other Co", N), ("OTHR", T), ("22", C), ("Ab", B))
    m = masker(a, b)
    assert m.mask("AB") == SHARED_TOKEN
    assert m.mask("Ab") == "ENTITY_02"


def test_output_never_contains_an_identity_string() -> None:
    m = masker(ACME, ACME_ROBOTICS, BERKSHIRE, NESTLE)
    text = "Acme Robotics (ACRB), Nestlé's NESN, BRK.B and Widget Pro; CIK 320193."
    out = m.mask(text).casefold()
    for needle in ("acme", "acrb", "nestl", "nesn", "brk", "widget", "320193"):
        assert needle not in out


def test_replacement_is_idempotent() -> None:
    m = masker(ACME)
    once = m.mask("Acme")
    assert m.mask(once) == once == "ENTITY_01"


def test_empty_alias_list_only_normalizes() -> None:
    assert AliasMasker(AliasList(as_of=AS_OF, securities=()), {}).mask("Acme") == "Acme"


def test_constructor_validates_tokens() -> None:
    aliases = AliasList(as_of=AS_OF, securities=(ACME,))
    with pytest.raises(ValueError, match="no entity token"):
        AliasMasker(aliases, {})
    with pytest.raises(ValueError):
        AliasMasker(aliases, {1: "acme"})
    with pytest.raises(ValueError, match="reserved"):
        AliasMasker(aliases, {1: SHARED_TOKEN})


# --- PERSON aliases (named insiders) ----------------------------------------------------------


def _with_people(filers: dict[int, list[str]]) -> AliasList:
    from store.aliases import add_people

    base = AliasList(as_of=AS_OF, securities=(ACME, ACME_ROBOTICS))
    return add_people(base, filers)


def test_person_token_is_stable_hashed_and_company_scoped() -> None:
    from agents.partitioner import person_token

    a = person_token("SMITH JOHN A", 320193)
    assert a == person_token("  smith   john a ", 320193)  # canonicalised
    assert a != person_token("SMITH JOHN A", 999)  # scoped to the company
    assert a.startswith("EXEC_") and len(a) == len("EXEC_") + 8
    assert all(c in "0123456789ABCDEF" for c in a[5:])


def test_every_variant_of_a_filer_maps_to_one_token() -> None:
    from agents.partitioner import person_token

    masker = AliasMasker(_with_people({1: ["Rivera Maria L"]}), {1: "TICKER_01", 2: "TICKER_02"})
    token = person_token("Rivera Maria L", 320193)
    for text in ("RIVERA MARIA L", "Maria Rivera", "Maria L Rivera", "Maria L. Rivera", "Rivera"):
        assert masker.mask(f"said {text} today") == f"said {token} today"
    assert "Rivera" not in masker.mask("CEO Maria Rivera's plan; Rivera declined")


def test_short_surnames_are_not_masked_alone_but_full_names_are() -> None:
    masker = AliasMasker(_with_people({1: ["Cook Tim D"]}), {1: "TICKER_01", 2: "TICKER_02"})
    assert masker.mask("A cook prepared it") == "A cook prepared it"
    assert "Tim Cook" not in masker.mask("Tim Cook resigned")


def test_person_shared_by_two_companies_masks_to_shared_token() -> None:
    masker = AliasMasker(
        _with_people({1: ["Rivera Maria L"], 2: ["Rivera Maria L"]}),
        {1: "TICKER_01", 2: "TICKER_02"},
    )
    assert masker.mask("Maria Rivera") == SHARED_TOKEN


def test_person_alias_contract_requires_canonical_only_for_person() -> None:
    with pytest.raises(ValueError, match="canonical"):
        Alias(text="Maria Rivera", kind=AliasKind.PERSON)
    with pytest.raises(ValueError, match="canonical"):
        Alias(text="Acme", kind=AliasKind.NAME, canonical="Acme")
