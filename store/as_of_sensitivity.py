"""Sensitivity/debug readers that also see ``backfill`` evidence (P6.4, §4.6).

Backfilled corporate actions, coverage and delistings were first observed long after their events;
they may support an explicitly labelled sensitivity or debug analysis only, never headline metrics,
sequential decisions or forward-test evidence. An import-linter contract forbids decision, scoring
and evaluation code from importing this module; the headline readers live in ``store.as_of``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime

from contracts.corporate_actions import CorporateAction, CorporateActionCoverage, Delisting
from contracts.enums import KnowledgeBasis
from store.as_of import Conn, _action_coverage, _corporate_actions, _delisting_history

_ALL = tuple(b.value for b in KnowledgeBasis)

__all__ = [
    "sensitivity_action_coverage",
    "sensitivity_corporate_actions",
    "sensitivity_delisting_history",
]


def sensitivity_corporate_actions(
    conn: Conn,
    security_ids: Iterable[int],
    as_of: datetime,
    *,
    process_from: date | None = None,
    process_to: date | None = None,
) -> list[CorporateAction]:
    return _corporate_actions(conn, security_ids, as_of, _ALL, process_from, process_to)


def sensitivity_action_coverage(
    conn: Conn, security_id: int, as_of: datetime
) -> list[CorporateActionCoverage]:
    return _action_coverage(conn, security_id, as_of, _ALL)


def sensitivity_delisting_history(conn: Conn, security_id: int, as_of: datetime) -> list[Delisting]:
    return _delisting_history(conn, security_id, as_of, _ALL)
