"""Alias contract (invariant 1): strict shape, mandatory identity kinds, no duplicates."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from contracts.data import Alias, AliasList, SecurityAliases
from contracts.enums import AliasKind

AS_OF = datetime(2024, 6, 3, tzinfo=UTC)


def _aliases(*extra: Alias) -> tuple[Alias, ...]:
    return (
        Alias(text="Acme Corp", kind=AliasKind.NAME),
        Alias(text="ACME", kind=AliasKind.TICKER),
        Alias(text="320193", kind=AliasKind.CIK),
        *extra,
    )


def test_valid_round_trip() -> None:
    lst = AliasList(
        as_of=AS_OF,
        securities=(
            SecurityAliases(
                security_id=1, aliases=_aliases(Alias(text="Widget", kind=AliasKind.BRAND))
            ),
        ),
    )
    assert AliasList.model_validate_json(lst.model_dump_json()) == lst


@pytest.mark.parametrize(
    "kwargs",
    [
        {"text": "", "kind": "name"},
        {"text": " padded", "kind": "name"},
        {"text": "x" * 257, "kind": "name"},
        {"text": "Acme", "kind": "nickname"},
        {"text": "12a", "kind": "cik"},
        {"text": "0", "kind": "cik"},
        {"text": "٣٢٠", "kind": "cik"},
        {"text": "acme", "kind": "ticker"},
        {"text": "Acme", "kind": "name", "extra": 1},
    ],
)
def test_bad_alias_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Alias.model_validate(kwargs)


def test_mandatory_kinds() -> None:
    for drop in (AliasKind.NAME, AliasKind.TICKER, AliasKind.CIK):
        kept = tuple(a for a in _aliases() if a.kind is not drop)
        with pytest.raises(ValidationError):
            SecurityAliases(security_id=1, aliases=kept)
    with pytest.raises(ValidationError):
        SecurityAliases(security_id=1, aliases=())


def test_duplicate_alias_within_security_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate alias"):
        SecurityAliases(
            security_id=1, aliases=_aliases(Alias(text="ACME CORP", kind=AliasKind.NAME))
        )


def test_alias_list_rejects_duplicate_security_and_naive_as_of() -> None:
    sa = SecurityAliases(security_id=1, aliases=_aliases())
    with pytest.raises(ValidationError, match="duplicate security_id"):
        AliasList(as_of=AS_OF, securities=(sa, sa))
    with pytest.raises(ValidationError):
        AliasList(as_of=datetime(2024, 6, 3), securities=(sa,))


def test_frozen() -> None:
    sa = SecurityAliases(security_id=1, aliases=_aliases())
    with pytest.raises(ValidationError):
        sa.security_id = 2
