"""EDGAR submissions index: company metadata, filings and their acceptance times."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from ingest.edgar_client import EdgarClient
from ingest.timeutil import parse_edgar_acceptance

DATA_SEC = "https://data.sec.gov"
WWW_SEC = "https://www.sec.gov"
TICKERS_URL = f"{WWW_SEC}/files/company_tickers_exchange.json"


@dataclass(frozen=True)
class Filing:
    accession: str
    form: str
    filing_date: date
    accepted_at: datetime
    primary_document: str


@dataclass(frozen=True)
class Company:
    cik: int
    name: str
    sic: int | None
    sic_description: str | None
    filings: tuple[Filing, ...]


def submissions_url(cik: int) -> str:
    return f"{DATA_SEC}/submissions/CIK{cik:010d}.json"


def _columns(block: dict[str, Any]) -> Iterator[Filing]:
    for accn, form, fdate, accepted, doc in zip(
        block["accessionNumber"],
        block["form"],
        block["filingDate"],
        block["acceptanceDateTime"],
        block["primaryDocument"],
        strict=True,
    ):
        yield Filing(
            accession=accn,
            form=form,
            filing_date=date.fromisoformat(fdate),
            accepted_at=parse_edgar_acceptance(accepted),
            primary_document=doc,
        )


def parse_submissions(payload: dict[str, Any], older_pages: list[dict[str, Any]]) -> Company:
    filings = list(_columns(payload["filings"]["recent"]))
    for page in older_pages:
        filings.extend(_columns(page))
    sic = payload.get("sic")
    return Company(
        cik=int(payload["cik"]),
        name=payload["name"],
        sic=int(sic) if sic else None,
        sic_description=payload.get("sicDescription") or None,
        filings=tuple(filings),
    )


def fetch_company(client: EdgarClient, cik: int, *, since: date | None = None) -> Company:
    """Recent filings plus any older pages that reach back to ``since``."""
    payload = client.get_json(submissions_url(cik))
    older = [
        client.get_json(f"{DATA_SEC}/submissions/{f['name']}")
        for f in payload["filings"].get("files", [])
        if since is None or date.fromisoformat(f["filingTo"]) >= since
    ]
    return parse_submissions(payload, older)


@dataclass(frozen=True)
class ListedTicker:
    cik: int
    name: str
    ticker: str
    exchange: str | None


def fetch_tickers(client: EdgarClient) -> list[ListedTicker]:
    """SEC's current ticker list. It has no delisted names, so seeding from it is not
    survivorship-free for dates before the first backfill (see docs/plans/P1.md)."""
    payload = client.get_json(TICKERS_URL)
    idx = {name: i for i, name in enumerate(payload["fields"])}
    return [
        ListedTicker(
            cik=int(row[idx["cik"]]),
            name=row[idx["name"]],
            ticker=row[idx["ticker"]].upper().replace("-", "."),
            exchange=row[idx["exchange"]],
        )
        for row in payload["data"]
    ]
