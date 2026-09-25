"""Market-time helpers. US equity sessions and SEC timestamps are America/New_York."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SESSION_CLOSE = time(16, 0)


def at_et(d: date, t: time) -> datetime:
    return datetime.combine(d, t, ET)


def session_close(d: date) -> datetime:
    """Regular close. Early-close days are treated as 16:00, which only delays availability."""
    return at_et(d, SESSION_CLOSE)


def parse_utc(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"timestamp without zone: {s!r}")
    return dt.astimezone(UTC)


def parse_edgar_acceptance(s: str) -> datetime:
    """EDGAR ``acceptanceDateTime`` (e.g. ``2024-02-02T18:03:26.000Z``).

    The digits match the filing header's ACCEPTANCE-DATETIME, which is Eastern time, despite the
    ``Z``. We read them as Eastern: that is the later of the two readings, so the result can never
    be earlier than the true acceptance (no look-ahead).
    """
    naive = datetime.fromisoformat(s.replace("Z", "").split("+")[0])
    return naive.replace(tzinfo=None, microsecond=0).replace(tzinfo=ET).astimezone(UTC)


def end_of_day_et(d: date) -> datetime:
    return at_et(d, time(23, 59, 59)).astimezone(UTC)
