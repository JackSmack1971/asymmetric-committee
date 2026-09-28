"""P6.4 fixtures: one record per Alpaca corporate-action type, shaped like the API reference
examples (``GET /v1/corporate-actions``, re-checked 2026-09-27). Synthetic until a real
recording exists; symbols are remapped onto the test securities."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

import httpx

from contracts.corporate_actions import ALL_ACTION_TYPES, CorporateActionCoverage, actions_sha256
from contracts.enums import CorporateActionType, KnowledgeBasis

EXAMPLES: dict[CorporateActionType, dict[str, Any]] = {
    CorporateActionType.FORWARD_SPLIT: {
        "id": "fs-1",
        "symbol": "AAA",
        "cusip": "816851109",
        "new_rate": 2,
        "old_rate": 1,
        "ex_date": "2024-03-12",
        "record_date": "2024-03-04",
        "payable_date": "2024-03-11",
        "process_date": "2024-03-12",
        "due_bill_redemption_date": "2024-03-13",
    },
    CorporateActionType.REVERSE_SPLIT: {
        "id": "rs-1",
        "symbol": "AAA",
        "old_cusip": "60879E101",
        "new_cusip": "60879E200",
        "new_rate": 1,
        "old_rate": 50,
        "ex_date": "2024-03-14",
        "record_date": "2024-03-14",
        "process_date": "2024-03-14",
    },
    CorporateActionType.UNIT_SPLIT: {
        "id": "us-1",
        "old_symbol": "AAA",
        "old_cusip": "G8990L119",
        "old_rate": 1,
        "new_symbol": "AAB",
        "new_cusip": "G5391L102",
        "new_rate": 1,
        "alternate_symbol": "AAAW",
        "alternate_cusip": "G5391L110",
        "alternate_rate": 0.3333,
        "effective_date": "2024-03-01",
        "process_date": "2024-03-01",
    },
    CorporateActionType.CASH_DIVIDEND: {
        "id": "cd-1",
        "symbol": "AAA",
        "cusip": "319829107",
        "rate": 0.125,
        "special": False,
        "foreign": False,
        "ex_date": "2024-03-04",
        "record_date": "2024-03-05",
        "payable_date": "2024-03-19",
        "process_date": "2024-03-19",
    },
    CorporateActionType.STOCK_DIVIDEND: {
        "id": "sd-1",
        "symbol": "AAA",
        "cusip": "605015106",
        "rate": 0.05,
        "ex_date": "2024-03-19",
        "record_date": "2024-03-22",
        "payable_date": "2024-03-05",
        "process_date": "2024-03-19",
    },
    CorporateActionType.SPIN_OFF: {
        "id": "so-1",
        "source_symbol": "AAA",
        "source_cusip": "48208F105",
        "source_rate": 19.35,
        "new_symbol": "SPUN",
        "new_cusip": "85237B101",
        "new_rate": 1,
        "ex_date": "2024-03-15",
        "record_date": "2024-03-15",
        "process_date": "2024-03-15",
    },
    CorporateActionType.CASH_MERGER: {
        "id": "cm-1",
        "acquiree_symbol": "AAA",
        "acquiree_cusip": "Y2687W108",
        "rate": 5.37,
        "effective_date": "2024-03-18",
        "payable_date": "2024-03-18",
        "process_date": "2024-03-18",
    },
    CorporateActionType.STOCK_MERGER: {
        "id": "sm-1",
        "acquiree_symbol": "AAA",
        "acquiree_cusip": "53223X107",
        "acquiree_rate": 1,
        "acquirer_symbol": "BBB",
        "acquirer_cusip": "30225T102",
        "acquirer_rate": 0.895,
        "effective_date": "2024-03-20",
        "payable_date": "2024-03-20",
        "process_date": "2024-03-20",
    },
    CorporateActionType.STOCK_AND_CASH_MERGER: {
        "id": "scm-1",
        "acquiree_symbol": "AAA",
        "acquiree_cusip": "561409103",
        "acquiree_rate": 1,
        "acquirer_symbol": "BBB",
        "acquirer_cusip": "31931U102",
        "acquirer_rate": 0.7733,
        "cash_rate": 7.8,
        "effective_date": "2024-03-18",
        "payable_date": "2024-03-18",
        "process_date": "2024-03-18",
    },
    CorporateActionType.REDEMPTION: {
        "id": "rd-1",
        "symbol": "AAA",
        "cusip": "687305102",
        "rate": 0.141134,
        "payable_date": "2024-03-13",
        "process_date": "2024-03-13",
    },
    CorporateActionType.NAME_CHANGE: {
        "id": "nc-1",
        "old_symbol": "AAA",
        "old_cusip": "G11537100",
        "new_symbol": "ZZZ",
        "new_cusip": "Y9390M103",
        "process_date": "2024-03-15",
    },
    CorporateActionType.WORTHLESS_REMOVAL: {
        "id": "wr-1",
        "symbol": "AAA",
        "cusip": "078771300",
        "process_date": "2024-03-19",
    },
    CorporateActionType.RIGHTS_DISTRIBUTION: {
        "id": "rt-1",
        "source_symbol": "AAA",
        "source_cusip": "454089103",
        "new_symbol": "AAA.RTWI",
        "new_cusip": "454089111",
        "rate": 1,
        "ex_date": "2024-03-17",
        "record_date": "2024-03-18",
        "payable_date": "2024-03-19",
        "process_date": "2024-03-19",
        "expiration_date": "2024-04-14",
    },
    CorporateActionType.PARTIAL_CALL: {
        "id": "pc-1",
        "symbol": "AAA",
        "cusip": "000000000",
        "price": 25.0,
        "process_date": "2024-03-19",
    },
    CorporateActionType.REORGANIZATION: {
        "id": "ro-1",
        "symbol": "AAA",
        "cusip": "111111111",
        "cash_rate": 1.5,
        "stock_movements": [
            {"symbol": "NEW1", "cusip": "222222222", "new_rate": 1, "source_rate": 2}
        ],
        "effective_date": "2024-03-19",
        "process_date": "2024-03-19",
    },
    CorporateActionType.CAPITAL_GAINS_DISTRIBUTION: {
        "id": "cgd-1",
        "symbol": "AAA",
        "rate": 0.25,
        "ex_date": "2024-03-19",
        "payable_date": "2024-03-28",
        "process_date": "2024-03-19",
    },
}


def record(kind: CorporateActionType, **changes: Any) -> dict[str, Any]:
    out = copy.deepcopy(EXAMPLES[kind])
    out.update(changes)
    return out


def page(
    records: Mapping[CorporateActionType, Sequence[dict[str, Any]]], token: str | None = None
) -> dict[str, Any]:
    return {
        "corporate_actions": {k.response_key: list(v) for k, v in records.items()},
        "next_page_token": token,
    }


Handler = Callable[[httpx.Request], httpx.Response]


def pages_transport(
    pages: Sequence[dict[str, Any] | int], calls: list[httpx.Request] | None = None
) -> httpx.MockTransport:
    """Serves ``pages`` in order; an int is an HTTP status to return instead of a page."""
    queue = list(pages)

    def handle(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        item = queue.pop(0)
        if isinstance(item, int):
            return httpx.Response(item, json={"message": "error"})
        return httpx.Response(200, content=json.dumps(item).encode())

    return httpx.MockTransport(handle)


def coverage(
    security_id: int,
    *,
    start: date,
    end: date,
    established_at: datetime,
    symbols: tuple[str, ...] = ("AAA",),
    pairs: Sequence[tuple[str, str]] = (),
    basis: KnowledgeBasis = KnowledgeBasis.PROSPECTIVE,
) -> CorporateActionCoverage:
    digest = actions_sha256(pairs)
    return CorporateActionCoverage(
        security_id=security_id,
        range_start=start,
        range_end=end,
        established_at=established_at,
        symbols=symbols,
        action_types=ALL_ACTION_TYPES,
        page_count=1,
        action_count=len(pairs),
        actions_sha256=digest,
        knowledge_basis=basis,
        source_version=f"coverage:{digest[:16]}:{established_at.isoformat()}",
    )


def utc(y: int, m: int, d: int, hh: int = 12) -> datetime:
    return datetime(y, m, d, hh, tzinfo=UTC)
