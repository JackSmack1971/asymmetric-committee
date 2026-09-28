"""Celery application (§11). Redis is the transport only; business truth is Postgres and the broker.

Delivery is at-least-once (``acks_late``: a task whose worker dies is redelivered), which is safe
because every task is idempotent and guarded by a Postgres advisory lock. The visibility timeout
is set well above the longest task so Redis does not redeliver a task that is merely still running.
"""

import os
from datetime import timedelta
from typing import Any

from celery import Celery
from celery.schedules import crontab

from config.loader import load_config
from contracts.enums import ReferenceJob

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

app = Celery("asymmetric_committee", broker=REDIS_URL, backend=REDIS_URL)
app.conf.update(
    imports=("orchestration.tasks",),
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    result_expires=3600,  # results are informational; no logic ever reads one
    task_acks_late=True,
    task_reject_on_worker_lost=False,
    worker_prefetch_multiplier=1,
    broker_transport_options={"visibility_timeout": 6 * 3600},
    timezone="America/New_York",
    enable_utc=True,
)

# Schedules are wall-clock hints only. The tasks decide from the broker's trading calendar whether
# they have anything to do (holidays, early closes, week-final sessions) and the sweeper repairs
# whatever a missed or duplicated beat left behind.
app.conf.beat_schedule = {
    # After the close on each weekday; acts only when that close ends a trading week.
    "weekly-pipeline": {
        "task": "orchestration.run_weekly_pipeline",
        "schedule": crontab(minute=15, hour=17, day_of_week="mon-fri"),
    },
    # Rebuilds the queue from Postgres: crash recovery, Redis loss, the Monday execution window.
    "sweep-runs": {
        "task": "orchestration.sweep_runs",
        "schedule": crontab(minute="*/5"),
    },
    # Bitcoin confirmation of pending OpenTimestamps proofs (needed before scoring).
    "upgrade-anchors": {
        "task": "orchestration.upgrade_anchors",
        "schedule": crontab(minute=7),
    },
}


def ingest_schedule() -> dict[str, dict[str, Any]]:
    """One beat entry per feed at its ``pipeline.yaml`` cadence (§4.1); the cadence is validated to
    be shorter than the feed's freshness SLA, so a healthy feed cannot read stale."""
    cadence = load_config(allow_placeholders=True, env={}).pipeline.ingest.cadence_minutes
    return {
        f"ingest-{feed.value}": {
            "task": "orchestration.ingest_feed",
            "schedule": timedelta(minutes=minutes),
            "args": (feed.value,),
        }
        for feed, minutes in cadence.items()
    }


_REFERENCE_TASKS = {
    ReferenceJob.CALENDAR: "orchestration.sync_calendar",
    ReferenceJob.BENCHMARKS: "orchestration.ingest_benchmarks",
    ReferenceJob.DGS3MO: "orchestration.sync_dgs3mo",
    ReferenceJob.REFERENCES: "orchestration.capture_due_references",
    ReferenceJob.HALT_SWEEP: "orchestration.sweep_halt_references",
    ReferenceJob.CORPORATE_ACTIONS: "orchestration.sync_corporate_actions",
    ReferenceJob.LISTING_STATUS: "orchestration.poll_listing_status",
    ReferenceJob.DELISTINGS: "orchestration.derive_delistings",
}


def reference_schedule() -> dict[str, dict[str, Any]]:
    """P6.3 acquisition jobs at their ``reference_data.cadence_minutes``. They are polls, not
    clock-time crons: the reference poll fires when a run's D0 open + delay is due per the stored
    calendar. They are not SLA feeds and cannot trigger a stale-feed halt."""
    cadence = load_config(allow_placeholders=True, env={}).pipeline.reference_data.cadence_minutes
    return {
        f"reference-{job.value}": {
            "task": _REFERENCE_TASKS[job],
            "schedule": timedelta(minutes=minutes),
        }
        for job, minutes in cadence.items()
    }


app.conf.beat_schedule.update(ingest_schedule())
app.conf.beat_schedule.update(reference_schedule())
