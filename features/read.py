"""Point-in-time adapter for the pure feature builder."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from contracts.data import FeatureRow, FundamentalFact, PriceBar
from features.builder import build_feature_rows
from store import as_of as point_in_time
from store.as_of import Conn


def build_as_of(conn: Conn, as_of: datetime) -> tuple[FeatureRow, ...]:
    members = point_in_time.universe(conn, as_of)
    ids = [member.security_id for member in members]
    securities = {s.security_id: s for s in point_in_time.securities(conn, ids)}
    price_rows = point_in_time.prices(conn, ids, as_of, timedelta(days=400))
    prices: dict[int, list[PriceBar]] = defaultdict(list)
    for row in price_rows:
        prices[row.security_id].append(row)
    fundamentals: dict[int, list[FundamentalFact]] = {
        sid: point_in_time.fundamentals(conn, sid, as_of) for sid in ids
    }
    return build_feature_rows(
        as_of=as_of,
        security_ids=ids,
        sectors={sid: securities[sid].sector or "Unknown" for sid in ids},
        market_caps={member.security_id: member.mcap_usd for member in members},
        prices=prices,
        fundamentals=fundamentals,
        insiders=point_in_time.insider_txns(conn, ids, as_of, timedelta(days=90)),
        news=point_in_time.news(conn, ids, as_of, timedelta(days=56)),
    )
