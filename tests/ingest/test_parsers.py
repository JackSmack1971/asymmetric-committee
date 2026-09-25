"""Ingestor parsing from recorded-shape fixtures (tests/fixtures/, synthetic; no network)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import Connection

from contracts.enums import InsiderRole, InsiderTxnCode, NewsProviderName, PriceFeed
from ingest import alpaca, edgar_form4, edgar_submissions, edgar_xbrl, news
from ingest.timeutil import ET
from store import as_of
from store.write import insert_fundamentals, insert_news
from tests.store import factories as f

FIX = Path(__file__).resolve().parents[1] / "fixtures"
CIK = 900001


def load(rel: str) -> Any:
    return json.loads((FIX / rel).read_text())


@pytest.fixture
def company() -> edgar_submissions.Company:
    return edgar_submissions.parse_submissions(load("edgar/submissions_CIK0000900001.json"), [])


# --- submissions -----------------------------------------------------------------------------


def test_submissions_acceptance_is_read_as_eastern(company: edgar_submissions.Company) -> None:
    assert company.sic == 3571 and company.name == "Alfa Systems Inc"
    tenk = next(x for x in company.filings if x.form == "10-K")
    # "2024-02-15T16:31:10.000Z" is Eastern wall time on EDGAR: 16:31 EST = 21:31 UTC.
    assert tenk.accepted_at == datetime(2024, 2, 15, 21, 31, 10, tzinfo=UTC)


# --- XBRL ------------------------------------------------------------------------------------


def test_companyfacts_one_row_per_period_and_accession(company: edgar_submissions.Company) -> None:
    acceptance = {x.accession: x.accepted_at for x in company.filings}
    rows = edgar_xbrl.parse_companyfacts(
        load("edgar/companyfacts_CIK0000900001.json"), 1, acceptance
    )
    rev = [r for r in rows if r.concept == "us-gaap:Revenues"]
    # Q3 quarter and 9-month YTD share period_end and accession but are different facts.
    q3 = {
        (r.period_start, r.source_version): r.value
        for r in rev
        if r.period_end == date(2023, 9, 30)
    }
    assert q3 == {
        (date(2023, 7, 1), "0000900001-23-000088"): 500e6,
        (date(2023, 1, 1), "0000900001-23-000088"): 1400e6,
        (date(2023, 7, 1), "0000900001-24-000012"): 480e6,  # restated in the 10-K
    }
    fy23 = [r for r in rev if r.period_end == date(2023, 12, 31)]
    assert len(fy23) == 1  # duplicate (frame / no frame) entries collapse
    assert fy23[0].available_at == acceptance["0000900001-24-000012"]
    shares = [r for r in rows if r.concept == "dei:EntityCommonStockSharesOutstanding"]
    assert {r.unit for r in shares} == {"shares"}
    eps = next(r for r in rows if r.concept == "us-gaap:EarningsPerShareBasic")
    assert eps.unit == "USD/shares"


def test_companyfacts_unknown_accession_falls_back_to_end_of_filed_day() -> None:
    rows = edgar_xbrl.parse_companyfacts(load("edgar/companyfacts_CIK0000900001.json"), 1, {})
    old = next(r for r in rows if r.source_version == "0000900001-22-000999")
    assert old.available_at == datetime(2023, 2, 14, 23, 59, 59, tzinfo=ET)


def test_ingested_restatement_is_point_in_time(
    db: Connection, company: edgar_submissions.Company
) -> None:
    sid = f.security(db, "ALFA", CIK)
    acceptance = {x.accession: x.accepted_at for x in company.filings}
    rows = edgar_xbrl.parse_companyfacts(
        load("edgar/companyfacts_CIK0000900001.json"), sid, acceptance
    )
    assert insert_fundamentals(db, rows) == len(rows)
    assert insert_fundamentals(db, rows) == 0  # idempotent

    def q3(ts: datetime) -> float:
        (r,) = [
            r
            for r in as_of.fundamentals(db, sid, ts, ["us-gaap:Revenues"])
            if r.period_start == date(2023, 7, 1)
        ]
        return r.value

    tenk_at = acceptance["0000900001-24-000012"]
    assert q3(tenk_at - timedelta(seconds=1)) == 500e6
    assert q3(tenk_at) == 480e6


# --- Form 4 ----------------------------------------------------------------------------------


def _filing(company: edgar_submissions.Company, accn: str) -> edgar_submissions.Filing:
    return next(x for x in company.filings if x.accession == accn)


def test_form4_new_schema_checkbox(company: edgar_submissions.Company) -> None:
    filing = _filing(company, "0000900001-24-000031")
    xml = (FIX / "edgar/form4_0000900001-24-000031.xml").read_text()
    sale, withholding = edgar_form4.parse_form4(xml, filing, 7)
    assert sale.code is InsiderTxnCode.S and not sale.acquired
    assert (sale.shares, sale.price, sale.post_holdings) == (2500, 41.37, 97500)
    assert sale.is_10b5_1 and withholding.is_10b5_1  # document-level checkbox
    assert sale.role is InsiderRole.OFFICER and sale.officer_title == "Chief Financial Officer"
    assert withholding.code is InsiderTxnCode.F and withholding.seq == 1
    assert sale.txn_date == date(2024, 3, 4)
    assert sale.available_at == filing.accepted_at == datetime(2024, 3, 6, 21, 15, 2, tzinfo=UTC)


def test_form4_old_schema_footnote_flag(company: edgar_submissions.Company) -> None:
    filing = _filing(company, "0001234567-24-000001")
    xml = (FIX / "edgar/form4_0001234567-24-000001.xml").read_text()
    sale, buy = edgar_form4.parse_form4(xml, filing, 7)
    assert sale.is_10b5_1 and not buy.is_10b5_1  # footnote only on the sale
    assert buy.code is InsiderTxnCode.P and buy.acquired
    assert sale.role is InsiderRole.DIRECTOR and sale.filer == "Roe Richard"


def test_form4_url_uses_raw_xml(company: edgar_submissions.Company) -> None:
    filing = _filing(company, "0000900001-24-000031")
    assert edgar_form4.form4_url(CIK, filing) == (
        "https://www.sec.gov/Archives/edgar/data/900001/000090000124000031/wk-form4_1709759702.xml"
    )


def test_form4_filter_by_acceptance_window(company: edgar_submissions.Company) -> None:
    got = edgar_form4.form4_filings(
        company.filings,
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 2, 28, tzinfo=UTC),
    )
    assert [x.accession for x in got] == ["0001234567-24-000002", "0001234567-24-000001"]


# --- Alpaca bars -----------------------------------------------------------------------------


def test_sip_history_available_15m_after_close_and_paginates() -> None:
    client = alpaca.AlpacaBars(key_id="k", secret="s")
    pages = [load("alpaca/bars_ALFA_page1.json"), load("alpaca/bars_ALFA_page2.json")]
    with respx.mock() as mock:
        route = mock.get("https://data.alpaca.markets/v2/stocks/ALFA/bars").mock(
            side_effect=[httpx.Response(200, json=p) for p in pages]
        )
        bars = client.history(
            "ALFA", 3, date(2024, 3, 1), date(2024, 3, 11), now=datetime(2024, 3, 12, tzinfo=UTC)
        )
    assert route.calls[0].request.url.params["feed"] == "sip"
    assert route.calls[0].request.url.params["adjustment"] == "raw"
    assert route.calls[1].request.url.params["page_token"] == pages[0]["next_page_token"]
    assert route.calls[0].request.headers["APCA-API-KEY-ID"] == "k"
    assert [b.feed for b in bars] == [PriceFeed.SIP] * 3
    # 2024-03-04 (EST): close 21:00 UTC → +15m; 2024-03-11 (EDT): close 20:00 UTC → +15m.
    assert bars[1].available_at == datetime(2024, 3, 4, 21, 15, tzinfo=UTC)
    assert bars[2].available_at == datetime(2024, 3, 11, 20, 15, tzinfo=UTC)


def test_history_never_returns_bars_younger_than_15_minutes() -> None:
    client = alpaca.AlpacaBars(key_id="k", secret="s")
    now = datetime(2024, 3, 4, 21, 10, tzinfo=UTC)  # 16:10 ET on 2024-03-04
    with respx.mock() as mock:
        route = mock.get("https://data.alpaca.markets/v2/stocks/ALFA/bars").mock(
            return_value=httpx.Response(
                200, json={**load("alpaca/bars_ALFA_page1.json"), "next_page_token": None}
            )
        )
        bars = client.history("ALFA", 3, date(2024, 3, 1), date(2024, 3, 4), now=now)
    assert route.calls[0].request.url.params["end"] == "2024-03-04T20:55:00Z"
    assert [b.event_time.astimezone(ET).date() for b in bars] == [date(2024, 3, 1)]


def test_live_iex_bar_available_at_receipt() -> None:
    received = datetime(2024, 3, 4, 21, 1, tzinfo=UTC)
    bars = alpaca.parse_bars(
        load("alpaca/bars_ALFA_page1.json"), 3, PriceFeed.IEX, received_at=received
    )
    assert {b.available_at for b in bars} == {received}
    with pytest.raises(ValueError, match="receipt"):
        alpaca.parse_bars(load("alpaca/bars_ALFA_page1.json"), 3, PriceFeed.IEX)


def test_ingest_never_calls_a_trading_endpoint() -> None:
    src = "".join(p.read_text() for p in (Path(alpaca.__file__).parent).glob("*.py"))
    assert "api.alpaca.markets" not in src and "paper-api" not in src


# --- news ------------------------------------------------------------------------------------

SIDS = {"ALFA": 1, "BRVO": 2}


def test_alpaca_news_backfill_uses_update_stamp() -> None:
    items = news.parse_alpaca_news(load("news/alpaca_news.json"), SIDS, received_at=None)
    assert [n.item_id for n in items] == ["alpaca:37000001", "alpaca:37000002"]  # ZZZZ-only dropped
    pact = items[1]
    assert pact.security_ids == (1, 2)
    assert pact.published_at == datetime(2024, 2, 16, 13, 0, tzinfo=UTC)
    # Only the updated text exists, so it is not knowable before updated_at.
    assert pact.available_at == datetime(2024, 2, 16, 15, 30, tzinfo=UTC)
    assert pact.source is NewsProviderName.ALPACA


def test_alpaca_news_live_uses_receipt_time() -> None:
    received = datetime(2024, 2, 16, 15, 45, tzinfo=UTC)
    items = news.parse_alpaca_news(load("news/alpaca_news.json"), SIDS, received_at=received)
    assert {n.available_at for n in items} == {received}


def test_alphavantage_time_is_read_as_eastern() -> None:
    (item,) = news.parse_alphavantage_news(
        load("news/alphavantage_news.json"), SIDS, received_at=None
    )
    assert item.published_at == datetime(2024, 2, 15, 21, 40, 5, tzinfo=UTC)
    assert item.item_id.startswith("av:") and item.source is NewsProviderName.ALPHAVANTAGE


def test_alphavantage_error_payload_raises() -> None:
    with pytest.raises(ValueError, match="Alpha Vantage"):
        news.parse_alphavantage_news({"Information": "rate limit"}, SIDS, received_at=None)


def test_news_revision_is_a_new_row(db: Connection) -> None:
    a, b = f.security(db, "ALFA", 1), f.security(db, "BRVO", 2)
    sids = {"ALFA": a, "BRVO": b}
    payload = load("news/alpaca_news.json")
    first = news.parse_alpaca_news(payload, sids, received_at=None)
    edited = json.loads(json.dumps(payload))
    edited["news"][0]["summary"] = "Revenue rose 13% (corrected)."
    edited["news"][0]["updated_at"] = "2024-02-16T09:00:00Z"
    second = news.parse_alpaca_news(edited, sids, received_at=None)
    assert insert_news(db, first) == 2
    assert insert_news(db, second) == 1  # only the edited item is new
    lb = timedelta(days=7)
    before = as_of.news(db, [a], datetime(2024, 2, 16, 8, tzinfo=UTC), lb)
    after = as_of.news(db, [a], datetime(2024, 2, 16, 16, tzinfo=UTC), lb)
    assert [n.summary for n in before] == ["Revenue rose 12%."]
    assert sorted(n.summary for n in after) == ["Revenue rose 13% (corrected).", "Terms added."]


def test_provider_selection() -> None:
    assert isinstance(news.provider_from_env(env={"NEWS_PROVIDER": "alpaca"}), news.AlpacaNews)
    assert isinstance(
        news.provider_from_env(env={"NEWS_PROVIDER": "alphavantage"}), news.AlphaVantageNews
    )
    with pytest.raises(ValueError):
        news.provider_from_env(env={"NEWS_PROVIDER": "twitter"})


def test_timestamp_audit_flags_bad_stamps() -> None:
    now = datetime(2024, 2, 20, tzinfo=UTC)
    good = news.parse_alpaca_news(load("news/alpaca_news.json"), SIDS, received_at=None)
    assert news.timestamp_audit(good, now=now) == []
    future = good[0].model_copy(update={"published_at": now + timedelta(days=1)})
    assert news.timestamp_audit([future], now=now)
