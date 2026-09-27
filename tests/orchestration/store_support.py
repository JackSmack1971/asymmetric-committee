"""Shared seed world for store-backed orchestration tests (Postgres, service-gated)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Engine

from contracts.data import FeatureRow, UniverseMember
from contracts.enums import McapTier
from store import write
from tests.agents.world import AS_OF, _feature_row, _securities
from tests.store import factories as f

LATER = AS_OF + timedelta(days=3)  # not yet knowable at AS_OF


def _member(sid: int, snapshot: date, available_at: datetime, rank: int) -> UniverseMember:
    return UniverseMember(
        snapshot_date=snapshot,
        security_id=sid,
        mcap_usd=5e9,
        mcap_tier=McapTier.MID,
        adv_usd=5e7,
        price=10.0,
        score=1.0 / rank,
        rank=rank,
        included=True,
        reason="included",
        event_time=datetime.combine(snapshot, datetime.min.time(), UTC),
        available_at=available_at,
        source_version="cfg1",
    )


def _feature(sid: int, event_time: datetime, available_at: datetime, version: str) -> FeatureRow:
    return _feature_row(sid).model_copy(
        update={"event_time": event_time, "available_at": available_at, "source_version": version}
    )


def seed(engine: Engine) -> list[int]:
    """Four listed names with a knowable snapshot and, for every table, a future-available row."""
    ids: list[int] = []
    with engine.begin() as c:
        for s in _securities():
            sid = write.ensure_security(
                c, ticker=s.ticker, cik=s.cik, name=s.name, sector=s.sector, industry=None
            )
            write.set_listing(c, sid, listed_from=date(2015, 1, 1), listed_to=None)
            ids.append(sid)
        snap_ok = AS_OF - timedelta(days=7)
        write.insert_universe_snapshot(
            c,
            [_member(sid, date(2025, 2, 28), snap_ok, n) for n, sid in enumerate(ids, start=1)]
            # a newer snapshot that is published only after AS_OF must not be used
            + [_member(sid, date(2025, 3, 31), LATER, n) for n, sid in enumerate(ids, start=1)],
        )
        rows: list[FeatureRow] = []
        for sid in ids:
            rows.append(_feature(sid, AS_OF - timedelta(days=1), AS_OF - timedelta(hours=1), "a"))
            rows.append(_feature(sid, LATER, LATER, "b"))  # a future vector
        write.insert_features(c, rows)
        for sid in ids:
            write.insert_fundamentals(
                c,
                [
                    f.fact(
                        sid, available_at=AS_OF - timedelta(days=30), accn="0000000001-25-000001"
                    ),
                    f.fact(
                        sid,
                        available_at=LATER,  # a restatement filed after AS_OF
                        accn="0000000001-25-000002",
                        value=999.0,
                    ),
                ],
            )
            write.insert_insider_txns(
                c,
                [
                    f.insider(
                        sid,
                        txn_date=date(2025, 3, 3),
                        accepted_at=AS_OF - timedelta(days=2),
                        accn=f"0000000002-25-{sid:06d}",
                    ),
                    f.insider(
                        sid,
                        txn_date=date(2025, 3, 6),  # transacted before AS_OF, filed after it
                        accepted_at=LATER,
                        accn=f"0000000002-25-{1000 + sid:06d}",
                    ),
                ],
            )
            write.insert_news(
                c,
                [
                    f.news_item(
                        (sid,),
                        f"n{sid}-old",
                        published_at=AS_OF - timedelta(days=2),
                        available_at=AS_OF - timedelta(days=2),
                        revision="r1",
                    ),
                    f.news_item(
                        (sid,),
                        f"n{sid}-late",
                        published_at=AS_OF - timedelta(days=1),  # published before, ingested after
                        available_at=LATER,
                        revision="r1",
                    ),
                ],
            )
            bars = [
                f.bar(sid, date(2025, 1, 20) + timedelta(days=i), close=10.0) for i in range(46)
            ]
            # a later SIP correction of an already-visible bar, not yet available
            bars.append(f.bar(sid, date(2025, 3, 6), close=999.0, available_at=LATER))
            write.insert_price_bars(c, bars)
    return ids
