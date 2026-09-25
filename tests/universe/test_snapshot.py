"""Monthly universe snapshots (§4.4): rules, ranking, survivorship safety."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import Connection

from config.loader import UniverseConfig
from contracts.data import UniverseMember
from contracts.enums import McapTier
from ingest.timeutil import ET, session_close
from store import as_of
from store.write import insert_fundamentals, insert_price_bars, set_listing
from tests.store import factories as f
from universe.sectors import SECTORS, sector_for_sic
from universe.snapshot import build_snapshot, month_ends, snapshot_month_ends, snapshot_time


def build(db: Connection, d: date) -> list[UniverseMember]:
    return build_snapshot(db, d, CFG)


CFG = UniverseConfig(
    sectors=("Information Technology",),
    market_cap_min_usd=500e6,
    market_cap_max_usd=50e9,
    adv20_min_usd=10e6,
    price_min_usd=5.0,
    top_n=2,
)


def _weekdays(start: date, end: date) -> list[date]:
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _seed(
    db: Connection,
    ticker: str,
    cik: int,
    *,
    close: float,
    volume: float,
    shares: float,
    sector: str = "Information Technology",
) -> int:
    sid = f.security(db, ticker, cik, sector)
    bars = []
    for d in _weekdays(date(2024, 1, 1), date(2024, 3, 29)):
        b = f.bar(sid, d, close=close, available_at=session_close(d) + timedelta(minutes=15))
        bars.append(
            b.model_copy(
                update={
                    "volume": volume,
                    "event_time": datetime.combine(d, datetime.min.time(), ET),
                }
            )
        )
    insert_price_bars(db, bars)
    insert_fundamentals(
        db,
        [
            f.fact(
                sid,
                available_at=datetime(2023, 11, 2, 21, tzinfo=ET),
                accn=f.accession(cik % 1000),
                value=shares,
                concept="dei:EntityCommonStockSharesOutstanding",
                period_end=date(2023, 10, 27),
                period_start=None,
            )
        ],
    )
    return sid


def test_month_ends_are_last_weekdays() -> None:
    assert month_ends(date(2024, 1, 1), date(2024, 4, 1)) == [
        date(2024, 1, 31),
        date(2024, 2, 29),
        date(2024, 3, 29),
    ]


def test_sic_sector_map() -> None:
    assert sector_for_sic(3571) == "Information Technology"
    assert sector_for_sic(2834) == "Health Care"
    assert sector_for_sic(1311) == "Energy"
    assert sector_for_sic(6798) == "Real Estate"
    assert sector_for_sic(None) is None
    assert sector_for_sic(9999) is None
    assert {sector_for_sic(s) for s in range(100, 9000)} - {None} <= set(SECTORS)


def test_filters_rank_and_survivorship(db: Connection) -> None:
    big = _seed(db, "BIGG", 1, close=50, volume=2e6, shares=100e6)  # adv 100M, mcap 5B
    mid = _seed(db, "MIDD", 2, close=40, volume=1e6, shares=100e6)  # adv 40M, mcap 4B
    small = _seed(db, "SMAL", 3, close=20, volume=1e6, shares=100e6)  # adv 20M: rank 3 > top 2
    penny = _seed(db, "PENY", 4, close=3, volume=9e6, shares=500e6)  # price < 5
    illiquid = _seed(db, "ILLQ", 5, close=30, volume=1e5, shares=100e6)  # adv 3M
    _seed(db, "BANK", 6, close=50, volume=2e6, shares=100e6, sector="Financials")
    # MIDD delists mid-February.
    set_listing(db, mid, listed_from=date(2010, 1, 1), listed_to=date(2024, 2, 15))

    snapshot_month_ends(db, date(2024, 1, 1), date(2024, 3, 31), CFG)

    jan = {
        m.security_id: m
        for m in as_of.universe(db, snapshot_time(date(2024, 1, 31)), included_only=False)
    }
    assert set(jan) == {big, mid, small, penny, illiquid}  # BANK: out-of-sector, not evaluated
    assert jan[big].included and jan[mid].included and jan[mid].rank == 2
    assert jan[small].reason == "rank" and not jan[small].included
    feb = {
        m.security_id: m
        for m in as_of.universe(db, snapshot_time(date(2024, 2, 29)), included_only=False)
    }
    assert feb[big].included and feb[big].rank == 1 and feb[big].mcap_tier is McapTier.MID
    assert feb[small].included and feb[small].rank == 2
    assert feb[penny].reason == "price" and feb[illiquid].reason == "adv"
    assert mid not in feb  # delisted before the snapshot date

    # The January snapshot still holds the delisted name, read at any later time.
    later = datetime(2024, 6, 1, tzinfo=ET)
    assert as_of.universe(db, later)[0].snapshot_date == date(2024, 3, 29)
    jan_again = as_of.universe(db, snapshot_time(date(2024, 2, 29)) - timedelta(seconds=1))
    assert {m.security_id for m in jan_again} == {big, mid}


def test_short_history_is_excluded(db: Connection) -> None:
    sid = _seed(db, "BIGG", 1, close=50, volume=2e6, shares=100e6)
    (m,) = [x for x in build(db, date(2024, 1, 12)) if x.security_id == sid]
    assert m.reason == "insufficient_history" and not m.included


def test_snapshot_is_invisible_before_its_date(db: Connection) -> None:
    _seed(db, "BIGG", 1, close=50, volume=2e6, shares=100e6)
    snapshot_month_ends(db, date(2024, 2, 1), date(2024, 2, 29), CFG)
    assert as_of.universe(db, snapshot_time(date(2024, 2, 29)) - timedelta(seconds=1)) == []
