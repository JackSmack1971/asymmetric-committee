"""The one definition of a run's decision commitment hash (§12.2, invariant 5).

``commitment_v1`` is the sha256 of a canonical JSON document holding the run identity, every
committee decision (sorted by security and horizon) and the final portfolio snapshot (book, cash
and CIO decision). The pipeline computes it when it commits; execution, anchoring and the
evaluator recompute it from the rows read back from the store and compare. A stored hash alone is
never trusted.

Canonical form: object keys sorted, compact separators, ASCII only, no NaN/Infinity, every aware
datetime rendered in UTC with microseconds (so a Postgres round trip through another time zone
cannot change the bytes), enums as their values.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Any
from uuid import UUID

from pydantic import AwareDatetime

from contracts.models import (
    CommitmentAnchor,
    CommitteeDecisionRecord,
    Contract,
    DecisionCommitment,
    KillSwitchEvent,
    PortfolioSnapshot,
    RunRecord,
    Sha256Hex,
)

COMMITMENT_VERSION = "commitment_v1"


class CommitmentIntegrityError(RuntimeError):
    """A recomputed commitment does not match what was stored or anchored. Never auto-retried."""


class MaterialDefectKind(StrEnum):
    """Why stored rows could not be rebuilt into the committed material (kept, never collapsed)."""

    SNAPSHOT_MISSING = "snapshot_missing"
    SNAPSHOT_INVALID = "snapshot_invalid"
    DECISION_ENTITY_TOKEN_MISSING = "decision_entity_token_missing"
    DECISION_ROW_INVALID = "decision_row_invalid"


class MaterialDefectError(CommitmentIntegrityError):
    """A stored row cannot be rebuilt into ``commitment_v1`` material; ``kind`` says why."""

    def __init__(self, kind: MaterialDefectKind, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


class MaterialDefect(Contract):
    kind: MaterialDefectKind
    detail: str


class CommitmentMaterial(Contract):
    """Everything read back from the store to recompute a run's commitment."""

    run: RunRecord
    decisions: tuple[CommitteeDecisionRecord, ...]
    snapshot: PortfolioSnapshot
    stored_sha256: Sha256Hex
    committed_at: AwareDatetime
    anchor: CommitmentAnchor | None = None


def _canon(value: Any) -> Any:
    if isinstance(value, Enum):
        return _canon(value.value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetime in a commitment")
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float in a commitment")
        return value
    if isinstance(value, Mapping):
        return {str(k): _canon(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    raise TypeError(f"cannot canonicalise {type(value).__name__}")


def commitment_payload(
    run: RunRecord, decisions: Iterable[CommitteeDecisionRecord], snapshot: PortfolioSnapshot
) -> dict[str, Any]:
    """The canonical document. Depends only on the run's identity, never on its status or times."""
    if snapshot.run_id != run.run_id:
        raise ValueError("snapshot belongs to a different run")
    ordered = sorted(
        decisions, key=lambda d: (d.decision.security_id, int(d.decision.horizon_days))
    )
    if any(d.decision.run_id != run.run_id for d in ordered):
        raise ValueError("decision belongs to a different run")
    return {
        "v": COMMITMENT_VERSION,
        "run": {
            "run_id": run.run_id,
            "mode": run.mode,
            "as_of": run.as_of,
            "config_hash": run.config_hash,
        },
        "decisions": [_canon(d.model_dump(mode="python")) for d in ordered],
        "snapshot": _canon(snapshot.model_dump(mode="python")),
    }


def canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        _canon(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def commitment_hash(
    run: RunRecord, decisions: Iterable[CommitteeDecisionRecord], snapshot: PortfolioSnapshot
) -> str:
    return hashlib.sha256(canonical_bytes(commitment_payload(run, decisions, snapshot))).hexdigest()


def verify_commitment(
    stored_sha256: str,
    run: RunRecord,
    decisions: Iterable[CommitteeDecisionRecord],
    snapshot: PortfolioSnapshot,
) -> str:
    """Recompute from the rows and compare; return the hash or raise on any difference."""
    try:
        recomputed = commitment_hash(run, decisions, snapshot)
    except (ValueError, TypeError) as exc:
        raise CommitmentIntegrityError(
            f"run {run.run_id}: commitment cannot be recomputed: {exc}"
        ) from exc
    if recomputed != stored_sha256:
        raise CommitmentIntegrityError(
            f"run {run.run_id}: recomputed {recomputed[:12]} != stored {stored_sha256[:12]}"
        )
    return recomputed


def verify_material(material: CommitmentMaterial) -> str:
    """Recompute from stored rows; the stored hash and any anchor must both equal the result."""
    recomputed = verify_commitment(
        material.stored_sha256, material.run, material.decisions, material.snapshot
    )
    if material.anchor is not None and material.anchor.sha256 != recomputed:
        raise CommitmentIntegrityError(
            f"run {material.run.run_id}: anchor {material.anchor.sha256[:12]} != recomputed "
            f"{recomputed[:12]}"
        )
    return recomputed


class ScoringEvidence(Contract):
    """Everything the scorability gate needs, read in one snapshot; nothing here is trusted.

    Absent pieces stay absent (``None``/empty) and rows that could not be rebuilt are listed in
    ``defects`` with their kind, so refusal classification never depends on which query failed.
    ``anchor.verified_at`` is carried as evidence only.
    """

    run: RunRecord | None = None
    commitment: DecisionCommitment | None = None
    decisions: tuple[CommitteeDecisionRecord, ...] = ()
    snapshot: PortfolioSnapshot | None = None
    anchor: CommitmentAnchor | None = None
    kill_switch_events: tuple[KillSwitchEvent, ...] = ()
    defects: tuple[MaterialDefect, ...] = ()
