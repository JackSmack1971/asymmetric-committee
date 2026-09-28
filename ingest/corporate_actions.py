"""Corporate actions, listing evidence and the delisting ledger (P6.4, §4.6).

* Actions: Alpaca ``GET /v1/corporate-actions`` (market-data host). ``start``/``end`` are inclusive
  bounds on ``process_date``; every supported type is requested with ``data_quality=complete``.
  A query's coverage is written only after every page succeeded, together with the actions it
  vouches for. Any HTTP, auth, network, parsing or pagination failure writes nothing.
* Listing status: Alpaca ``GET /v2/assets/{symbol}`` on the **paper** trading host only.
* Form 25 / 25-NSE: EDGAR submissions, ``available_at`` = acceptance time.
* Delistings: `universe.delistings.derive` over point-in-time reads, appended when they change.

``available_at`` is always our observation time. Provider dates never become availability, and a
record Alpaca creates late is knowable only from when we first saw it.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Connection, Engine

from contracts.corporate_actions import (
    ALL_ACTION_TYPES,
    FORM25_AMENDMENTS,
    FORM25_FORMS,
    FORM25_MIN_EFFECTIVE_DAYS,
    ISSUER_FORM25,
    AssetStatusObservation,
    CorporateAction,
    CorporateActionCoverage,
    DelistingFiling,
    SecuritySymbol,
    SymbolResolver,
    actions_sha256,
    payload_version,
)
from contracts.data import Ticker
from contracts.enums import (
    ACTION_INTERPRETATION,
    AssetStatus,
    CorporateActionType,
    Form25Provision,
    KnowledgeBasis,
    SymbolSource,
)
from evaluation.calendar_rules import CalendarCoverageError
from execution.alpaca import PAPER_HOST, paper_base_url
from ingest.edgar_client import EdgarClient
from ingest.edgar_submissions import fetch_company
from store import as_of, write
from store import as_of_sensitivity as sensitivity
from universe.delistings import DEFAULT_GAP_SESSIONS, DelistingEvidence, derive

DATA_URL = "https://data.alpaca.markets"
ACTIONS_PATH = "/v1/corporate-actions"
PAGE_LIMIT = 1000
_ET = ZoneInfo("America/New_York")
_SPLITS = frozenset(
    {
        CorporateActionType.FORWARD_SPLIT,
        CorporateActionType.REVERSE_SPLIT,
        CorporateActionType.UNIT_SPLIT,
    }
)
_TYPE_BY_KEY = {t.response_key: t for t in CorporateActionType}
# Which field names the security an action acts on (the one whose holders are affected).
_SUBJECT = {
    CorporateActionType.UNIT_SPLIT: "old_symbol",
    CorporateActionType.NAME_CHANGE: "old_symbol",
    CorporateActionType.SPIN_OFF: "source_symbol",
    CorporateActionType.RIGHTS_DISTRIBUTION: "source_symbol",
    CorporateActionType.CASH_MERGER: "acquiree_symbol",
    CorporateActionType.STOCK_MERGER: "acquiree_symbol",
    CorporateActionType.STOCK_AND_CASH_MERGER: "acquiree_symbol",
}
_TICKER: TypeAdapter[str] = TypeAdapter(Ticker)


class CorporateActionsError(RuntimeError):
    """A query could not be completed; nothing (no actions, no coverage) was written."""


def _date(raw: Mapping[str, Any], key: str) -> date | None:
    v = raw.get(key)
    return date.fromisoformat(str(v)) if v else None


def _num(raw: Mapping[str, Any], key: str) -> float | None:
    v = raw.get(key)
    return None if v is None or v == "" else float(v)


def _symbol(value: Any) -> str | None:
    """A provider symbol as our ticker form, or None when it is empty or not a ticker."""
    if not value:
        return None
    s = str(value).upper().replace("-", ".")
    try:
        _TICKER.validate_python(s)
    except ValidationError:
        return None
    return s


def subject_symbol(action_type: CorporateActionType, raw: Mapping[str, Any]) -> str | None:
    return _symbol(raw.get(_SUBJECT.get(action_type, "symbol")))


@dataclass(frozen=True)
class ProviderAction:
    action_type: CorporateActionType
    raw: dict[str, Any]

    @property
    def provider_id(self) -> str:
        return str(self.raw["id"])


def parse_page(payload: Any) -> tuple[list[ProviderAction], str | None]:
    """One response page. An unknown group, a missing key or a malformed record is a failure:
    the coverage of a query must describe exactly the provider's schema."""
    if not isinstance(payload, dict) or set(payload) != {"corporate_actions", "next_page_token"}:
        raise CorporateActionsError("unexpected corporate-actions response shape")
    groups = payload["corporate_actions"]
    if not isinstance(groups, dict):
        raise CorporateActionsError("corporate_actions is not an object")
    out: list[ProviderAction] = []
    for key, records in groups.items():
        kind = _TYPE_BY_KEY.get(key)
        if kind is None:
            raise CorporateActionsError(f"unsupported corporate-action group {key!r}")
        if not isinstance(records, list):
            raise CorporateActionsError(f"{key} is not a list")
        for rec in records:
            if not isinstance(rec, dict) or not rec.get("id") or not rec.get("process_date"):
                raise CorporateActionsError(f"{key} record without id or process_date")
            out.append(ProviderAction(kind, rec))
    token = payload["next_page_token"]
    if token is not None and not isinstance(token, str):
        raise CorporateActionsError("next_page_token is not a string")
    return out, token or None


def to_action(
    p: ProviderAction,
    *,
    security_id: int,
    observed_at: datetime,
    basis: KnowledgeBasis,
    resolve: Callable[[str, date], int | None] = lambda symbol, day: None,
) -> CorporateAction:
    """Normalize one provider record. Per-share cash/stock for mergers is per acquiree share.

    ``resolve`` maps the acquirer's symbol to a security on the effective date (dated identity);
    it is a convenience only: delisting derivation resolves the acquirer again from symbol history.
    """
    r, t = p.raw, p.action_type
    subject = subject_symbol(t, r)
    if subject is None:
        raise CorporateActionsError(f"{p.provider_id}: no subject symbol")
    cash = stock = old = new = None
    acquirer = new_symbol = None
    if (
        t in (CorporateActionType.FORWARD_SPLIT, CorporateActionType.REVERSE_SPLIT)
        or t is CorporateActionType.UNIT_SPLIT
    ):
        old, new = _num(r, "old_rate"), _num(r, "new_rate")
        new_symbol = _symbol(r.get("new_symbol"))
    elif t is CorporateActionType.CASH_DIVIDEND:
        cash = _num(r, "rate")
    elif t is CorporateActionType.STOCK_DIVIDEND:
        stock = _num(r, "rate")
    elif t in (CorporateActionType.CASH_MERGER, CorporateActionType.REDEMPTION):
        cash = _num(r, "rate")
        acquirer = _symbol(r.get("acquirer_symbol"))
    elif t in (CorporateActionType.STOCK_MERGER, CorporateActionType.STOCK_AND_CASH_MERGER):
        per = _num(r, "acquiree_rate")
        acq = _num(r, "acquirer_rate")
        if per and per > 0 and acq is not None:
            stock = acq / per
        if t is CorporateActionType.STOCK_AND_CASH_MERGER:
            c = _num(r, "cash_rate")
            cash = None if c is None or not per or per <= 0 else c / per
        acquirer = _symbol(r.get("acquirer_symbol"))
    elif t is CorporateActionType.NAME_CHANGE:
        new_symbol = _symbol(r.get("new_symbol"))
    elif t is CorporateActionType.SPIN_OFF:
        old, new = _num(r, "source_rate"), _num(r, "new_rate")
        new_symbol = _symbol(r.get("new_symbol"))
    elif t is CorporateActionType.RIGHTS_DISTRIBUTION:
        stock = _num(r, "rate")
        new_symbol = _symbol(r.get("new_symbol"))
    elif t is CorporateActionType.PARTIAL_CALL:
        cash = _num(r, "price")
    elif t is CorporateActionType.REORGANIZATION:
        cash = _num(r, "cash_rate")
    process = _date(r, "process_date")
    assert process is not None
    return CorporateAction(
        provider_action_id=p.provider_id,
        available_at=observed_at,
        source_version=payload_version(r),
        security_id=security_id,
        subject_symbol=subject,
        action_type=t,
        interpretation=ACTION_INTERPRETATION[t],
        knowledge_basis=basis,
        process_date=process,
        ex_date=_date(r, "ex_date"),
        record_date=_date(r, "record_date"),
        payable_date=_date(r, "payable_date"),
        effective_date=_date(r, "effective_date"),
        old_rate=old,
        new_rate=new,
        cash_rate=cash,
        stock_rate=stock,
        acquirer_symbol=acquirer,
        acquirer_security_id=(
            resolve(acquirer, _date(r, "effective_date") or process) if acquirer else None
        ),
        new_symbol=new_symbol,
        currency=(r.get("currency") or None),
        raw_payload=dict(r),
    )


@dataclass(frozen=True)
class QueryResult:
    actions: list[ProviderAction]
    page_count: int
    completed_at: datetime


class AlpacaCorporateActions:
    """Paginated action queries. Retries 429/5xx/network a bounded number of times, then fails;
    a failure never yields a partial result."""

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        *,
        key_id: str | None = None,
        secret: str | None = None,
        max_retries: int = 3,
        backoff_s: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._http = httpx.Client(
            base_url=DATA_URL,
            transport=transport,
            timeout=30.0,
            follow_redirects=False,
            headers={
                "APCA-API-KEY-ID": key_id or os.environ.get("ALPACA_API_KEY_ID", ""),
                "APCA-API-SECRET-KEY": secret or os.environ.get("ALPACA_API_SECRET", ""),
            },
        )
        self.max_retries, self.backoff_s = max_retries, backoff_s
        self._sleep, self._clock = sleep, clock

    def _get(self, params: dict[str, str]) -> Any:
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._http.get(ACTIONS_PATH, params=params)
            except httpx.TransportError as e:
                if attempt == self.max_retries:
                    raise CorporateActionsError(f"network: {e!r}") from e
                self._sleep(self.backoff_s * 2**attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self.max_retries:
                    raise CorporateActionsError(f"HTTP {resp.status_code} after retries")
                wait = resp.headers.get("Retry-After")
                self._sleep(float(wait) if wait else self.backoff_s * 2**attempt)
                continue
            if resp.status_code != 200:
                raise CorporateActionsError(f"HTTP {resp.status_code}")  # auth, 4xx: no retry
            try:
                return resp.json()
            except ValueError as e:
                raise CorporateActionsError("response is not JSON") from e
        raise CorporateActionsError("unreachable")  # pragma: no cover

    def query(self, symbols: Sequence[str], start: date, end: date) -> QueryResult:
        base = {
            "symbols": ",".join(symbols),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "limit": str(PAGE_LIMIT),
            "data_quality": "complete",
            "region": "us",
            "sort": "asc",
        }
        actions: list[ProviderAction] = []
        seen: set[str] = set()
        token: str | None = None
        pages = 0
        while True:
            params = dict(base, page_token=token) if token else base
            found, token = parse_page(self._get(params))
            actions.extend(found)
            pages += 1
            if token is None:
                return QueryResult(actions, pages, self._clock())
            if token in seen:
                raise CorporateActionsError("pagination repeated a page token")
            seen.add(token)

    def close(self) -> None:
        self._http.close()


def _paper_only(request: httpx.Request) -> None:
    if request.url.host != PAPER_HOST or request.url.scheme != "https":
        raise CorporateActionsError(f"refusing asset request to {request.url.host!r}")


class AlpacaAssets:
    """``GET /v2/assets/{symbol}`` on the paper host (no live endpoint exists in this code)."""

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        *,
        key_id: str | None = None,
        secret: str | None = None,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        source = os.environ if env is None else env
        self._http = httpx.Client(
            base_url=paper_base_url(env),
            transport=transport,
            timeout=30.0,
            follow_redirects=False,
            event_hooks={"request": [_paper_only]},
            headers={
                "APCA-API-KEY-ID": key_id or source.get("ALPACA_API_KEY_ID", ""),
                "APCA-API-SECRET-KEY": secret or source.get("ALPACA_API_SECRET", ""),
            },
        )
        self._clock = clock

    def observe(self, security_id: int, symbol: str) -> AssetStatusObservation:
        resp = self._http.get(f"/v2/assets/{symbol}")
        observed = self._clock()
        if resp.status_code == 404:
            return AssetStatusObservation(
                security_id=security_id,
                symbol=symbol,
                observed_at=observed,
                status=AssetStatus.NOT_FOUND,
                tradable=None,
                source_version="http:404",
            )
        if resp.status_code != 200:
            raise CorporateActionsError(f"asset {symbol}: HTTP {resp.status_code}")
        body = resp.json()
        status = AssetStatus(str(body["status"]))
        if status is AssetStatus.NOT_FOUND:
            raise CorporateActionsError("not_found is not a provider status")
        return AssetStatusObservation(
            security_id=security_id,
            symbol=symbol,
            observed_at=observed,
            status=status,
            tradable=bool(body["tradable"]) if "tradable" in body else None,
            source_version=payload_version(body),
        )

    def close(self) -> None:
        self._http.close()


def form25_filings(
    client: EdgarClient, cik: int, *, since: date | None = None
) -> list[DelistingFiling]:
    company = fetch_company(client, cik, since=since)
    return [
        DelistingFiling(
            cik=cik,
            accession=f.accession,
            form=f.form,
            filing_date=f.filing_date,
            earliest_effective_date=f.filing_date + timedelta(days=FORM25_MIN_EFFECTIVE_DAYS),
            # An issuer files under (c). An exchange's paragraph and specified date live in the
            # filing document, which is not parsed yet: both stay None and derivation fails closed.
            rule_provision=Form25Provision.C if f.form in ISSUER_FORM25 else None,
            stated_effective_date=None,
            available_at=f.accepted_at,
        )
        for f in company.filings
        if f.form in FORM25_FORMS | FORM25_AMENDMENTS
    ]


@dataclass(frozen=True)
class SecurityScope:
    """One security and every symbol it is known to have traded under."""

    security_id: int
    symbols: tuple[str, ...]


@dataclass
class JobResult:
    written: int = 0
    errors: list[str] = field(default_factory=list)


def _chunks[T](items: Sequence[T], n: int) -> Iterable[Sequence[T]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


@dataclass
class CorporateActionsIngestor:
    engine: Engine
    actions: AlpacaCorporateActions
    assets: AlpacaAssets | None = None
    edgar: EdgarClient | None = None
    lookback_days: int = 30
    batch_size: int = 50
    gap_sessions: int = DEFAULT_GAP_SESSIONS

    # --- scope ---------------------------------------------------------------------------------

    def scopes(self, now: datetime) -> list[SecurityScope]:
        with self.engine.connect() as conn:
            out = []
            for sec in as_of.securities(conn):
                hist = as_of.security_symbols(conn, sec.security_id, now)
                symbols = sorted({h.symbol for h in hist} | {sec.ticker})
                out.append(SecurityScope(sec.security_id, tuple(symbols)))
            return out

    # --- actions + coverage --------------------------------------------------------------------

    def sync(
        self,
        scopes: Sequence[SecurityScope],
        start: date,
        end: date,
        *,
        basis: KnowledgeBasis,
    ) -> JobResult:
        """Query each batch; one transaction per batch, so one failure never hides the others."""
        result = JobResult()
        for batch in _chunks(list(scopes), self.batch_size):
            try:
                written, errors = self._sync_batch(batch, start, end, basis)
                result.written += written
                result.errors.extend(errors)
            except Exception as e:
                result.errors.append(f"{[s.security_id for s in batch]}: {e!r}")
        return result

    def _sync_batch(
        self, batch: Sequence[SecurityScope], start: date, end: date, basis: KnowledgeBasis
    ) -> tuple[int, list[str]]:
        """One query, then (1) actions + coverage in one transaction, (2) name-change continuity
        separately, so an identity conflict can never roll back the evidence.

        Attribution is dated: an action belongs to the one security whose symbol history holds the
        action's subject symbol on its ``process_date`` (the day before, for the name change
        itself). An action held by a security outside the batch is not ours. An unknown or
        ambiguous holder fails closed: no coverage is written for the batch securities that ever
        traded under that symbol, since their absence of actions would not be proven.
        """
        ids = {s.security_id for s in batch}
        res = self.actions.query(sorted({sym for s in batch for sym in s.symbols}), start, end)
        established = res.completed_at
        with self.engine.connect() as conn:
            resolver = SymbolResolver(as_of.symbol_history(conn, established))
        observed: list[CorporateAction] = []
        tainted: dict[int, str] = {}
        for p in res.actions:
            subject = subject_symbol(p.action_type, p.raw)
            if subject is None:
                continue
            day = date.fromisoformat(str(p.raw["process_date"]))
            if p.action_type is CorporateActionType.NAME_CHANGE:
                day -= timedelta(days=1)  # the old symbol's last day under it
            holders = resolver.holders(subject, day)
            if len(holders) == 1:
                (sid,) = holders
                if sid in ids:
                    observed.append(
                        to_action(
                            p,
                            security_id=sid,
                            observed_at=established,
                            basis=basis,
                            resolve=resolver.resolve,
                        )
                    )
                continue
            why = "ambiguous" if holders else "unattributable"
            for s in batch:
                if subject in s.symbols:
                    tainted[s.security_id] = f"{why} {subject} on {day} ({p.provider_id})"
        errors = [f"{sid}: coverage withheld, {why}" for sid, why in sorted(tainted.items())]
        kept = [s for s in batch if s.security_id not in tainted]
        observed = [a for a in observed if a.security_id not in tainted]
        written = 0
        if kept:
            coverages = [
                self._coverage(s, observed, start, end, established, res.page_count, basis)
                for s in kept
            ]
            with self.engine.begin() as conn:
                written = write.record_action_query(conn, coverages=coverages, observed=observed)
        if basis is KnowledgeBasis.PROSPECTIVE:  # backfilled renames are evidence only
            errors += self._apply_name_changes(observed, established)
        return written, errors

    def _apply_name_changes(
        self, observed: Sequence[CorporateAction], established: datetime
    ) -> list[str]:
        errors: list[str] = []
        for a in observed:
            if a.action_type is not CorporateActionType.NAME_CHANGE or not a.new_symbol:
                continue
            with self.engine.begin() as conn:
                conflict = write.apply_name_change(
                    conn,
                    SecuritySymbol(
                        security_id=a.security_id,
                        symbol=a.new_symbol,
                        valid_from=a.process_date,
                        source=SymbolSource.NAME_CHANGE,
                        source_ref=a.provider_action_id,
                        available_at=established,
                    ),
                )
            if conflict is not None:
                errors.append(
                    f"{a.security_id}: identity conflict, {a.new_symbol} is held by security "
                    f"{conflict.holder_security_id} (recorded; owner must reconcile)"
                )
        return errors

    @staticmethod
    def _coverage(
        s: SecurityScope,
        observed: Sequence[CorporateAction],
        start: date,
        end: date,
        established: datetime,
        pages: int,
        basis: KnowledgeBasis,
    ) -> CorporateActionCoverage:
        pairs = [
            (a.provider_action_id, a.source_version)
            for a in observed
            if a.security_id == s.security_id
        ]
        digest = actions_sha256(pairs)
        return CorporateActionCoverage(
            security_id=s.security_id,
            range_start=start,
            range_end=end,
            established_at=established,
            symbols=tuple(sorted(s.symbols)),
            action_types=ALL_ACTION_TYPES,
            page_count=pages,
            pagination_exhausted=True,
            action_count=len(pairs),
            actions_sha256=digest,
            knowledge_basis=basis,
            source_version=f"coverage:{digest[:16]}:{established.isoformat()}"[:128],
        )

    def run_corporate_actions(self, now: datetime) -> JobResult:
        """Scheduled poll: the trailing ``lookback_days`` of process dates through today (ET)."""
        today = now.astimezone(_ET).date()
        return self.sync(
            self.scopes(now),
            today - timedelta(days=self.lookback_days),
            today,
            basis=KnowledgeBasis.PROSPECTIVE,
        )

    def backfill(self, start: date, end: date, *, now: datetime) -> JobResult:
        """Historical collection. Rows are labelled ``backfill``: sensitivity/debug only."""
        return self.sync(self.scopes(now), start, end, basis=KnowledgeBasis.BACKFILL)

    # --- listing status + Form 25 --------------------------------------------------------------

    def run_listing_status(self, now: datetime) -> JobResult:
        """Poll every equity's asset status; fetch Form 25s for issuers currently inactive."""
        if self.assets is None:
            raise RuntimeError("no asset-status client configured")
        result = JobResult()
        with self.engine.connect() as conn:
            secs = as_of.securities(conn)
        for sec in secs:
            try:
                obs = self.assets.observe(sec.security_id, sec.ticker)
                with self.engine.begin() as conn:
                    result.written += write.insert_asset_status(conn, [obs])
                if obs.status is not AssetStatus.ACTIVE and self.edgar is not None:
                    filings = form25_filings(self.edgar, sec.cik)
                    with self.engine.begin() as conn:
                        result.written += write.insert_delisting_filings(conn, filings)
            except Exception as e:
                result.errors.append(f"{sec.security_id}: {e!r}")
        return result

    # --- derivation ----------------------------------------------------------------------------

    def evidence(self, conn: Connection, security_id: int, now: datetime) -> DelistingEvidence:
        """Prospective evidence only (the headline ledger)."""
        return self._evidence(
            conn,
            security_id,
            now,
            actions=as_of.corporate_actions(conn, [security_id], now),
            coverages=as_of.action_coverage(conn, security_id, now),
            basis=KnowledgeBasis.PROSPECTIVE,
        )

    def sensitivity_evidence(
        self, conn: Connection, security_id: int, now: datetime
    ) -> DelistingEvidence:
        """Prospective + backfill evidence, labelled ``backfill`` (sensitivity/debug only)."""
        return self._evidence(
            conn,
            security_id,
            now,
            actions=sensitivity.sensitivity_corporate_actions(conn, [security_id], now),
            coverages=sensitivity.sensitivity_action_coverage(conn, security_id, now),
            basis=KnowledgeBasis.BACKFILL,
        )

    @staticmethod
    def _evidence(
        conn: Connection,
        security_id: int,
        now: datetime,
        *,
        actions: Sequence[CorporateAction],
        coverages: Sequence[CorporateActionCoverage],
        basis: KnowledgeBasis,
    ) -> DelistingEvidence:
        cik, holders = as_of.security_cik(conn, security_id)
        filings = as_of.delisting_filings(conn, cik, now) if cik and holders == 1 else []
        try:
            cal = as_of.trading_calendar(conn, now)
        except CalendarCoverageError:
            cal = None
        return DelistingEvidence(
            security_id=security_id,
            now=now,
            actions=actions,
            coverages=coverages,
            polls=as_of.asset_status(conn, security_id, now),
            filings=filings,
            calendar=cal,
            bar_dates=as_of.bar_dates(conn, security_id, now),
            symbols=SymbolResolver(as_of.symbol_history(conn, now)),
            knowledge_basis=basis,
        )

    def run_delistings(self, now: datetime) -> JobResult:
        result = JobResult()
        with self.engine.connect() as conn:
            ids = [s.security_id for s in as_of.securities(conn)]
        for sid in ids:
            try:
                with self.engine.begin() as conn:
                    row = derive(self.evidence(conn, sid, now), gap_sessions=self.gap_sessions)
                    if row is not None:
                        result.written += write.record_delisting(conn, row)
            except Exception as e:
                result.errors.append(f"{sid}: {e!r}")
        return result
