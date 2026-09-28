from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from contracts.corporate_actions import ALL_ACTION_TYPES, CorporateActionCoverage, actions_sha256
from contracts.enums import KnowledgeBasis


def test_action_coverage_cannot_claim_full_history_without_provider_bound() -> None:
    digest = actions_sha256(())
    with pytest.raises(ValueError, match="provider lower bound"):
        CorporateActionCoverage(
            security_id=1,
            range_start=date(2020, 1, 1),
            range_end=date(2024, 1, 1),
            established_at=datetime(2024, 1, 2, tzinfo=UTC),
            symbols=("AAA",),
            action_types=ALL_ACTION_TYPES,
            page_count=1,
            pagination_exhausted=True,
            full_history=True,
            action_count=0,
            actions_sha256=digest,
            knowledge_basis=KnowledgeBasis.PROSPECTIVE,
            source_version="coverage-v1",
        )
