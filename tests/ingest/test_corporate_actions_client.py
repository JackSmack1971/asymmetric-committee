"""P6.4 Alpaca corporate-actions parsing, pagination and asset-status polling (no network)."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from contracts.corporate_actions import ALL_ACTION_TYPES
from contracts.enums import (
    ACTION_INTERPRETATION,
    ActionInterpretation,
    AssetStatus,
    CorporateActionType,
    KnowledgeBasis,
)
from execution.alpaca import LiveTradingError
from ingest.corporate_actions import (
    AlpacaAssets,
    AlpacaCorporateActions,
    CorporateActionsError,
    ProviderAction,
    parse_page,
    subject_symbol,
    to_action,
)
from tests.corporate_actions_support import EXAMPLES, page, pages_transport, record, utc

START, END = date(2024, 3, 1), date(2024, 3, 31)


def client(
    pages: list[dict[str, object] | int], calls: list[httpx.Request] | None = None
) -> AlpacaCorporateActions:
    return AlpacaCorporateActions(
        pages_transport(pages, calls),
        key_id="k",
        secret="s",
        sleep=lambda _: None,
        clock=lambda: utc(2024, 4, 1),
    )


def test_supported_types_match_the_api_reference_exactly() -> None:
    assert {t.value for t in ALL_ACTION_TYPES} == {
        "reverse_split",
        "forward_split",
        "unit_split",
        "cash_dividend",
        "stock_dividend",
        "spin_off",
        "cash_merger",
        "stock_merger",
        "stock_and_cash_merger",
        "redemption",
        "name_change",
        "worthless_removal",
        "rights_distribution",
        "partial_call",
        "reorganization",
    }
    assert set(ACTION_INTERPRETATION) == set(CorporateActionType)


def test_every_supported_type_parses_and_normalizes() -> None:
    found, token = parse_page(page({k: [v] for k, v in EXAMPLES.items()}))
    assert token is None and {p.action_type for p in found} == set(CorporateActionType)
    for p in found:
        assert subject_symbol(p.action_type, p.raw) == "AAA"
        a = to_action(
            p,
            security_id=1,
            observed_at=utc(2024, 4, 1),
            basis=KnowledgeBasis.PROSPECTIVE,
            resolve=lambda s, d: 2 if s == "BBB" else None,
        )
        assert a.raw_payload == p.raw and a.available_at == utc(2024, 4, 1)
        assert a.interpretation is ACTION_INTERPRETATION[p.action_type]


def test_split_and_dividend_parsing() -> None:
    def one(kind: CorporateActionType, **kw: object):  # type: ignore[no-untyped-def]
        return to_action(
            ProviderAction(kind, record(kind, **kw)),
            security_id=1,
            observed_at=utc(2024, 4, 1),
            basis=KnowledgeBasis.PROSPECTIVE,
            resolve=lambda s, d: None,
        )

    fwd = one(CorporateActionType.FORWARD_SPLIT)
    assert fwd.split_factor == 2.0 and str(fwd.ex_date) == "2024-03-12"
    rev = one(CorporateActionType.REVERSE_SPLIT)
    assert rev.split_factor == pytest.approx(1 / 50)
    div = one(CorporateActionType.CASH_DIVIDEND)
    assert div.cash_rate == 0.125 and str(div.ex_date) == "2024-03-04"
    assert str(div.payable_date) == "2024-03-19" and str(div.process_date) == "2024-03-19"
    stock = one(CorporateActionType.STOCK_DIVIDEND)
    assert stock.interpretation is ActionInterpretation.STOCK_DISTRIBUTION
    assert stock.split_factor == pytest.approx(1.05)
    # Event dates are never availability.
    assert all(d != fwd.available_at.date() for d in (fwd.ex_date, fwd.process_date))


def test_unknown_group_or_malformed_page_fails() -> None:
    with pytest.raises(CorporateActionsError):
        parse_page({"corporate_actions": {"mystery_events": []}, "next_page_token": None})
    with pytest.raises(CorporateActionsError):
        parse_page({"corporate_actions": {}})  # next_page_token missing
    with pytest.raises(CorporateActionsError):
        parse_page(
            {"corporate_actions": {"cash_dividends": [{"symbol": "AAA"}]}, "next_page_token": None}
        )


def test_query_requests_every_type_complete_quality_and_follows_pages() -> None:
    calls: list[httpx.Request] = []
    c = client(
        [
            page(
                {CorporateActionType.CASH_DIVIDEND: [record(CorporateActionType.CASH_DIVIDEND)]},
                "t1",
            ),
            page({CorporateActionType.FORWARD_SPLIT: [record(CorporateActionType.FORWARD_SPLIT)]}),
        ],
        calls,
    )
    res = c.query(["AAA"], START, END)
    assert res.page_count == 2 and len(res.actions) == 2
    p = calls[0].url.params
    assert (
        p["data_quality"] == "complete" and p["start"] == "2024-03-01" and p["end"] == "2024-03-31"
    )
    assert set(p["types"].split(",")) == {t.value for t in CorporateActionType}
    assert calls[1].url.params["page_token"] == "t1"
    assert calls[0].url.path == "/v1/corporate-actions"


@pytest.mark.parametrize(
    "pages",
    [
        [401],
        [403],
        [page({}, "t1"), 500, 500, 500, 500],  # retries exhausted mid-pagination
        [page({}, "t1"), page({}, "t1")],  # repeated page token
        [page({}, "t1"), 400],
    ],
)
def test_failed_or_partial_pagination_raises(pages: list[dict[str, object] | int]) -> None:
    with pytest.raises(CorporateActionsError):
        client(pages).query(["AAA"], START, END)


def test_transient_errors_are_retried_a_bounded_number_of_times() -> None:
    res = client([429, 503, page({})]).query(["AAA"], START, END)
    assert res.page_count == 1


def asset_transport(status: int, body: dict[str, object] | None = None) -> httpx.MockTransport:
    return httpx.MockTransport(lambda r: httpx.Response(status, json=body or {}))


def test_asset_status_poll_uses_the_paper_host_only() -> None:
    seen: list[httpx.Request] = []

    def handle(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return httpx.Response(200, json={"status": "inactive", "tradable": False, "symbol": "AAA"})

    obs = AlpacaAssets(
        httpx.MockTransport(handle), key_id="k", secret="s", env={}, clock=lambda: utc(2024, 4, 1)
    ).observe(1, "AAA")
    assert obs.status is AssetStatus.INACTIVE and obs.tradable is False
    assert seen[0].url.host == "paper-api.alpaca.markets"
    gone = AlpacaAssets(asset_transport(404), key_id="k", secret="s", env={}).observe(1, "AAA")
    assert gone.status is AssetStatus.NOT_FOUND
    with pytest.raises(CorporateActionsError):
        AlpacaAssets(asset_transport(500), key_id="k", secret="s", env={}).observe(1, "AAA")
    with pytest.raises(LiveTradingError):
        AlpacaAssets(env={"APCA_API_BASE_URL": "https://api.alpaca.markets"})
