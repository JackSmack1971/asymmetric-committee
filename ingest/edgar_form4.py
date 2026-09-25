"""SEC Form 4 → ``insider_txns`` (§4.1).

``available_at`` is the filing's acceptance time, never the transaction date: the two differ by
about two business days, and using the transaction date is look-ahead. Only the non-derivative
table is ingested (open-market buys/sells, grants, tax withholding, exercises into common).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from datetime import UTC, date, datetime

from contracts.data import InsiderTxn
from contracts.enums import InsiderRole, InsiderTxnCode
from ingest.edgar_client import EdgarClient
from ingest.edgar_submissions import WWW_SEC, Filing

FORMS = frozenset({"4", "4/A"})
_TRUE = frozenset({"1", "true"})
_10B5_1 = re.compile(r"10b5-1", re.IGNORECASE)


def form4_url(cik: int, filing: Filing) -> str:
    """Raw XML (``primaryDocument`` points at the XSL-rendered copy, e.g. ``xslF345X05/x.xml``)."""
    doc = filing.primary_document.rsplit("/", 1)[-1]
    return f"{WWW_SEC}/Archives/edgar/data/{cik}/{filing.accession.replace('-', '')}/{doc}"


def _text(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    node = el.find(path)
    if node is None or node.text is None:
        return None
    return node.text.strip() or None


def _float(el: ET.Element, path: str) -> float | None:
    raw = _text(el, path)
    return float(raw) if raw is not None else None


def _role(rel: ET.Element | None) -> InsiderRole:
    flags = {
        tag: (_text(rel, tag) or "").lower() in _TRUE
        for tag in ("isOfficer", "isDirector", "isTenPercentOwner")
    }
    if flags["isOfficer"]:
        return InsiderRole.OFFICER
    if flags["isDirector"]:
        return InsiderRole.DIRECTOR
    if flags["isTenPercentOwner"]:
        return InsiderRole.TEN_PERCENT_OWNER
    return InsiderRole.OTHER


def parse_form4(xml: str, filing: Filing, security_id: int) -> list[InsiderTxn]:
    root = ET.fromstring(xml)
    owners = root.findall("reportingOwner")
    filer = (
        "; ".join(n for o in owners if (n := _text(o, "reportingOwnerId/rptOwnerName")) is not None)
        or "unknown"
    )
    rel = owners[0].find("reportingOwnerRelationship") if owners else None
    footnotes = {fn.get("id"): "".join(fn.itertext()) for fn in root.findall("footnotes/footnote")}
    plan_box = (_text(root, "aff10b5One") or "").lower() in _TRUE

    out = []
    for seq, txn in enumerate(root.findall("nonDerivativeTable/nonDerivativeTransaction")):
        code = _text(txn, "transactionCoding/transactionCode")
        tdate = _text(txn, "transactionDate/value")
        shares = _float(txn, "transactionAmounts/transactionShares/value")
        if code is None or tdate is None or shares is None:
            continue
        refs = [f.get("id") for f in txn.iter("footnoteId")]
        in_plan = plan_box or any(_10B5_1.search(footnotes.get(r, "")) for r in refs)
        txn_date = date.fromisoformat(tdate[:10])
        out.append(
            InsiderTxn(
                security_id=security_id,
                accession=filing.accession,
                seq=seq,
                filer=filer[:256],
                role=_role(rel),
                officer_title=_text(rel, "officerTitle"),
                txn_date=txn_date,
                code=InsiderTxnCode(code),
                acquired=_text(txn, "transactionAmounts/transactionAcquiredDisposedCode/value")
                == "A",
                shares=shares,
                price=_float(txn, "transactionAmounts/transactionPricePerShare/value"),
                post_holdings=_float(
                    txn, "postTransactionAmounts/sharesOwnedFollowingTransaction/value"
                ),
                is_10b5_1=in_plan,
                event_time=datetime.combine(txn_date, datetime.min.time(), UTC),
                available_at=filing.accepted_at,
                source_version=filing.accession,
            )
        )
    return out


def form4_filings(filings: Iterable[Filing], start: datetime, end: datetime) -> list[Filing]:
    return [f for f in filings if f.form in FORMS and start <= f.accepted_at <= end]


def fetch_insider_txns(
    client: EdgarClient, cik: int, security_id: int, filings: Iterable[Filing]
) -> list[InsiderTxn]:
    return [
        txn
        for filing in filings
        for txn in parse_form4(client.get_text(form4_url(cik, filing)), filing, security_id)
    ]
