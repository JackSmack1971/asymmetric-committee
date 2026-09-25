"""Monthly rules-based universe (§4.4), built only from point-in-time reads.

On each snapshot date: names listed that day in the configured sectors; market cap (latest dei
shares outstanding * last close) within the band; 20-day average dollar volume ≥ min; price ≥ min.
Survivors are ranked by log(ADV20) (SPEC-GAP: §4.4 names a "liquidity-adjusted score" without
defining it) and the top N are included. Every evaluated candidate is stored with a reason, and
rows are never deleted, so delisted names stay in historical snapshots.
"""

from __future__ import annotations

import calendar
import hashlib
import math
from datetime import UTC, date, datetime, time, timedelta

from config.loader import UniverseConfig
from contracts.data import Security, UniverseMember
from contracts.enums import McapTier
from ingest.timeutil import at_et
from store import as_of
from store.as_of import Conn
from store.write import insert_universe_snapshot

SHARES_CONCEPT = "dei:EntityCommonStockSharesOutstanding"
ADV_DAYS = 20


def month_ends(start: date, end: date) -> list[date]:
    """Last weekday of each month in ``[start, end]`` (exchange holidays are not modelled)."""
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        d = date(y, m, calendar.monthrange(y, m)[1])
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        if start <= d <= end:
            out.append(d)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def snapshot_time(d: date) -> datetime:
    """Snapshots use what was knowable by the end of the snapshot day (Eastern)."""
    return at_et(d, time(23, 59)).astimezone(UTC)


def config_hash(cfg: UniverseConfig) -> str:
    return hashlib.sha256(cfg.model_dump_json().encode()).hexdigest()[:16]


def mcap_tier(mcap: float) -> McapTier:
    if mcap < 2e9:
        return McapTier.SMALL
    if mcap < 10e9:
        return McapTier.MID
    return McapTier.LARGE


def _evaluate(
    conn: Conn, sec: Security, ts: datetime, cfg: UniverseConfig
) -> tuple[str, float | None, float | None, float | None]:
    """(reason, price, adv, mcap); reason is "ok" when every filter passes."""
    bars = as_of.prices(conn, [sec.security_id], ts, lookback=timedelta(days=45))
    if not bars:
        return "no_price", None, None, None
    price = bars[-1].close
    recent = bars[-ADV_DAYS:]
    adv = sum(b.close * b.volume for b in recent) / len(recent)
    shares = as_of.fundamentals(conn, sec.security_id, ts, [SHARES_CONCEPT])
    mcap = price * max(shares, key=lambda s: s.period_end).value if shares else None
    if len(recent) < ADV_DAYS:
        return "insufficient_history", price, adv, mcap
    if price < cfg.price_min_usd:
        return "price", price, adv, mcap
    if mcap is None:
        return "no_shares", price, adv, mcap
    if not cfg.market_cap_min_usd <= mcap <= cfg.market_cap_max_usd:
        return "mcap", price, adv, mcap
    if adv < cfg.adv20_min_usd:
        return "adv", price, adv, mcap
    return "ok", price, adv, mcap


def build_snapshot(conn: Conn, snapshot_date: date, cfg: UniverseConfig) -> list[UniverseMember]:
    ts = snapshot_time(snapshot_date)
    version = config_hash(cfg)
    candidates = [
        s for s in as_of.securities(conn, listed_on=snapshot_date) if s.sector in cfg.sectors
    ]
    evaluated = [(s, *_evaluate(conn, s, ts, cfg)) for s in candidates]
    passing = sorted(
        (e for e in evaluated if e[1] == "ok"),
        key=lambda e: (-(e[3] or 0.0), e[0].security_id),
    )
    rank = {e[0].security_id: i + 1 for i, e in enumerate(passing)}
    out = []
    for sec, reason, price, adv, mcap in evaluated:
        r = rank.get(sec.security_id)
        included = r is not None and r <= cfg.top_n
        out.append(
            UniverseMember(
                snapshot_date=snapshot_date,
                security_id=sec.security_id,
                mcap_usd=mcap,
                mcap_tier=mcap_tier(mcap) if mcap is not None else None,
                adv_usd=adv,
                price=price,
                score=math.log(adv) if adv else None,
                rank=r,
                included=included,
                reason="included" if included else ("rank" if reason == "ok" else reason),
                event_time=ts,
                available_at=ts,
                source_version=version,
            )
        )
    return out


def snapshot_month_ends(conn: Conn, start: date, end: date, cfg: UniverseConfig) -> int:
    """Build and store every month-end snapshot in the range; returns rows written."""
    return sum(
        insert_universe_snapshot(conn, build_snapshot(conn, d, cfg)) for d in month_ends(start, end)
    )
