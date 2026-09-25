"""Celery application. Tasks and beat schedule arrive in P5."""

import os

from celery import Celery

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

app = Celery("asymmetric_committee", broker=REDIS_URL, backend=REDIS_URL)
app.conf.beat_schedule = {}
