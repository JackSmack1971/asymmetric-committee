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
    security: Security, brands: Mapping[int, Sequence[str]] | None = None
) -> SecurityAliases:
    candidates = [
        (security.name.strip(), AliasKind.NAME),
        *((n, AliasKind.NAME) for n in short_names(security.name)),
        (security.ticker, AliasKind.TICKER),
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


def build_alias_list(
    securities: Sequence[Security],
    as_of: datetime,
    brands: Mapping[int, Sequence[str]] | None = None,
) -> AliasList:
    """``brands`` maps CIK -> brand/product aliases (``config.aliases``)."""
    return AliasList(
        as_of=as_of,
        securities=tuple(security_aliases(s, brands) for s in securities),
    )
