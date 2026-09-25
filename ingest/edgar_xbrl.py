"""SEC companyfacts XBRL → ``fundamentals_asfiled`` (§4.1, invariant 3).

One row per ``(concept, unit, period_start, period_end, accession)``, stored as filed and never
updated. A restatement arrives as a new accession and therefore a new row. ``available_at`` is the
filing's acceptance time from the submissions index; if an accession is missing there, the end of
its ``filed`` date (Eastern) is used, which is never earlier than the true acceptance.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from datetime import UTC, date, datetime
from typing import Any

from contracts.data import FundamentalFact
from ingest.edgar_client import EdgarClient
from ingest.edgar_submissions import DATA_SEC
from ingest.timeutil import end_of_day_et

TAXONOMIES = ("us-gaap", "dei")


def companyfacts_url(cik: int) -> str:
    return f"{DATA_SEC}/api/xbrl/companyfacts/CIK{cik:010d}.json"


def parse_companyfacts(
    payload: dict[str, Any],
    security_id: int,
    acceptance: Mapping[str, datetime],
    *,
    concepts: Collection[str] | None = None,
) -> list[FundamentalFact]:
    rows: dict[tuple[str, str, date | None, date, str], FundamentalFact] = {}
    for taxonomy in TAXONOMIES:
        for name, body in (payload.get("facts", {}).get(taxonomy) or {}).items():
            concept = f"{taxonomy}:{name}"
            if concepts is not None and concept not in concepts:
                continue
            for unit, entries in body.get("units", {}).items():
                for e in entries:
                    value = float(e["val"])
                    if not math.isfinite(value):
                        continue
                    accn = e["accn"]
                    start = date.fromisoformat(e["start"]) if e.get("start") else None
                    end = date.fromisoformat(e["end"])
                    available = acceptance.get(accn) or end_of_day_et(
                        date.fromisoformat(e["filed"])
                    )
                    key = (concept, unit, start, end, accn)
                    if key in rows:  # the same fact can repeat within one filing
                        continue
                    rows[key] = FundamentalFact(
                        security_id=security_id,
                        concept=concept,
                        unit=unit[:32],
                        period_start=start,
                        period_end=end,
                        fiscal_period=e.get("fp"),
                        form=e.get("form"),
                        value=value,
                        event_time=datetime.combine(end, datetime.min.time(), UTC),
                        available_at=available,
                        source_version=accn,
                    )
    return list(rows.values())


def fetch_fundamentals(
    client: EdgarClient,
    cik: int,
    security_id: int,
    acceptance: Mapping[str, datetime],
) -> list[FundamentalFact]:
    return parse_companyfacts(client.get_json(companyfacts_url(cik)), security_id, acceptance)
