"""Alias source: pure builder plus the point-in-time ``as_of.alias_list`` read path."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Connection

from contracts.data import Security
from contracts.enums import AliasKind
from store import as_of
from store.aliases import build_alias_list, short_names
from store.write import ensure_security, set_listing

AS_OF = datetime(2024, 6, 3, tzinfo=UTC)


def _sec(sid: int, ticker: str, cik: int, name: str) -> Security:
    return Security(
        security_id=sid,
        ticker=ticker,
        cik=cik,
        name=name,
        sector=None,
        industry=None,
        listed_from=None,
        listed_to=None,
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Acme Robotics Corp.", ["Acme Robotics"]),
        ("APPLE INC", ["APPLE"]),
        ("Acme Co Inc", ["Acme"]),
        ("Foo Bar, Inc.", ["Foo Bar"]),
        ("ACME CORP /DE/", ["ACME"]),
        ("Acme", []),
        ("Inc", []),
        ("Ab Co", []),
    ],
)
def test_short_names(name: str, expected: list[str]) -> None:
    assert short_names(name) == expected


def test_builder_collects_name_ticker_cik_brands() -> None:
    lst = build_alias_list(
        [_sec(1, "BRK.B", 1067983, "Berkshire Hathaway Inc")], AS_OF, {1067983: ["Geico", "geico"]}
    )
    got = {(a.kind, a.text) for a in lst.securities[0].aliases}
    assert got == {
        (AliasKind.NAME, "Berkshire Hathaway Inc"),
        (AliasKind.NAME, "Berkshire Hathaway"),
        (AliasKind.TICKER, "BRK.B"),
        (AliasKind.CIK, "1067983"),
        (AliasKind.BRAND, "Geico"),
    }


def test_brands_for_other_ciks_are_ignored() -> None:
    lst = build_alias_list([_sec(1, "AAA", 1, "Aaa Corp")], AS_OF, {2: ["Leak"]})
    assert all(a.text != "Leak" for a in lst.securities[0].aliases)


def test_alias_list_is_point_in_time(db: Connection) -> None:
    old = ensure_security(db, ticker="XYZ", cik=111, name="Old Xyz Corp")
    new = ensure_security(db, ticker="XYZ", cik=222, name="New Xyz Inc")
    future = ensure_security(db, ticker="FUT", cik=333, name="Future Corp")
    set_listing(db, old, listed_from=date(2010, 1, 1), listed_to=date(2023, 12, 31))
    set_listing(db, new, listed_from=date(2024, 1, 2), listed_to=None)
    set_listing(db, future, listed_from=date(2025, 1, 1), listed_to=None)

    brands = {111: ["OldBrand"], 222: ["NewBrand"], 333: ["FutureBrand"]}
    now = as_of.alias_list(db, AS_OF, brands)
    assert [s.security_id for s in now.securities] == [new]
    texts = {a.text for a in now.securities[0].aliases}
    assert texts == {"New Xyz Inc", "New Xyz", "XYZ", "222", "NewBrand"}

    past = as_of.alias_list(db, datetime(2023, 6, 1, tzinfo=UTC), brands)
    assert [s.security_id for s in past.securities] == [old]

    bounded = as_of.alias_list(db, AS_OF, brands, security_ids=[old])
    assert bounded.securities == ()


def test_alias_list_requires_aware_as_of(db: Connection) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        as_of.alias_list(db, datetime(2024, 6, 3))
