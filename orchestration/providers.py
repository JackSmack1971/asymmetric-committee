"""Store-backed providers for the execution stage (§9): what to trade, at what price, on what data.

These implement the callables ``ExecutionStage`` already takes (``market``, ``reference``,
``feeds``); no new abstraction is introduced. Facts are read only through ``store.as_of`` (invariant
2), so a decision price is the last close knowable at the run's ``as_of``, never a later bar.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine

from contracts.enums import FeedName
from contracts.models import RunRecord
from execution.executor import ReferenceFn
from execution.reference import QuoteSource, reference_price
from orchestration.execution_stage import MarketView
from store import as_of as point_in_time

PRICE_LOOKBACK = timedelta(days=10)  # a decision price older than this is missing, not "latest"


class MarketViewError(RuntimeError):
    """The universe, the book or the broker's positions cannot be mapped to tradable symbols."""


class StoreMarketView:
    """``MarketView`` for a run: every name it may buy or must sell, with Friday-close prices.

    Symbols are the universe snapshot knowable at the run's ``as_of``, the names in its committed
    book, and every security the broker currently holds (so a name that fell out of the universe
    is sold, not forgotten). A held ticker that is not a stored security is an error: nothing is
    guessed about positions we cannot name.
    """

    def __init__(self, engine: Engine, held: Callable[[], Mapping[str, float]]) -> None:
        self._engine = engine
        self._held = held

    def __call__(self, run: RunRecord) -> MarketView:
        held = {sym for sym, qty in self._held().items() if qty != 0}
        with self._engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
            ids = {m.security_id for m in point_in_time.universe(conn, run.as_of)}
            listed_on = run.as_of.astimezone(UTC).date()
            listed = point_in_time.securities(conn, ids, listed_on=listed_on)
            by_id = {s.security_id: s for s in listed}
            by_ticker = {s.ticker: s for s in point_in_time.securities(conn)}
            unknown = sorted(held - set(by_ticker))
            if unknown:
                raise MarketViewError(
                    f"broker holds tickers that are not stored securities: {unknown}"
                )
            ids |= {by_ticker[t].security_id for t in held}
            symbols = {sid: by_id[sid].ticker for sid in ids if sid in by_id}
            symbols.update({by_ticker[t].security_id: t for t in held})
            if not symbols:
                raise MarketViewError(f"no tradable securities at {run.as_of.isoformat()}")
            last: dict[int, float] = {}
            newest: dict[int, datetime] = {}
            for bar in point_in_time.prices(conn, sorted(symbols), run.as_of, PRICE_LOOKBACK):
                if bar.event_time > newest.get(bar.security_id, bar.event_time - timedelta(1)):
                    newest[bar.security_id] = bar.event_time
                    last[bar.security_id] = bar.close
        return MarketView(symbols=symbols, decision_prices=last)


def store_feeds(
    engine: Engine,
) -> Callable[[], Mapping[FeedName, datetime | None]]:
    """Last successful ingest per feed, from ``feed_health`` (what the stale-feed halt reads)."""

    def read() -> Mapping[FeedName, datetime | None]:
        with engine.connect() as conn:
            return {h.feed: h.last_success_at for h in point_in_time.feed_health(conn)}

    return read


def reference_provider(
    source: QuoteSource, clock: Callable[[], datetime]
) -> Callable[[str], ReferenceFn]:
    """The §9 reference-price rule (IEX midpoint if the spread is at most 50 bps, else a SIP trade
    at least 15 minutes old) as the ``symbol -> ReferenceFn`` the stage wants."""

    def for_symbol(symbol: str) -> ReferenceFn:
        return lambda: reference_price(source, symbol, clock())

    return for_symbol
