"""Store-backed providers for the execution stage, over a real Postgres (service-gated)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import Engine, text

from contracts.enums import FeedName, ReferenceSource, RunMode, RunStatus
from contracts.models import RunRecord
from execution.reference import NoReferencePriceError
from orchestration.providers import (
    MarketViewError,
    StoreMarketView,
    reference_provider,
    store_feeds,
)
from store import write
from tests.agents.world import AS_OF
from tests.orchestration.store_support import seed
from tests.store import factories as f


@pytest.fixture
def engine(pg_engine: Engine) -> Iterator[Engine]:
    def wipe() -> None:
        with pg_engine.begin() as c:
            c.execute(text("TRUNCATE runs, securities RESTART IDENTITY CASCADE"))
            c.execute(
                text(
                    "TRUNCATE universe_snapshots, features, fundamentals_asfiled, insider_txns, "
                    "news_items, price_bars, feed_health RESTART IDENTITY CASCADE"
                )
            )

    wipe()
    yield pg_engine
    wipe()


def run_at(as_of: datetime) -> RunRecord:
    return RunRecord(
        run_id=uuid4(),
        mode=RunMode.LIVE,
        as_of=as_of,
        config_hash="c" * 64,
        status=RunStatus.ANCHORED,
        started_at=as_of,
    )


def test_the_view_is_the_knowable_universe_with_friday_close_prices(engine: Engine) -> None:
    ids = seed(engine)
    view = StoreMarketView(engine, lambda: {})(run_at(AS_OF))
    assert sorted(view.symbols) == sorted(ids)
    assert set(view.symbols.values()) == {"ZPHR", "BRLS", "CLDR", "DNMR"}
    # every close is 10.0; the 999.0 SIP correction only becomes available at LATER
    assert set(view.decision_prices) == set(ids)
    assert set(view.decision_prices.values()) == {10.0}


def test_a_security_outside_the_knowable_snapshot_is_never_included(engine: Engine) -> None:
    seed(engine)
    with engine.begin() as c:  # a name added by a snapshot published only after AS_OF
        extra = write.ensure_security(
            c, ticker="LATE", cik=9_000_001, name="Late Corp", sector="Tech"
        )
        write.set_listing(c, extra, listed_from=date(2015, 1, 1), listed_to=None)
    view = StoreMarketView(engine, lambda: {})(run_at(AS_OF))
    assert "LATE" not in view.symbols.values()


def test_held_names_outside_the_universe_are_included_so_they_can_be_sold(engine: Engine) -> None:
    seed(engine)
    with engine.begin() as c:
        dropped = write.ensure_security(
            c, ticker="OLD", cik=9_000_002, name="Old Corp", sector="Tech"
        )
        write.set_listing(c, dropped, listed_from=date(2015, 1, 1), listed_to=None)
        write.insert_price_bars(
            c,
            [f.bar(dropped, date(2025, 3, 6), close=42.0, available_at=AS_OF - timedelta(days=1))],
        )
    view = StoreMarketView(engine, lambda: {"OLD": 30.0, "ZPHR": 5.0, "BRLS": 0.0})(run_at(AS_OF))
    assert view.symbols[dropped] == "OLD" and view.decision_prices[dropped] == 42.0


def test_a_position_we_cannot_name_is_an_error_not_a_guess(engine: Engine) -> None:
    seed(engine)
    with pytest.raises(MarketViewError, match="not stored securities"):
        StoreMarketView(engine, lambda: {"MYSTERY": 10.0})(run_at(AS_OF))


def test_no_knowable_universe_means_no_view(engine: Engine) -> None:
    seed(engine)
    with pytest.raises(MarketViewError, match="no tradable securities"):
        StoreMarketView(engine, lambda: {})(run_at(AS_OF - timedelta(days=90)))


def test_a_name_with_no_recent_bar_has_no_decision_price(engine: Engine) -> None:
    ids = seed(engine)
    with engine.begin() as c:
        c.execute(text("DELETE FROM price_bars WHERE security_id = :s"), {"s": ids[0]})
    view = StoreMarketView(engine, lambda: {})(run_at(AS_OF))
    assert ids[0] in view.symbols and ids[0] not in view.decision_prices  # the plan fails closed


def test_feeds_report_the_last_successful_ingest(engine: Engine) -> None:
    at = datetime(2025, 3, 7, 12, 0, tzinfo=UTC)
    with engine.begin() as c:
        write.record_feed_run(c, FeedName.PRICE_BARS, at=at, rows=5)
        write.record_feed_run(c, FeedName.NEWS, at=at - timedelta(hours=3), rows=1)
        write.record_feed_run(
            c, FeedName.NEWS, at=at, error="boom"
        )  # a failure keeps the last success
    got = store_feeds(engine)()
    assert got[FeedName.PRICE_BARS] == at and got[FeedName.NEWS] == at - timedelta(hours=3)
    assert FeedName.INSIDER_TRADES not in got  # never ran: the kill switch reads that as stale


class Quotes:
    def __init__(self, quote: tuple[float, float] | None, trade: float | None) -> None:
        self.quote, self.trade = quote, trade
        self.trade_asked_at: datetime | None = None

    def iex_quote(self, symbol: str) -> tuple[float, float] | None:
        return self.quote

    def sip_last_trade(self, symbol: str, *, at_or_before: datetime) -> float | None:
        self.trade_asked_at = at_or_before
        return self.trade


def test_the_reference_provider_applies_the_section_9_rule() -> None:
    now = datetime(2025, 3, 10, 14, 30, tzinfo=UTC)
    tight = reference_provider(Quotes((99.9, 100.1), 50.0), lambda: now)("ZPHR")()
    assert tight[1] is ReferenceSource.IEX_MID and tight[0] == pytest.approx(100.0)

    wide = Quotes((90.0, 110.0), 101.5)
    assert reference_provider(wide, lambda: now)("ZPHR")() == (101.5, ReferenceSource.SIP_LAST)
    assert wide.trade_asked_at == now - timedelta(minutes=15)  # never a trade inside the delay

    with pytest.raises(NoReferencePriceError):
        reference_provider(Quotes(None, None), lambda: now)("ZPHR")()
