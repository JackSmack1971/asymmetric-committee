"""Corporate actions, action coverage, listing evidence and delistings (P6.4, §4.3, §4.6).

Nothing here is LLM-facing. Every row is insert-only. ``available_at`` is always the time *we*
first observed that version (the end of the fetch that returned it); provider dates (declared,
ex, record, payable, process, effective) are event dates and never availability. Alpaca gives no
guarantee on when an action is created, so a record can appear long after its event; it is then
knowable only from the moment we saw it.

No returns are computed here: split factors, cash amounts and terminal consideration are stored as
inputs for scoring (P6.5).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Self
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, model_validator

from contracts.data import Ticker, Version
from contracts.enums import (
    ACTION_INTERPRETATION,
    ActionCoverageFilter,
    ActionInterpretation,
    AssetStatus,
    CorporateActionType,
    DelistingReason,
    DelistingStatus,
    Form25Provision,
    KnowledgeBasis,
    SymbolSource,
    TerminalReturnSource,
)
from contracts.models import Contract, Finite, Label, NonNegative, SecurityId, Sha256Hex

ALPACA = "alpaca"
DEFAULT_TERMINAL_RETURN = -0.30  # Shumway (1997), flagged `default` (§4.6)
WORTHLESS_TERMINAL_RETURN = -1.0
WITHDRAWN = "withdrawn"
ALL_ACTION_TYPES: tuple[CorporateActionType, ...] = tuple(sorted(CorporateActionType))
# 17 CFR 240.12d2-2: an issuer's Form 25 strikes the class 10 days after filing ((d)(1)); an
# exchange's (EDGAR "25-NSE") becomes effective on the date the exchange specifies, not less than
# 10 days after filing ((a)); the Commission may postpone ((d)(3)); a successor admission can delay
# it ((d)(8)). Filing + 10 days is therefore only the earliest legally possible date.
FORM25_MIN_EFFECTIVE_DAYS = 10
FORM25_FORMS = frozenset({"25", "25-NSE"})
FORM25_AMENDMENTS = frozenset({"25/A", "25-NSE/A"})
ISSUER_FORM25 = frozenset({"25", "25/A"})  # EDGAR "25-NSE" is exchange-filed

_ET = ZoneInfo("America/New_York")
ProviderId = Annotated[str, Field(min_length=1, max_length=128)]
Accession = Annotated[str, Field(pattern=r"^\d{10}-\d{2}-\d{6}$")]


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def payload_version(raw: Mapping[str, Any]) -> str:
    """``source_version`` of one provider record: a digest of its canonical JSON."""
    return f"payload:{_digest(dict(raw))[:32]}"


def actions_sha256(pairs: Iterable[tuple[str, str]]) -> str:
    """Digest of the ``(provider_action_id, source_version)`` set a coverage row vouches for."""
    return hashlib.sha256("\n".join(f"{a}|{v}" for a, v in sorted(set(pairs))).encode()).hexdigest()


def ex_in_window(entry_day: date, ex_date: date, exit_day: date) -> bool:
    """Action eligibility for a holding window (§4.6): ``entry_day < ex_date <= exit_day``."""
    return entry_day < ex_date <= exit_day


class CorporateAction(Contract):
    """One observed version of one provider action, attributed to the security it acts on.

    Key ``(provider, provider_action_id, available_at)``. A revised payload is a new row with a new
    ``source_version``; an action missing from a later complete query of its scope is a new
    ``withdrawn`` row (never a deletion). The latest row knowable at ``as_of`` wins.
    """

    provider: Label = ALPACA
    provider_action_id: ProviderId
    available_at: AwareDatetime
    source_version: Version
    withdrawn: bool = False
    security_id: SecurityId
    subject_symbol: Ticker  # the symbol the action names for the affected holders
    action_type: CorporateActionType
    interpretation: ActionInterpretation
    knowledge_basis: KnowledgeBasis
    process_date: date
    ex_date: date | None = None
    record_date: date | None = None
    payable_date: date | None = None
    effective_date: date | None = None
    old_rate: Finite | None = None  # split: shares before
    new_rate: Finite | None = None  # split: shares after
    cash_rate: NonNegative | None = None  # dividend / cash merger / redemption, per share
    stock_rate: NonNegative | None = None  # stock dividend rate or acquirer shares per share
    acquirer_symbol: Ticker | None = None
    acquirer_security_id: SecurityId | None = None
    new_symbol: Ticker | None = None
    currency: Annotated[str, Field(max_length=8)] | None = None
    raw_payload: dict[str, Any]

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.interpretation is not ACTION_INTERPRETATION[self.action_type]:
            raise ValueError("interpretation must follow ACTION_INTERPRETATION")
        if self.withdrawn != self.source_version.startswith(WITHDRAWN):
            raise ValueError("withdrawn rows, and only they, carry a withdrawn source_version")
        if not self.withdrawn and self.source_version != payload_version(self.raw_payload):
            raise ValueError("source_version must be the digest of raw_payload")
        if self.interpretation is ActionInterpretation.SPLIT_FACTOR and not (
            self.old_rate and self.new_rate and self.old_rate > 0 and self.new_rate > 0
        ):
            raise ValueError("a split states positive old and new rates")
        if self.interpretation is ActionInterpretation.SPLIT_FACTOR and self.ex_date is None:
            raise ValueError("a split has an ex_date")
        if self.interpretation in (
            ActionInterpretation.CASH_DIVIDEND,
            ActionInterpretation.STOCK_DISTRIBUTION,
        ) and (self.ex_date is None):
            raise ValueError("a distribution has an ex_date")
        return self

    @property
    def split_factor(self) -> float | None:
        """Shares after / shares before, for a split; ``1 + rate`` for a stock distribution."""
        if self.interpretation is ActionInterpretation.SPLIT_FACTOR:
            assert self.old_rate and self.new_rate
            return self.new_rate / self.old_rate
        if self.interpretation is ActionInterpretation.STOCK_DISTRIBUTION:
            return None if self.stock_rate is None else 1.0 + self.stock_rate
        return None

    @property
    def event_date(self) -> date:
        """The date the action takes economic effect (ex, else effective, else process)."""
        return self.ex_date or self.effective_date or self.process_date


def coverage_through(established_at: datetime, range_end: date) -> date:
    """Last process date a coverage row can vouch for.

    A query cannot speak for actions Alpaca has not processed yet, so coverage never extends past
    the day before the fetch (Eastern), whatever ``end`` was requested.
    """
    return min(range_end, established_at.astimezone(_ET).date() - timedelta(days=1))


class CorporateActionCoverage(Contract):
    """One security's complete, successful action query at ``established_at``.

    Describes the provider filter exactly: every supported type, ``data_quality=complete``, the
    exact symbols queried, and an inclusive ``process_date`` range (Alpaca ``start``/``end``).
    Written only after every page returned 2xx; together with the actions it vouches for.
    Absence of action rows means "none" only inside a coverage range (§4.6).
    """

    provider: Label = ALPACA
    security_id: SecurityId
    date_filter: ActionCoverageFilter = ActionCoverageFilter.PROCESS_DATE
    range_start: date
    range_end: date
    established_at: AwareDatetime
    symbols: tuple[Ticker, ...] = Field(min_length=1)
    action_types: tuple[CorporateActionType, ...]
    data_quality: Label = "complete"
    region: Label = "us"
    page_count: int = Field(ge=1)
    pagination_exhausted: bool = False
    full_history: bool = False
    provider_lower_bound: date | None = None
    action_count: int = Field(ge=0)
    actions_sha256: Sha256Hex
    knowledge_basis: KnowledgeBasis
    source_version: Version

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.range_end < self.range_start:
            raise ValueError("coverage range ends before it starts")
        if tuple(sorted(set(self.action_types))) != ALL_ACTION_TYPES:
            raise ValueError("coverage requires every supported action type to be requested")
        if self.data_quality != "complete":
            raise ValueError("coverage requires data_quality=complete")
        if self.full_history and (
            not self.pagination_exhausted or self.provider_lower_bound is None
        ):
            raise ValueError(
                "full-history coverage requires exhausted pages and provider lower bound"
            )
        if list(self.symbols) != sorted(set(self.symbols)):
            raise ValueError("coverage symbols are sorted and unique")
        return self

    @property
    def covered_through(self) -> date:
        return coverage_through(self.established_at, self.range_end)


class SecuritySymbol(Contract):
    """``security_id`` trades as ``symbol`` from ``valid_from`` (append-only history).

    The symbol on date d is the row with the latest ``valid_from <= d`` knowable at ``as_of``. A
    name change adds a row to the *same* security, so identity never follows the ticker.
    """

    security_id: SecurityId
    symbol: Ticker
    valid_from: date
    source: SymbolSource
    source_ref: Label
    available_at: AwareDatetime


class AssetStatusObservation(Contract):
    """One persisted Alpaca ``GET /v2/assets/{symbol}`` poll (paper host)."""

    security_id: SecurityId
    symbol: Ticker
    observed_at: AwareDatetime
    status: AssetStatus
    tradable: bool | None
    source_version: Version


class DelistingFiling(Contract):
    """An EDGAR Form 25 / 25-NSE or an amendment for an issuer; ``available_at`` = acceptance.

    Timing per 17 CFR 240.12d2-2 and the Form 25 General Instructions:

    * issuer filing (``25``/``25/A``, paragraph (c)) and exchange filing under paragraph (b):
      removal is effective 10 days after filing;
    * exchange filing under paragraph (a)(1)-(4): effective on the date the exchange specifies,
      not less than 10 days after filing ((a)(3) may be delayed further by a successor, (d)(8));
    * an amendment: effective 10 days after the amendment is filed (the clock restarts);
    * the Commission may postpone by written notice ((d)(3)); EDGAR shows no such notice, so its
      absence is never proven here (residual risk, mitigated only by inactive-poll corroboration).

    ``earliest_effective_date`` (filing + 10 days) is only the legal floor of this filing.
    ``rule_provision`` is the paragraph relied on: ``c`` for an issuer filing; for an exchange
    ``25-NSE`` it is known only from the filing document (not parsed yet, so None).
    ``stated_effective_date`` is the exchange-specified date when parsed. Form 25 is filed per
    class but indexed per issuer: it is attributable only when the issuer has exactly one security.
    """

    cik: int = Field(ge=1)
    accession: Accession
    form: Label
    filing_date: date
    earliest_effective_date: date
    rule_provision: Form25Provision | None = None
    stated_effective_date: date | None = None
    available_at: AwareDatetime

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.form not in FORM25_FORMS | FORM25_AMENDMENTS:
            raise ValueError("only Form 25 / 25-NSE (and amendments) are delisting evidence")
        floor = self.filing_date + timedelta(days=FORM25_MIN_EFFECTIVE_DAYS)
        if self.earliest_effective_date != floor:
            raise ValueError("earliest_effective_date is filing + 10 days")
        if self.stated_effective_date is not None and self.stated_effective_date < floor:
            raise ValueError("a Form 25 cannot be effective sooner than 10 days after filing")
        issuer = self.form in ISSUER_FORM25
        if issuer and self.rule_provision is not Form25Provision.C:
            raise ValueError("an issuer Form 25 relies on paragraph (c)")
        if not issuer and self.rule_provision is Form25Provision.C:
            raise ValueError("paragraph (c) is the issuer's; an exchange files under (a) or (b)")
        return self

    @property
    def amendment(self) -> bool:
        return self.form in FORM25_AMENDMENTS


class IdentityConflict(Contract):
    """A name change to a ticker another security already holds. Neither security is changed."""

    security_id: SecurityId
    symbol: Ticker
    valid_from: date
    source_ref: Label
    holder_security_id: SecurityId
    detected_at: AwareDatetime


class Delisting(Contract):
    """One derivation of a security's listing state from stored evidence (§4.6 hierarchy).

    Key ``(security_id, available_at)``; a changed conclusion is a new row, the earlier one stays.
    ``terminal_return`` is set only when no price is needed (worthless -1.0, default -0.30); for
    ``consideration`` the inputs are stored and the return is computed at scoring.
    """

    security_id: SecurityId
    status: DelistingStatus
    reason: DelistingReason
    last_trade_date: date | None
    terminal_return: Finite | None
    terminal_return_source: TerminalReturnSource | None
    cash_per_share: NonNegative | None = None
    acquirer_security_id: SecurityId | None = None
    acquirer_symbol: Ticker | None = None
    acquirer_rate: NonNegative | None = None
    evidence: tuple[Label, ...] = Field(min_length=1)
    knowledge_basis: KnowledgeBasis
    derivation_version: Label
    available_at: AwareDatetime
    source_version: Version

    @model_validator(mode="after")
    def _check(self) -> Self:
        src = self.terminal_return_source
        if self.status is DelistingStatus.DELISTED:
            if src is None:
                raise ValueError("a delisting states its terminal return source")
            if src is TerminalReturnSource.DEFAULT and self.terminal_return != (
                DEFAULT_TERMINAL_RETURN
            ):
                raise ValueError("the default terminal return is -0.30")
            if src is TerminalReturnSource.WORTHLESS and self.terminal_return != (
                WORTHLESS_TERMINAL_RETURN
            ):
                raise ValueError("a worthless removal returns -1.0")
            if src is TerminalReturnSource.CONSIDERATION and (
                self.terminal_return is not None
                or (self.cash_per_share is None and self.acquirer_rate is None)
                or (self.acquirer_rate is not None and self.acquirer_symbol is None)
            ):
                raise ValueError("consideration stores its inputs (acquirer by symbol), no return")
        elif src is not None or self.terminal_return is not None:
            raise ValueError("only a delisting has a terminal return")
        if self.source_version != delisting_version(self):
            raise ValueError("source_version must be the digest of the conclusion")
        return self


_DELISTING_PAYLOAD = (
    "security_id",
    "status",
    "reason",
    "last_trade_date",
    "terminal_return",
    "terminal_return_source",
    "cash_per_share",
    "acquirer_security_id",
    "acquirer_symbol",
    "acquirer_rate",
    "evidence",
    "knowledge_basis",
    "derivation_version",
)


def delisting_version(d: Delisting | Mapping[str, Any]) -> str:
    """Digest of a conclusion (everything except when it was derived)."""
    data = d.model_dump(mode="json") if isinstance(d, Delisting) else dict(d)
    return f"delisting:{_digest({k: data.get(k) for k in _DELISTING_PAYLOAD})[:32]}"


class SymbolResolver:
    """Dated identity: which securities traded as ``symbol`` on ``day`` (from `SecuritySymbol`s).

    A security's row with ``valid_from`` v holds until its next row's ``valid_from``; its latest
    row is open-ended. Returning a set lets callers fail closed on an ambiguous symbol.
    """

    def __init__(self, history: Iterable[SecuritySymbol]) -> None:
        by_sid: dict[int, list[SecuritySymbol]] = {}
        for row in history:
            by_sid.setdefault(row.security_id, []).append(row)
        self._spans: dict[str, list[tuple[date, date | None, int]]] = {}
        for sid, rows in by_sid.items():
            rows.sort(key=lambda r: (r.valid_from, r.available_at))
            for i, r in enumerate(rows):
                end = rows[i + 1].valid_from if i + 1 < len(rows) else None
                if end is not None and end <= r.valid_from:
                    continue  # superseded on the same day
                self._spans.setdefault(r.symbol, []).append((r.valid_from, end, sid))

    def holders(self, symbol: str, day: date) -> set[int]:
        return {
            sid
            for start, end, sid in self._spans.get(symbol, ())
            if start <= day and (end is None or day < end)
        }

    def ever_held(self, symbol: str) -> set[int]:
        return {sid for _, _, sid in self._spans.get(symbol, ())}

    def resolve(self, symbol: str, day: date) -> int | None:
        found = self.holders(symbol, day)
        return next(iter(found)) if len(found) == 1 else None
