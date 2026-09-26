"""Partitioner anonymizer (invariant 4): removes identity strings from LLM-bound text.

Only the alias-masking half exists so far; data isolation lands with the P3 agents. Aliases come
from ``store.as_of.alias_list`` (point-in-time), never from ad-hoc lookups.

Matching rules, all covered by tests:
- Unicode-aware whole-word matching (NFKC-normalised input, curly apostrophes folded to ``'``), so
  ``Acme`` never matches inside ``Acmeville`` and possessives (``Acme's``) still match.
- Case-insensitive, except tickers of at most two characters (``A``, ``GE``), which match only in
  capitals: masking the word "a" everywhere would wreck the text. A ``$`` cashtag prefix is masked
  with the ticker, and ``.``, ``-`` and ``/`` are interchangeable inside tickers (``BRK.B``).
- The longest alias wins where several start at the same position.
- Bare CIK digits are masked only after a ``CIK`` label (they would otherwise hit ordinary
  numbers); the 10-digit zero-padded form is masked anywhere.
- An alias that belongs to several securities is replaced by ``SHARED_TOKEN``, never by one
  security's token.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping

from pydantic import TypeAdapter

from contracts.data import Alias, AliasList
from contracts.enums import AliasKind
from contracts.models import EntityToken

SHARED_TOKEN = "ENTITY_00"

_TOKEN = TypeAdapter(EntityToken)
_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'"})
_TICKER_SEP = re.compile(r"[.\-/]")
_TICKER_SEP_CLASS = r"[.\-/]"
_CIK_TEXT = re.compile(r"(?:cik(?:[\s:#=\-]|no\.?)*)?0*(\d+)")
_SHORT_TICKER = 2


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_APOSTROPHES)


def _canon(text: str) -> str:
    return " ".join(_normalize(text).split())


def _words(text: str) -> str:
    return r"\s+".join(re.escape(part) for part in text.split(" "))


def _fragments(alias: Alias) -> tuple[list[str], list[str]]:
    """Regex fragments and the lookup keys a match of them resolves through."""
    canon = _canon(alias.text)
    match alias.kind:
        case AliasKind.NAME | AliasKind.BRAND:
            forms = dict.fromkeys((canon, canon.casefold()))
            return [f"(?i:{'|'.join(_words(f) for f in forms)})"], [canon.casefold()]
        case AliasKind.TICKER:
            body = _TICKER_SEP_CLASS.join(re.escape(p) for p in _TICKER_SEP.split(canon))
            short = len(re.sub(r"\W", "", canon)) <= _SHORT_TICKER
            flag = "-i" if short else "i"
            key = f"S:{canon}" if short else _TICKER_SEP.sub(".", canon.casefold())
            return [rf"\$?(?{flag}:{body})"], [key]
        case AliasKind.CIK:
            digits = str(int(canon))
            padded = digits.zfill(10)
            return (
                [rf"(?i:cik(?:[\s:#=\-]|no\.?)*)0*{digits}", re.escape(padded)],
                [f"cik:{digits}"],
            )
    raise AssertionError(alias.kind)  # pragma: no cover


def _candidate_keys(matched: str) -> list[str]:
    canon = _canon(matched).removeprefix("$")
    folded = canon.casefold()
    keys = [folded, _TICKER_SEP.sub(".", folded), f"S:{canon}"]
    if m := _CIK_TEXT.fullmatch(folded):
        keys.append(f"cik:{m.group(1)}")
    return keys


class AliasMasker:
    """Replace every alias in a text with its security's entity token."""

    def __init__(self, aliases: AliasList, tokens: Mapping[int, str]) -> None:
        missing = {s.security_id for s in aliases.securities} - set(tokens)
        if missing:
            raise ValueError(f"no entity token for securities {sorted(missing)}")
        for token in tokens.values():
            _TOKEN.validate_python(token)
        if SHARED_TOKEN in tokens.values():
            raise ValueError(f"{SHARED_TOKEN} is reserved for shared aliases")
        self._tokens = dict(tokens)
        self._owners: dict[str, set[int]] = {}
        fragments: dict[str, int] = {}
        for sec in aliases.securities:
            for alias in sec.aliases:
                frags, keys = _fragments(alias)
                for key in keys:
                    self._owners.setdefault(key, set()).add(sec.security_id)
                for frag in frags:
                    fragments[frag] = max(fragments.get(frag, 0), len(_canon(alias.text)))
        ordered = sorted(fragments, key=lambda f: (-fragments[f], f))
        self._pattern = re.compile(rf"(?<!\w)(?:{'|'.join(ordered)})(?!\w)") if ordered else None

    def _replace(self, match: re.Match[str]) -> str:
        owners: set[int] = set()
        for key in _candidate_keys(match.group()):
            owners |= self._owners.get(key, set())
        if not owners:
            return match.group()
        return SHARED_TOKEN if len(owners) > 1 else self._tokens[next(iter(owners))]

    def mask(self, text: str) -> str:
        text = _normalize(text)
        return text if self._pattern is None else self._pattern.sub(self._replace, text)
