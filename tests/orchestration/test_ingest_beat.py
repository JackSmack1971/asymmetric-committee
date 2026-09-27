"""Ingestion on the Celery beat (§11): schedule, eager task delivery, and the stale-feed halt.

The chain under test is beat -> ``orchestration.ingest_feed`` -> ``feed_health`` -> the kill
switch's stale-feed trigger, over real Postgres. A failed poll must leave the feed looking stale.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Engine

from config.loader import load_config
from contracts.enums import FeedName, KillTrigger
from ingest.scheduled import FeedRunResult, IngestFailedError
from orchestration import tasks
from orchestration.celery_app import app
from orchestration.providers import store_feeds
from risk.kill_switch import evaluate
from tests.ingest.test_scheduled import FakeNews, engine, ingestor  # noqa: F401
from tests.orchestration.test_tasks import NOW, make_runtime

CFG = load_config(allow_placeholders=True, env={}).pipeline


def test_every_sla_feed_is_on_the_beat_at_its_configured_cadence() -> None:
    entries = {
        FeedName(v["args"][0]): v
        for v in app.conf.beat_schedule.values()
        if v["task"] == "orchestration.ingest_feed"
    }
    assert set(entries) == set(CFG.freshness_sla_hours)
    for feed, entry in entries.items():
        assert entry["schedule"] == timedelta(minutes=CFG.ingest.cadence_minutes[feed])
        assert entry["schedule"] < timedelta(
            hours=CFG.freshness_sla_hours[feed]
        )  # SLA is reachable
    assert entries[FeedName.NEWS]["schedule"] == timedelta(minutes=15)  # §4.1


def test_the_task_is_registered_and_fails_closed_without_an_ingest_runtime() -> None:
    assert "orchestration.ingest_feed" in app.tasks
    rt, *_ = make_runtime()
    with pytest.raises(RuntimeError, match="no ingestion runtime"):
        tasks.ingest_feed(rt, FeedName.NEWS)


def _rt(eng: Engine, **kw: Any) -> tasks.Runtime:
    rt, *_ = make_runtime()
    rt.ingest = ingestor(eng, lookback=timedelta(days=900), **kw)
    return rt


def test_eager_delivery_ingests_and_a_redelivery_is_a_no_op(engine: Engine) -> None:  # noqa: F811
    tasks.set_runtime(_rt(engine))
    try:
        first = tasks.ingest_feed_task.apply(args=["price_bars"])
        again = tasks.ingest_feed_task.apply(args=["price_bars"])  # duplicate delivery
    finally:
        tasks.set_runtime(None)
    assert first.result == {"feed": "price_bars", "rows": 2}
    assert again.result == {"feed": "price_bars", "rows": 0}
    assert store_feeds(engine)()[FeedName.PRICE_BARS] == NOW


def test_a_failing_task_surfaces_the_failure_and_freshness_stays_unset(engine: Engine) -> None:  # noqa: F811
    tasks.set_runtime(_rt(engine, news=FakeNews(fail=True)))
    try:
        res = tasks.ingest_feed_task.apply(args=["news"])
    finally:
        tasks.set_runtime(None)
    # retried up to the cap, then the failure surfaces (eager mode re-wraps the exception)
    assert isinstance(res.result, Exception) and "timed out" in str(res.result)
    assert store_feeds(engine)().get(FeedName.NEWS) is None


def test_the_beat_keeps_the_kill_switch_quiet_and_a_failing_feed_trips_it(
    engine: Engine,  # noqa: F811
) -> None:
    rt = _rt(engine)
    for feed in CFG.freshness_sla_hours:
        tasks.ingest_feed(rt, feed)

    def trigger(at_offset: timedelta) -> KillTrigger | None:
        return evaluate(
            equity=100.0,
            prior_close_equity=100.0,
            limit=0.05,
            last_success=store_feeds(engine)(),
            sla_hours=CFG.freshness_sla_hours,
            now=NOW + at_offset,
        )

    assert trigger(timedelta(minutes=10)) is None  # everything just ingested

    # News keeps failing: past its 1h SLA the feed is stale and the halt fires, even though the
    # other feeds are healthy and the failed polls kept writing feed_health rows.
    rt_bad = _rt(engine, news=FakeNews(fail=True))
    with pytest.raises(IngestFailedError):
        tasks.ingest_feed(rt_bad, FeedName.NEWS, now=NOW + timedelta(minutes=30))
    assert trigger(timedelta(minutes=45)) is None  # still inside the SLA
    with pytest.raises(IngestFailedError):
        tasks.ingest_feed(rt_bad, FeedName.NEWS, now=NOW + timedelta(minutes=75))
    assert trigger(timedelta(minutes=75)) is KillTrigger.STALE_FEED

    # Recovery: a good poll makes it fresh again.
    assert isinstance(tasks.ingest_feed(rt, FeedName.NEWS, now=NOW + timedelta(minutes=80)), dict)
    assert trigger(timedelta(minutes=85)) is None


def test_a_result_without_success_is_never_reported_as_ok() -> None:
    class Failing:
        def run_feed(self, feed: FeedName, *, now: Any) -> FeedRunResult:
            return FeedRunResult(feed, False, 0, ("x",))

    rt, *_ = make_runtime()
    rt.ingest = Failing()
    with pytest.raises(IngestFailedError):
        tasks.ingest_feed(rt, FeedName.PRICE_BARS)
