"""Anchoring stage (§11, §12.2): ``COMMITTED -> ANCHORED``, and later Bitcoin confirmation.

A run becomes ``ANCHORED`` only when, in this order:

1. its commitment hash is *recomputed* from the stored rows and equals the stored hash (and any
   stored anchor); a mismatch raises ``CommitmentIntegrityError``, writes a dead-letter row and
   changes nothing else;
2. an OpenTimestamps proof from the configured calendars binds that exact digest;
3. a manifest naming the run and its hash is committed and pushed to the configured git remote,
   and the commit is confirmed reachable from the remote branch.

The anchor row and the ``COMMITTED -> ANCHORED`` move are then written in one transaction
(``mark_anchored``); the database also refuses an anchor whose hash is not the run's commitment.
The external side effects come first and are idempotent, so a crash between them and the database
write is repaired by running the stage again. Bitcoin confirmation is *not* awaited here: it is
verified later by ``AnchorUpgrader`` and is required before scoring, not before execution.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from contracts.commitment import CommitmentIntegrityError, CommitmentMaterial, verify_material
from contracts.enums import RunStatus
from contracts.models import CommitmentAnchor, DlqRecord, RunRecord
from evaluation.anchoring import (
    AnchorBindingError,
    HeaderSource,
    anchor_manifest,
    check_binding,
    confirm_bitcoin,
    dumps,
    loads,
    manifest_path,
    upgrade,
)

log = logging.getLogger(__name__)

_ANCHORED_OR_LATER = (RunStatus.ANCHORED, RunStatus.EXECUTED, RunStatus.SCORED)


class AnchorNotEligibleError(RuntimeError):
    """The run cannot be anchored (missing, uncommitted, halted or otherwise not ``COMMITTED``)."""


class AnchorStore(Protocol):
    def load_run(self, run_id: UUID) -> RunRecord | None: ...

    def load_commitment_material(self, run_id: UUID) -> CommitmentMaterial | None: ...

    def mark_anchored(self, anchor: CommitmentAnchor) -> bool: ...

    def record_dlq(self, records: tuple[DlqRecord, ...]) -> None: ...


class Stamper(Protocol):
    def stamp(self, sha256_hex: str) -> bytes: ...

    def fetch(self, uri: str, commitment: bytes) -> bytes | None: ...


class GitAnchors(Protocol):
    def anchor(self, files: dict[str, bytes]) -> dict[str, str]: ...

    def is_reachable(self, commit: str) -> bool: ...


@dataclass(frozen=True)
class AnchorResult:
    run_id: UUID
    status: RunStatus
    advanced: bool  # True only for the call that performed COMMITTED -> ANCHORED
    git_commit: str | None = None


class AnchorStage:
    def __init__(
        self,
        *,
        store: AnchorStore,
        stamper: Stamper,
        git: GitAnchors,
        clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._stamper = stamper
        self._git = git
        self._clock = clock

    def anchor_run(self, run_id: UUID) -> AnchorResult:
        run = self._store.load_run(run_id)
        if run is None:
            raise AnchorNotEligibleError(f"run {run_id} does not exist")
        material = self._verified(run)
        sha = material.stored_sha256
        anchor = material.anchor

        if run.status in _ANCHORED_OR_LATER:  # replay: prove the stored anchor still binds
            if anchor is None or anchor.ots_proof is None or anchor.git_commit is None:
                raise CommitmentIntegrityError(
                    f"run {run_id} is {run.status.value} without a full anchor"
                )
            check_binding(anchor.ots_proof, sha)
            return AnchorResult(run_id, run.status, False, anchor.git_commit)
        if run.status is not RunStatus.COMMITTED:
            raise AnchorNotEligibleError(f"run {run_id} is {run.status.value}, not COMMITTED")

        proof = anchor.ots_proof if anchor is not None and anchor.ots_proof else None
        if proof is not None:
            check_binding(proof, sha)  # a stored proof is reused only if it still binds this hash
        else:
            proof = self._stamper.stamp(sha)
            check_binding(proof, sha)

        manifest = anchor_manifest(run_id, sha, material.committed_at)
        path = manifest_path(run_id)
        commit = self._git.anchor({path: manifest})[path]
        if not self._git.is_reachable(commit):
            raise AnchorBindingError(f"anchor commit {commit[:12]} is not reachable on the remote")

        advanced = self._store.mark_anchored(
            CommitmentAnchor(
                run_id=run_id,
                sha256=sha,
                ots_proof=proof,
                git_commit=commit,
                anchored_at=self._clock(),
            )
        )
        return AnchorResult(run_id, RunStatus.ANCHORED, advanced, commit)

    def _verified(self, run: RunRecord) -> CommitmentMaterial:
        try:
            material = self._store.load_commitment_material(run.run_id)
            if material is None:
                raise AnchorNotEligibleError(f"run {run.run_id} has no decision commitment")
            verify_material(material)
        except CommitmentIntegrityError as exc:
            self._dead_letter(run, exc)
            raise
        return material

    def _dead_letter(self, run: RunRecord, exc: Exception) -> None:
        try:
            self._store.record_dlq(
                (
                    DlqRecord(
                        run_id=run.run_id,
                        as_of=run.as_of,
                        agent="anchor",
                        error_type="commitment_mismatch",
                        payload={"detail": str(exc)[:500]},
                    ),
                )
            )
        except Exception:  # the integrity error must reach the caller even if this write fails
            log.exception("could not record the commitment mismatch for %s", run.run_id)


class AnchorConfirmStore(Protocol):
    def anchors_awaiting_confirmation(self) -> list[CommitmentAnchor]: ...

    def record_anchor(self, anchor: CommitmentAnchor) -> None: ...


@dataclass(frozen=True)
class UpgradeSummary:
    checked: int = 0
    upgraded: int = 0
    confirmed: int = 0
    failed: int = 0


class AnchorUpgrader:
    """Fetch completed calendar timestamps and verify Bitcoin attestations against block headers.

    ``verified_at`` is set only after a Bitcoin attestation verified against a real header; a
    proof that merely gained an attestation is stored but stays unconfirmed.
    """

    def __init__(
        self,
        *,
        store: AnchorConfirmStore,
        stamper: Stamper,
        headers: HeaderSource | None,
        clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._stamper = stamper
        self._headers = headers
        self._clock = clock

    def upgrade_all(self) -> UpgradeSummary:
        checked = upgraded = confirmed = failed = 0
        for anchor in self._store.anchors_awaiting_confirmation():
            checked += 1
            if anchor.ots_proof is None:
                continue
            try:
                file = loads(anchor.ots_proof)
                changed = upgrade(file, self._stamper.fetch)
                proof = dumps(file) if changed else anchor.ots_proof
                status = check_binding(proof, anchor.sha256)
                verified_at: datetime | None = None
                if status.confirmed and self._headers is not None:
                    confirm_bitcoin(proof, anchor.sha256, self._headers)
                    verified_at = self._clock()
            except AnchorBindingError:
                log.exception("anchor for run %s failed verification", anchor.run_id)
                failed += 1
                continue
            if changed or verified_at is not None:
                upgraded += 1 if changed else 0
                confirmed += 1 if verified_at is not None else 0
                self._store.record_anchor(
                    CommitmentAnchor(
                        run_id=anchor.run_id,
                        sha256=anchor.sha256,
                        ots_proof=proof,
                        anchored_at=anchor.anchored_at,
                        verified_at=verified_at,
                    )
                )
        return UpgradeSummary(checked, upgraded, confirmed, failed)
