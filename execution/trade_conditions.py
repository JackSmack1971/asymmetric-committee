"""Which trades may set the consolidated last sale price (§4.6). Pure apart from reading the map.

The tables below are the normative consolidated-last effect of every sale condition, transcribed
from:

* CTA CTS Pillar Multicast Output Binary Specification v2.11b (29 Jan 2026), "Sale Condition"
  field description, column CONSOLIDATED / Update / LAST and Notes #2, #3, #4. Tapes A and B.
* UTP Data Feed Services Specification v4.1 (Sept 2026), section 3.14.2 "UTP Sale Condition
  Matrix", column Consolidated / Update Last and its footnotes. Tape C.

Alpaca's condition metadata is not normative. It only translates the provider's code for a tape
into the specification's code (`ProviderCodeMap`), and an entry counts only once its ``validated``
flag is true. Nothing in this repository sets that flag: the checked-in map ships every entry
unvalidated, so resolution that depends on it fails closed until the owner commits evidence from
the credentialed probe.

Multiple conditions: the trade qualifies only if every condition qualifies; one NO disqualifies it.
Anything the tables cannot decide (an unknown code, a code the specification marks TBD, an
unmapped or unvalidated provider code) is UNKNOWN and never skipped.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml

from contracts.enums import ConditionEligibility, RefReason, Tape


class Effect(StrEnum):
    """Consolidated-last effect of one sale condition, as the specification words it."""

    YES = "yes"
    NO = "no"
    FIRST_ONLY = (
        "first_only"  # CTS Note #2 / UTP footnote 1: only if the first or only qualifying last
    )
    NOTE3 = "note3"  # CTS Note #3 (L): also needs the participant / listing market, which we lack
    TBD = "tbd"  # the specification itself leaves it undefined


Y, N, F, T3, TBD = Effect.YES, Effect.NO, Effect.FIRST_ONLY, Effect.NOTE3, Effect.TBD

# CTS (tapes A, B). The regular-sale code is a space.
CTS_LAST: Mapping[str, Effect] = {
    " ": Y,  # Regular Sale
    "B": N,  # Average Price Trade
    "C": N,  # Cash Trade (Same Day Clearing)
    "E": Y,  # Automatic Execution
    "F": Y,  # Intermarket Sweep Order
    "H": N,  # Price Variation Trade
    "I": N,  # Odd Lot Trade
    "K": Y,  # Rule 127 (NYSE) / Rule 155 (NYSE American)
    "L": T3,  # Sold Last (Late Reporting)
    "M": N,  # Market Center Official Close
    "N": N,  # Reserved
    "O": Y,  # Market Center Opening Trade
    "P": F,  # Prior Reference Price
    "Q": N,  # Market Center Official Open
    "R": N,  # Seller
    "T": N,  # Extended Hours Trade
    "U": N,  # Extended Hours Sold (Out Of Sequence)
    "V": N,  # Contingent Trade
    "X": Y,  # Cross / Periodic Auction Trade
    "Z": F,  # Sold (Out Of Sequence)
    "4": F,  # Derivatively Priced
    "5": Y,  # Market Center Reopening Trade
    "6": Y,  # Market Center Closing Trade
    "7": N,  # Qualified Contingent Trade
    "8": N,  # Reserved
    "9": Y,  # Corrected Consolidated Close Price as per Listing Market
}

# UTP (tape C). The regular-sale code is "@".
UTP_LAST: Mapping[str, Effect] = {
    "@": Y,  # Regular Sale
    "A": Y,  # Acquisition
    "B": Y,  # Bunched Trade
    "C": N,  # Cash Sale
    "D": Y,  # Distribution
    "E": TBD,  # Placeholder
    "F": Y,  # Intermarket Sweep
    "G": F,  # Bunched Sold Trade
    "H": N,  # Price Variation Trade
    "I": N,  # Odd Lot Trade
    "K": Y,  # Rule 155 Trade (AMEX)
    "L": Y,  # Sold Last (footnote 2: only before End of Last Sale Eligibility, 16:00:10 ET)
    "M": N,  # Market Center Official Close
    "N": N,  # Reserved
    "O": Y,  # Opening Prints
    "P": F,  # Prior Reference Price
    "Q": N,  # Market Center Official Open
    "R": N,  # Seller
    "S": Y,  # Split Trade
    "T": N,  # Form T
    "U": N,  # Extended trading hours (Sold Out of Sequence)
    "V": N,  # Contingent Trade
    "W": N,  # Average Price Trade
    "X": Y,  # Cross / Periodic Auction Trade
    "Y": Y,  # Yellow Flag Regular Trade
    "Z": F,  # Sold (out of sequence)
    "1": Y,  # Stopped Stock (Regular Trade)
    "4": F,  # Derivatively priced
    "5": Y,  # Re-Opening Prints
    "6": Y,  # Closing Prints
    "7": N,  # Qualified Contingent Trade
    "8": TBD,  # Placeholder for 611 Exempt
    "9": Y,  # Corrected Consolidated Close (per listing market)
}


def spec_table(tape: Tape) -> Mapping[str, Effect]:
    return UTP_LAST if tape is Tape.C else CTS_LAST


@dataclass(frozen=True)
class ProviderEntry:
    """One provider (Alpaca) code for one tape and the specification code it stands for."""

    tape: Tape
    provider_code: str
    spec_code: str
    expected_provider_name: str
    spec_name: str
    validated: bool


@dataclass(frozen=True)
class Translation:
    spec_code: str | None
    reason: RefReason | None  # why there is no spec code


class ProviderCodeMap:
    """Provider code -> specification code, honouring only validated entries."""

    def __init__(self, entries: Iterable[ProviderEntry]) -> None:
        self._entries = {(e.tape, e.provider_code): e for e in entries}

    @property
    def entries(self) -> tuple[ProviderEntry, ...]:
        return tuple(self._entries.values())

    def any_validated(self) -> bool:
        return any(e.validated for e in self._entries.values())

    def translate(self, tape: Tape, provider_code: str) -> Translation:
        entry = self._entries.get((tape, provider_code))
        if entry is None:
            return Translation(None, RefReason.UNKNOWN_CONDITION)
        if not entry.validated:
            return Translation(None, RefReason.PROVIDER_MAPPING_UNVALIDATED)
        return Translation(entry.spec_code, None)


MAP_PATH = Path(__file__).resolve().parents[1] / "config" / "trade_condition_map.yaml"


def parse_provider_map(text: str) -> ProviderCodeMap:
    doc = yaml.safe_load(text)
    entries: list[ProviderEntry] = []
    for tape_name, rows in doc["tapes"].items():
        tape = Tape(tape_name)
        for row in rows:
            entries.append(
                ProviderEntry(
                    tape=tape,
                    provider_code=str(row["provider_code"]),
                    spec_code=str(row["spec_code"]),
                    expected_provider_name=str(row["expected_provider_name"]),
                    spec_name=str(row["spec_name"]),
                    validated=bool(row["validated"]),
                )
            )
    return ProviderCodeMap(entries)


def load_provider_map(path: Path = MAP_PATH) -> ProviderCodeMap:
    return parse_provider_map(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Eligibility:
    """The verdict on one trade before the same-day sequence is considered."""

    status: ConditionEligibility
    reason: RefReason | None = None  # set when UNKNOWN
    note3: bool = (
        False  # a conditional verdict that needs facts we do not hold when it is not first
    )


def evaluate_conditions(
    tape: Tape, provider_codes: Iterable[str], pmap: ProviderCodeMap
) -> Eligibility:
    """Combine the conditions on one trade (all must qualify; any NO disqualifies)."""
    table = spec_table(tape)
    unknown: RefReason | None = None
    conditional = False
    note3 = False
    codes = list(provider_codes)
    if not codes:  # a trade with no conditions says nothing: never assume it is a regular sale
        return Eligibility(ConditionEligibility.UNKNOWN, RefReason.UNKNOWN_CONDITION)
    for code in codes:
        tr = pmap.translate(tape, code)
        if tr.spec_code is None:
            # Not knowing what a code means cannot make a trade qualify; it can only be decisive
            # after the codes we can read have not already disqualified it.
            unknown = _worse(unknown, tr.reason)
            continue
        effect = table.get(tr.spec_code)
        if effect is None or effect is Effect.TBD:
            unknown = _worse(unknown, RefReason.UNKNOWN_CONDITION)
        elif effect is Effect.NO:
            return Eligibility(ConditionEligibility.INELIGIBLE)
        elif effect is Effect.FIRST_ONLY:
            conditional = True
        elif effect is Effect.NOTE3:
            conditional = True
            note3 = True
    if unknown is not None:
        return Eligibility(ConditionEligibility.UNKNOWN, unknown)
    if conditional:
        return Eligibility(ConditionEligibility.CONDITIONAL, note3=note3)
    return Eligibility(ConditionEligibility.ELIGIBLE)


def _worse(current: RefReason | None, new: RefReason | None) -> RefReason | None:
    """An unvalidated mapping is reported in preference to a merely unknown code."""
    if current is RefReason.PROVIDER_MAPPING_UNVALIDATED or new is None:
        return current
    if new is RefReason.PROVIDER_MAPPING_UNVALIDATED:
        return new
    return current or new
