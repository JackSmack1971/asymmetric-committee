"""Build the anonymizer alias list from reference rows (pure; no SQL). Read it through
``store.as_of.alias_list`` so only securities listed at ``as_of`` are included (invariant 3)."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime

from contracts.data import Alias, AliasList, Security, SecurityAliases
from contracts.enums import AliasKind

_LEGAL_TAIL = re.compile(
    r"[\s,.]*(?:/[A-Za-z]{2,3}/?|\b(?:inc|incorporated|corp|corporation|co|company|ltd|limited"
    r"|llc|lp|plc|sa|nv|ag))\.?\s*$",
    re.IGNORECASE,
)
_MIN_SHORT_NAME = 3


def short_names(name: str) -> list[str]:
    """``"Acme Robotics Corp."`` -> ``["Acme Robotics"]``: the form prose actually uses."""
    out: list[str] = []
    current = name.strip()
    while (m := _LEGAL_TAIL.search(current)) and m.start() > 0:
        current = current[: m.start()].strip(" ,.")
        if len(current) >= _MIN_SHORT_NAME:
            out.append(current)
    return out[-1:]


def security_aliases(
    security: Security,
    brands: Mapping[int, Sequence[str]] | None = None,
    symbols: Sequence[str] = (),
) -> SecurityAliases:
    """``symbols`` are every ticker the security has traded under (P6.4 ``security_symbols``): a
    renamed security's former ticker must stay masked."""
    candidates = [
        (security.name.strip(), AliasKind.NAME),
        *((n, AliasKind.NAME) for n in short_names(security.name)),
        (security.ticker, AliasKind.TICKER),
        *((sym, AliasKind.TICKER) for sym in symbols),
        (str(security.cik), AliasKind.CIK),
        *((b.strip(), AliasKind.BRAND) for b in (brands or {}).get(security.cik, ())),
    ]
    seen: set[tuple[AliasKind, str]] = set()
    aliases: list[Alias] = []
    for text, kind in candidates:
        key = (kind, text.casefold())
        if key not in seen:
            seen.add(key)
            aliases.append(Alias(text=text, kind=kind))
    return SecurityAliases(security_id=security.security_id, aliases=tuple(aliases))


_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "md", "phd"}
_MIN_SURNAME = 5  # shorter surnames are common words ("Cook", "May"); full names still mask


def name_variants(filed: str) -> list[str]:
    """Forms of a Form 4 owner name that prose uses. EDGAR files ``"SMITH JOHN A"`` (surname
    first); ``"Smith, John A"`` is also handled. Surname-only is included from 5 letters up."""
    text = filed.strip()
    if "," in text:
        last, _, rest = text.partition(",")
        last, parts = last.strip(), rest.split()
    else:
        last, *parts = text.split()
    parts = [p.strip(".") for p in parts if p.strip(".").lower() not in _SUFFIXES]
    if not last or not parts:
        return [text]
    given, initials = parts[0], parts[1:]
    forms = [text, f"{given} {last}"]
    if initials:
        forms.append(f"{given} {' '.join(initials)} {last}")
        forms.append(f"{given} {' '.join(i + '.' for i in initials)} {last}")
    if last.isalpha() and len(last) >= _MIN_SURNAME:
        forms.append(last)
    return list(dict.fromkeys(f for f in forms if len(f) >= _MIN_SHORT_NAME))


def add_people(alias_list: AliasList, filers: Mapping[int, Sequence[str]]) -> AliasList:
    """Add PERSON aliases (Form 4 filers, by security_id) so the masker hides named insiders."""
    out: list[SecurityAliases] = []
    for sec in alias_list.securities:
        seen = {(a.kind, a.text.casefold()) for a in sec.aliases}
        extra: list[Alias] = []
        for filer in dict.fromkeys(filers.get(sec.security_id, ())):
            for text in name_variants(filer):
                if (AliasKind.PERSON, text.casefold()) not in seen:
                    seen.add((AliasKind.PERSON, text.casefold()))
                    extra.append(Alias(text=text, kind=AliasKind.PERSON, canonical=filer.strip()))
        out.append(SecurityAliases(security_id=sec.security_id, aliases=(*sec.aliases, *extra)))
    return AliasList(as_of=alias_list.as_of, securities=tuple(out))


def build_alias_list(
    securities: Sequence[Security],
    as_of: datetime,
    brands: Mapping[int, Sequence[str]] | None = None,
    symbols: Mapping[int, Sequence[str]] | None = None,
) -> AliasList:
    """``brands`` maps CIK -> brand/product aliases (``config.aliases``); ``symbols`` maps
    security_id -> every ticker it has traded under."""
    return AliasList(
        as_of=as_of,
        securities=tuple(
            security_aliases(s, brands, (symbols or {}).get(s.security_id, ())) for s in securities
        ),
    )
