"""Backfill smoke (gate-P1): 5 tickers x 90 days replayed from tests/fixtures/http, no network."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text

from contracts.enums import FeedName
from ingest.backfill import main
from ingest.timeutil import session_close
from store import as_of
from tests.conftest import reset
from tests.fixtures.synth import END, HTTP_DIR, SMOKE_ARGS, TICKERS
from universe.snapshot import snapshot_time


@pytest.fixture
def db_url(pg_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> str:
    with pg_engine.begin() as conn:
        reset(conn)
    monkeypatch.setenv("NEWS_PROVIDER", "alpaca")
    monkeypatch.delenv("REDIS_URL", raising=False)
    return pg_engine.url.render_as_string(hide_password=False)


def _run(db_url: str, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    rc = main([*SMOKE_ARGS, "--replay", str(HTTP_DIR), "--database-url", db_url])
    out: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert rc == 0, out
    return out


def test_backfill_smoke(db_url: str, pg_engine: Engine, capsys: pytest.CaptureFixture[str]) -> None:
    out = _run(db_url, capsys)
    assert out["securities"] == len(TICKERS) == 5
    rows = out["rows"]
    assert isinstance(rows, dict)
    assert rows[FeedName.PRICE_BARS.value] >= 5 * 60  # ~63 sessions per name
    assert all(
        rows[f.value] > 0 for f in (FeedName.FUNDAMENTALS, FeedName.INSIDER_TRADES, FeedName.NEWS)
    )

    with pg_engine.connect() as conn:
        by_ticker = {s.ticker: s.security_id for s in as_of.securities(conn)}
        sids = list(by_ticker.values())
        at_end = session_close(END) + timedelta(minutes=15)

        # Point in time: nothing later than as_of, from any table.
        bars = as_of.prices(conn, sids, at_end, timedelta(days=90))
        assert len(bars) == rows[FeedName.PRICE_BARS.value]
        assert max(b.available_at for b in bars) <= at_end
        mid = datetime(2024, 5, 15, 12, tzinfo=UTC)
        assert all(b.available_at <= mid for b in as_of.prices(conn, sids, mid, timedelta(days=90)))
        txns = as_of.insider_txns(conn, sids, at_end, timedelta(days=120))
        assert txns and all(t.available_at > t.event_time for t in txns)  # filed after the trade

        # The FY2023 revenue restatement in the May 10-Q: first value before, restated after.
        alfa = by_ticker["ALFA"]

        def fy23(ts: datetime) -> float:
            (r,) = [
                r
                for r in as_of.fundamentals(conn, alfa, ts, ["us-gaap:Revenues"])
                if r.period_end == date(2023, 12, 31)
            ]
            return r.value

        assert fy23(datetime(2024, 5, 1, tzinfo=UTC)) > fy23(datetime(2024, 5, 3, tzinfo=UTC))

        # Month-end snapshots: IT + Health Care only, top 2 by liquidity.
        members = as_of.universe(conn, snapshot_time(END), included_only=False)
        assert {m.snapshot_date for m in members} == {END}
        reasons = {m.reason for m in members}
        assert sum(m.included for m in members) == 2 and "adv" in reasons
        assert by_ticker["ECHO"] not in {m.security_id for m in members}  # Financials
        snaps = conn.execute(text("SELECT DISTINCT snapshot_date FROM universe_snapshots"))
        assert sorted(d for (d,) in snaps) == [date(2024, 4, 30), date(2024, 5, 31), END]

        health = {h.feed: h for h in as_of.feed_health(conn)}
        assert set(health) == {
            FeedName.PRICE_BARS,
            FeedName.FUNDAMENTALS,
            FeedName.INSIDER_TRADES,
            FeedName.NEWS,
        }
        assert all(h.last_error is None for h in health.values())


def test_backfill_is_idempotent(
    db_url: str, pg_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(db_url, capsys)
    again = _run(db_url, capsys)
    assert again["rows"] == {f: 0 for f in again["rows"]}
    assert again["snapshot_rows"] == 0
