"""Pure row -> contract mappers for ``commitment_v1`` material, shared by the write and read sides.

One implementation, so the scorer rebuilds exactly what the pipeline hashed. No queries here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from contracts.commitment import MaterialDefectError, MaterialDefectKind
from contracts.enums import BearSeverity, CioAction, Horizon, SizingMode
from contracts.models import (
    AgentWeight,
    CommitteeDecision,
    CommitteeDecisionRecord,
    PortfolioSnapshot,
    RunRecord,
)


def decisions_from_rows(
    run: RunRecord, rows: Iterable[Mapping[str, Any]]
) -> list[CommitteeDecisionRecord]:
    """Stored committee decisions rebuilt as the records that were hashed, or raise.

    A row without ``entity_token`` (written before migration 0006) cannot be rebuilt, so the
    commitment of such a run cannot be verified: that is an integrity failure, not a skip.
    """
    out: list[CommitteeDecisionRecord] = []
    for r in rows:
        if r["entity_token"] is None:
            raise MaterialDefectError(
                MaterialDefectKind.DECISION_ENTITY_TOKEN_MISSING,
                f"run {run.run_id}: decision for security {r['security_id']} has no entity_token",
            )
        try:
            out.append(
                CommitteeDecisionRecord(
                    decision=CommitteeDecision(
                        run_id=run.run_id,
                        security_id=r["security_id"],
                        entity_token=r["entity_token"],
                        as_of=run.as_of,
                        horizon_days=Horizon(r["horizon"]),
                        pooled_p=r["pooled_p"],
                        dispersion=r["dispersion"],
                        agent_weights=tuple(AgentWeight.model_validate(w) for w in r["weights"]),
                        bear_severity=BearSeverity(r["bear_severity"])
                        if r["bear_severity"]
                        else None,
                        target_weight=r["target_weight"],
                    ),
                    pooled_logit=r["pooled_logit"],
                    sizing_mode=SizingMode(r["sizing_mode"]) if r["sizing_mode"] else None,
                    cio_action=CioAction(r["cio_action"]) if r["cio_action"] else None,
                    rationale=r["rationale"],
                )
            )
        except ValueError as exc:
            raise MaterialDefectError(
                MaterialDefectKind.DECISION_ROW_INVALID,
                f"run {run.run_id}: stored decision invalid: {exc}",
            ) from exc
    return out


def snapshot_from_row(row: Mapping[str, Any]) -> PortfolioSnapshot:
    return PortfolioSnapshot.model_validate(dict(row))
