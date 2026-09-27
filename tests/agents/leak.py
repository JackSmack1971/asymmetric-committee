"""Prompt-audit helpers for the numeric and date leak tests (§12.1 item 3, §13)."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable

_NUMBER = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?")
_DATES = (
    re.compile(r"\b(?:19|20)\d{2}[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])\b"),
    re.compile(r"\b(?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])[-/.](?:19|20)?\d{2}\b"),
    re.compile(
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}"
        r"(?:,\s*\d{4})?\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)"),  # a bare year is still a calendar anchor
)
SIG = 4


def _digits(token: str) -> str:
    mantissa = re.split(r"[eE]", token)[0]
    return re.sub(r"\D", "", mantissa).lstrip("0")


def significant(value: float) -> set[str]:
    """First four significant digits of ``value``, truncated and rounded; empty if it has fewer."""
    if value == 0 or not math.isfinite(value):
        return set()
    text = f"{abs(value):.15g}"
    if len(_digits(text).rstrip("0")) < SIG:
        return set()
    return {_digits(text)[:SIG], _digits(f"{abs(value):.{SIG - 1}e}")[:SIG]}


def numeric_leaks(text: str, raw_values: Iterable[float]) -> list[str]:
    """Raw values whose first four significant digits start any number in ``text``."""
    tokens = [_digits(m.group()) for m in _NUMBER.finditer(text)]
    hits: list[str] = []
    for raw in raw_values:
        prefixes = significant(raw)
        if any(tok[:SIG] == p for tok in tokens if len(tok) >= SIG for p in prefixes):
            hits.append(repr(raw))
    return hits


def calendar_dates(text: str) -> list[str]:
    return [m.group() for pattern in _DATES for m in pattern.finditer(text)]
