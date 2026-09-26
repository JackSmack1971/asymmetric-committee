"""Paper-only enforcement, wire format and reference-price policy. Mock transports only."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest

from contracts.enums import BrokerOrderStatus as S
from contracts.enums import OrderSide, ReferenceSource
from execution.alpaca import (
    PAPER_URL,
    AlpacaPaperGateway,
    LiveTradingError,
    paper_base_url,
    parse_order,
)
from execution.gateway import BrokerError, DuplicateClientOrderError, OrderNotCancelableError
from execution.reference import (
    AlpacaMarketData,
    NoReferencePriceError,
    reference_price,
)

NOW = datetime(2024, 3, 4, 14, 30, tzinfo=UTC)
ORDER = {
    "id": "b-1", "client_order_id": "c-1", "symbol": "ZZZ", "side": "buy", "type": "limit",
    "qty": "100", "filled_qty": "37", "limit_price": "100.25", "filled_avg_price": "99.5",
    "status": "partially_filled", "submitted_at": "2024-03-04T14:30:00.123456789Z",
    "filled_at": None,
}  # fmt: skip


@pytest.mark.parametrize(
    "url",
    [
        "https://api.alpaca.markets",
        "http://paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets.evil.com",
        "https://evil.com@paper-api.alpaca.markets.evil.com",
        "https://user:pw@paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets:8443",
        "https://paper-api.alpaca.markets/v2/../live",
        "https://paper-api.alpaca.markets?x=1",
        "localhost",
    ],
)
@pytest.mark.parametrize("var", ["ALPACA_PAPER_BASE_URL", "APCA_API_BASE_URL", "ALPACA_BASE_URL"])
def test_any_non_paper_configuration_fails_closed(var: str, url: str) -> None:
    with pytest.raises(LiveTradingError):
        paper_base_url({var: url})
    with pytest.raises(LiveTradingError):
        AlpacaPaperGateway(env={var: url})


def test_paper_urls_are_accepted_and_default_is_paper() -> None:
    assert paper_base_url({}) == PAPER_URL
    assert (
        paper_base_url({"ALPACA_PAPER_BASE_URL": "https://paper-api.alpaca.markets/"}) == PAPER_URL
    )
    assert paper_base_url({"APCA_API_BASE_URL": "https://paper-api.alpaca.markets/v2"}) == PAPER_URL


def test_every_request_is_pinned_to_the_paper_host_and_redirects_are_not_followed() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(302, headers={"location": "https://api.alpaca.markets/v2/orders"})

    gw = AlpacaPaperGateway(httpx.MockTransport(handler), env={})
    with pytest.raises(Exception):  # noqa: B017 - a 302 is an error, never followed
        gw.get_order("b-1")
    assert [r.url.host for r in seen] == ["paper-api.alpaca.markets"]
    with pytest.raises(LiveTradingError):  # a request built for another host is refused outright
        gw._http.get("https://api.alpaca.markets/v2/orders")


def test_parse_order_and_status_mapping() -> None:
    o = parse_order(ORDER)
    assert (o.filled_qty, o.filled_avg_price, o.status) == (37.0, 99.5, S.PARTIALLY_FILLED)
    assert parse_order({**ORDER, "status": "pending_cancel"}).status is S.PENDING_CANCEL
    assert parse_order({**ORDER, "status": "canceled"}).status.terminal
    for unknown in ("new", "accepted", "pending_new", "replaced", "something_new"):
        assert not parse_order({**ORDER, "status": unknown}).status.terminal  # never a false cancel


def test_submit_sends_day_orders_and_maps_duplicates() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            bodies.append(json.loads(req.content))
            if len(bodies) == 3:
                return httpx.Response(422, json={"message": "client_order_id must be unique"})
            return httpx.Response(200, json={**ORDER, "status": "new", "filled_qty": "0"})
        if req.url.path == "/v2/orders:by_client_order_id":
            return httpx.Response(404, json={})
        if req.method == "DELETE":
            return httpx.Response(422, json={"message": "order not cancelable"})
        return httpx.Response(200, json=[])

    gw = AlpacaPaperGateway(httpx.MockTransport(handler), env={})
    gw.submit_limit(
        client_order_id="c-1", symbol="ZZZ", side=OrderSide.BUY, qty=100, limit_price=100.25
    )
    gw.submit_market(client_order_id="c-2", symbol="ZZZ", side=OrderSide.SELL, qty=5)
    assert [b["type"] for b in bodies] == ["limit", "market"]
    assert all(b["time_in_force"] == "day" for b in bodies)
    assert bodies[0]["limit_price"] == "100.25" and bodies[0]["client_order_id"] == "c-1"
    with pytest.raises(DuplicateClientOrderError):
        gw.submit_market(client_order_id="c-2", symbol="ZZZ", side=OrderSide.SELL, qty=5)
    assert gw.find_by_client_id("nope") is None
    with pytest.raises(OrderNotCancelableError):
        gw.cancel_order("b-1")


# --- reference price -------------------------------------------------------------------------


class Quotes:
    def __init__(self, quote: tuple[float, float] | None, sip: float | None) -> None:
        self.quote, self.sip = quote, sip
        self.sip_cutoff: datetime | None = None

    def iex_quote(self, symbol: str) -> tuple[float, float] | None:
        return self.quote

    def sip_last_trade(self, symbol: str, *, at_or_before: datetime) -> float | None:
        self.sip_cutoff = at_or_before
        return self.sip


def test_iex_midpoint_when_spread_within_50_bps() -> None:
    q = Quotes((99.80, 100.20), sip=90.0)  # 40 bps
    price, source = reference_price(q, "ZZZ", NOW)
    assert price == pytest.approx(100.0) and source is ReferenceSource.IEX_MID
    assert q.sip_cutoff is None


def test_wide_or_missing_iex_falls_back_to_delayed_sip_trade() -> None:
    wide = Quotes((99.0, 101.0), sip=98.5)  # 200 bps
    assert reference_price(wide, "ZZZ", NOW) == (98.5, ReferenceSource.SIP_LAST)
    assert wide.sip_cutoff == NOW - timedelta(minutes=15)
    assert reference_price(Quotes(None, 98.5), "ZZZ", NOW)[1] is ReferenceSource.SIP_LAST
    crossed = Quotes((101.0, 99.0), sip=98.5)
    assert reference_price(crossed, "ZZZ", NOW)[1] is ReferenceSource.SIP_LAST
    edge = Quotes((99.75, 100.25), sip=1.0)  # exactly 50 bps: still IEX
    assert reference_price(edge, "ZZZ", NOW)[1] is ReferenceSource.IEX_MID


def test_no_reference_means_no_trade() -> None:
    with pytest.raises(NoReferencePriceError):
        reference_price(Quotes(None, None), "ZZZ", NOW)


def test_market_data_lookup_uses_data_host_only_and_respects_delay() -> None:
    hosts: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        hosts.append(req.url.host)
        if "quotes" in req.url.path:
            return httpx.Response(200, json={"quote": {"bp": 99.0, "ap": 101.0}})
        assert req.url.params["feed"] == "sip" and req.url.params["end"] == "2024-03-04T14:15:00Z"
        return httpx.Response(200, json={"trades": [{"t": "2024-03-04T14:14:59.5Z", "p": 98.7}]})

    md = AlpacaMarketData(httpx.MockTransport(handler))
    assert reference_price(md, "ZZZ", NOW) == (98.7, ReferenceSource.SIP_LAST)
    assert set(hosts) == {"data.alpaca.markets"}


def test_the_official_calendar_gives_sessions_including_early_closes() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(
            200,
            json=[
                {"date": "2024-11-29", "open": "09:30", "close": "13:00"},  # day after Thanksgiving
                {"date": "2024-11-27", "open": "09:30", "close": "16:00"},
            ],
        )

    gw = AlpacaPaperGateway(httpx.MockTransport(handler), env={})
    got = gw.sessions(date(2024, 11, 25), date(2024, 12, 1))
    assert [s.session_date for s in got] == [date(2024, 11, 27), date(2024, 11, 29)]  # oldest first
    assert got[1].closes_at == datetime(2024, 11, 29, 18, 0, tzinfo=UTC)  # 13:00 EST
    assert got[0].opens_at == datetime(2024, 11, 27, 14, 30, tzinfo=UTC)
    (req,) = seen
    assert req.url.host == "paper-api.alpaca.markets" and req.url.path == "/v2/calendar"
    assert dict(req.url.params) == {"start": "2024-11-25", "end": "2024-12-01"}


def test_a_broker_error_on_the_calendar_is_never_read_as_an_empty_calendar() -> None:
    gw = AlpacaPaperGateway(httpx.MockTransport(lambda r: httpx.Response(503, text="down")), env={})
    with pytest.raises(BrokerError):
        gw.sessions(date(2024, 3, 1), date(2024, 3, 8))
