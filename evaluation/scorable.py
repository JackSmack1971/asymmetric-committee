"""The scorability gate (§12.2, invariant 5): admission happens before any outcome is read.

Pure and read-only. Depends on ``contracts`` and ``evaluation.anchoring`` only; the database read
and the git/header probes are injected. Admission never writes, never loads a price or outcome,
and never trusts a stored verdict: the commitment is recomputed from the stored rows, the
OpenTimestamps proof is re-bound to that digest and re-verified against Bitcoin headers, and git
reachability is re-checked, every time. ``anchor.verified_at`` is never read and ``anchored_at``
is process evidence only.

Boundary. ``admit_run`` returns a ``ScoringTicket``: an opaque, frozen record of what was
admitted and when. It is a sequencing token, not a security mechanism: Python cannot stop code
from constructing or copying one. The enforced boundary is that ``run_scoring`` admits *before* it
calls the outcome loader (a refusal raises and the loader is never invoked) and that sanctioned
loaders take a ticket (``OutcomeLoader``).

Refusal precedence (first failing check wins, so one damaged run has one stable reason):
``RUN_MISSING`` > ``NO_COMMITMENT`` > ``NOT_SCORABLE_STATUS`` / ``HALT_EVIDENCE_MISSING`` >
``MATERIAL_INCOMPLETE`` > ``COMMITMENT_MISMATCH`` > ``ANCHOR_MISSING`` > ``ANCHOR_INVALID`` >
``OTS_NOT_CONFIRMED`` > ``HEADER_VERIFICATION_FAILED`` > ``GIT_UNREACHABLE`` > ``ANCHOR_TIMING``.
``EVIDENCE_UNAVAILABLE`` (retryable) replaces a check whose evidence could not be fetched.

Completeness. COMPLETE: BACKTEST/ABLATION ``ANCHORED``/``SCORED`` or LIVE ``EXECUTED``/``SCORED``.
HALTED: ``PARTIAL`` with the ``kill_switch:`` reason *and* a durable kill-switch event, plus every
check above; it never becomes ``SCORED``. Everything else is MISSING and refused (never scored as
a zero-return week).

Timing. All modes: ``committed_at <= anchored_at <= requested_at`` and the verified Bitcoin block
time ``<= requested_at``, where ``requested_at`` is the injected clock read inside ``admit_run``
(callers cannot choose it). LIVE additionally: the block time must be strictly before the
earliest outcome resolution requested, which the caller must state. BACKTEST/ABLATION market dates
are historical and are never compared with the anchor.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from contracts.commitment import (
    CommitmentIntegrityError,
    ScoringEvidence,
    verify_commitment,
)
from contracts.enums import HALT_REASON_PREFIX, RunMode, RunStatus
from evaluation.anchoring import (
    AnchorBindingError,
    HeaderSource,
    check_binding,
    confirm_bitcoin,
)


class Completeness(StrEnum):
    COMPLETE = "COMPLETE"
    HALTED = "HALTED"
    MISSING = "MISSING"


class RefusalReason(StrEnum):
    RUN_MISSING = "run_missing"
    NO_COMMITMENT = "no_commitment"
    NOT_SCORABLE_STATUS = "not_scorable_status"
    HALT_EVIDENCE_MISSING = "halt_evidence_missing"
    MATERIAL_INCOMPLETE = "material_incomplete"
    COMMITMENT_MISMATCH = "commitment_mismatch"
    ANCHOR_MISSING = "anchor_missing"
    ANCHOR_INVALID = "anchor_invalid"
    OTS_NOT_CONFIRMED = "ots_not_confirmed"
    HEADER_VERIFICATION_FAILED = "header_verification_failed"
    GIT_UNREACHABLE = "git_unreachable"
    ANCHOR_TIMING = "anchor_timing"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"


class ScoringRefusedError(Exception):
    """The run may not be scored. ``retryable`` is True only when evidence could not be fetched."""

    def __init__(
        self,
        run_id: UUID,
        reason: RefusalReason,
        detail: str,
        *,
        completeness: Completeness | None = None,
    ) -> None:
        super().__init__(f"run {run_id}: {reason.value}: {detail}")
        self.run_id = run_id
        self.reason = reason
        self.detail = detail
        self.completeness = completeness

    @property
    def retryable(self) -> bool:
        return self.reason is RefusalReason.EVIDENCE_UNAVAILABLE


class ScoringEvidenceSource(Protocol):
    """One consistent read of a run's scoring evidence (the store adapter owns the snapshot)."""

    def load(self, run_id: UUID) -> ScoringEvidence: ...


class GitReachability(Protocol):
    """True/False when the remote answered; raise if it could not be asked."""

    def is_reachable(self, commit: str) -> bool: ...


@dataclass(frozen=True)
class ScoringRequest:
    """What the caller wants scored. ``earliest_resolution_at`` is mandatory for LIVE runs."""

    run_id: UUID
    earliest_resolution_at: datetime | None = None


@dataclass(frozen=True)
class ScoringTicket:
    """Record of an admission: sequencing token required by sanctioned outcome loaders."""

    run_id: UUID
    mode: RunMode
    completeness: Completeness
    commitment_sha256: str
    git_commit: str
    bitcoin_height: int
    bitcoin_block_time: datetime
    run_as_of: datetime
    requested_at: datetime  # the clock reading at admission; scored_at must not precede it

    @property
    def may_become_scored(self) -> bool:
        """Only COMPLETE runs may move to ``SCORED``; HALTED stays ``PARTIAL`` forever."""
        return self.completeness is Completeness.COMPLETE


type OutcomeLoader[T] = Callable[[ScoringTicket], T]

_COMPLETE_STATUSES: dict[RunMode, tuple[RunStatus, ...]] = {
    RunMode.LIVE: (RunStatus.EXECUTED, RunStatus.SCORED),
    RunMode.BACKTEST: (RunStatus.ANCHORED, RunStatus.SCORED),
    RunMode.ABLATION: (RunStatus.ANCHORED, RunStatus.SCORED),
}


def _classify(evidence: ScoringEvidence, run_id: UUID) -> Completeness:
    run = evidence.run
    assert run is not None
    if run.status in _COMPLETE_STATUSES[run.mode]:
        return Completeness.COMPLETE
    if (
        run.status is RunStatus.PARTIAL
        and run.status_reason is not None
        and run.status_reason.startswith(HALT_REASON_PREFIX)
    ):
        if not evidence.kill_switch_events:
            raise ScoringRefusedError(
                run_id,
                RefusalReason.HALT_EVIDENCE_MISSING,
                "PARTIAL with a kill-switch reason but no durable kill-switch event",
                completeness=Completeness.MISSING,
            )
        return Completeness.HALTED
    raise ScoringRefusedError(
        run_id,
        RefusalReason.NOT_SCORABLE_STATUS,
        f"{run.mode.value} run in status {run.status.value} is not scorable",
        completeness=Completeness.MISSING,
    )


def _utc(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def admit_run(
    request: ScoringRequest,
    *,
    source: ScoringEvidenceSource,
    headers: HeaderSource,
    git: GitReachability,
    clock: Callable[[], datetime],
) -> ScoringTicket:
    """Admit a run for scoring or raise ``ScoringRefusedError``. Reads only; writes nothing."""
    run_id = request.run_id
    try:
        evidence = source.load(run_id)
    except Exception as exc:  # the read itself failed: nothing is known, so nothing is admitted
        raise ScoringRefusedError(
            run_id, RefusalReason.EVIDENCE_UNAVAILABLE, f"evidence could not be read: {exc}"
        ) from exc

    run = evidence.run
    if run is None:
        raise ScoringRefusedError(
            run_id, RefusalReason.RUN_MISSING, "no such run", completeness=Completeness.MISSING
        )
    commitment = evidence.commitment
    if commitment is None:
        raise ScoringRefusedError(
            run_id,
            RefusalReason.NO_COMMITMENT,
            "no decision commitment row",
            completeness=Completeness.MISSING,
        )
    completeness = _classify(evidence, run_id)

    if evidence.defects or evidence.snapshot is None:
        detail = "; ".join(f"{d.kind.value}: {d.detail}" for d in evidence.defects) or (
            "no stored book"
        )
        raise ScoringRefusedError(run_id, RefusalReason.MATERIAL_INCOMPLETE, detail)
    try:  # the single hash authority: recompute from the stored rows, never trust the digest
        digest = verify_commitment(commitment.sha256, run, evidence.decisions, evidence.snapshot)
    except CommitmentIntegrityError as exc:
        raise ScoringRefusedError(run_id, RefusalReason.COMMITMENT_MISMATCH, str(exc)) from exc

    anchor = evidence.anchor
    if anchor is None or anchor.ots_proof is None or anchor.git_commit is None:
        raise ScoringRefusedError(
            run_id, RefusalReason.ANCHOR_MISSING, "no complete anchor (proof and git commit)"
        )
    if anchor.sha256 != digest:
        raise ScoringRefusedError(
            run_id,
            RefusalReason.ANCHOR_INVALID,
            f"anchor digest {anchor.sha256[:12]} != recomputed {digest[:12]}",
        )

    try:
        status = check_binding(anchor.ots_proof, digest)
    except AnchorBindingError as exc:
        raise ScoringRefusedError(run_id, RefusalReason.ANCHOR_INVALID, str(exc)) from exc
    if not status.confirmed:
        raise ScoringRefusedError(
            run_id, RefusalReason.OTS_NOT_CONFIRMED, "proof has no Bitcoin attestation yet"
        )
    try:  # full verification on every admission; the check above only classifies the failure
        confirmation = confirm_bitcoin(anchor.ots_proof, digest, headers)
    except AnchorBindingError as exc:
        raise ScoringRefusedError(
            run_id, RefusalReason.HEADER_VERIFICATION_FAILED, str(exc)
        ) from exc
    except Exception as exc:  # header source unreachable: not evidence of invalidity
        raise ScoringRefusedError(
            run_id, RefusalReason.EVIDENCE_UNAVAILABLE, f"block headers unavailable: {exc}"
        ) from exc

    try:
        reachable = git.is_reachable(anchor.git_commit)
    except Exception as exc:
        raise ScoringRefusedError(
            run_id, RefusalReason.EVIDENCE_UNAVAILABLE, f"git remote unavailable: {exc}"
        ) from exc
    if not reachable:
        raise ScoringRefusedError(
            run_id,
            RefusalReason.GIT_UNREACHABLE,
            f"anchor commit {anchor.git_commit[:12]} is not reachable on the remote",
        )

    requested_at = clock()
    if requested_at.tzinfo is None:
        raise ValueError("clock must return an aware datetime")
    block_time = _utc(confirmation.block_time)
    if not commitment.committed_at <= anchor.anchored_at <= requested_at:
        raise ScoringRefusedError(
            run_id,
            RefusalReason.ANCHOR_TIMING,
            "need committed_at <= anchored_at <= admission time",
        )
    if block_time > requested_at:
        raise ScoringRefusedError(
            run_id, RefusalReason.ANCHOR_TIMING, "Bitcoin block time is after admission time"
        )
    if run.mode is RunMode.LIVE:
        earliest = request.earliest_resolution_at
        if earliest is None or earliest.tzinfo is None:
            raise ScoringRefusedError(
                run_id,
                RefusalReason.ANCHOR_TIMING,
                "a LIVE run needs an aware earliest_resolution_at",
            )
        if not block_time < earliest:
            raise ScoringRefusedError(
                run_id,
                RefusalReason.ANCHOR_TIMING,
                "Bitcoin block time is not before the earliest outcome resolution",
            )

    return ScoringTicket(
        run_id=run_id,
        mode=run.mode,
        completeness=completeness,
        commitment_sha256=digest,
        git_commit=anchor.git_commit,
        bitcoin_height=confirmation.height,
        bitcoin_block_time=block_time,
        run_as_of=run.as_of,
        requested_at=requested_at,
    )


def run_scoring[T](
    request: ScoringRequest,
    *,
    source: ScoringEvidenceSource,
    headers: HeaderSource,
    git: GitReachability,
    clock: Callable[[], datetime],
    load_outcomes: OutcomeLoader[T],
) -> T:
    """Admit first, then hand the ticket to the outcome loader. A refusal never reaches it."""
    ticket = admit_run(request, source=source, headers=headers, git=git, clock=clock)
    return load_outcomes(ticket)
