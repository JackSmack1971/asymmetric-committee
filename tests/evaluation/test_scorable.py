"""P6.2 scorability gate: every refusal happens before an outcome loader is invoked (§12.2)."""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import inspect
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from contracts.commitment import (
    MaterialDefect,
    MaterialDefectKind,
    ScoringEvidence,
    commitment_hash,
)
from contracts.enums import HALT_REASON_PREFIX, RunMode, RunStatus
from contracts.models import CommitmentAnchor, DecisionCommitment, RunRecord
from evaluation import scorable as s
from tests.orchestration.anchoring_support import (
    GIT_COMMIT,
    BlockHeaders,
    confirmed_proof,
    pending_proof,
)
from tests.orchestration.test_sink import AS_OF, step

R = s.RefusalReason
RUN = uuid4()
COMMITTED_AT = AS_OF
ANCHORED_AT = AS_OF + timedelta(hours=1)
BLOCK_TIME = AS_OF + timedelta(hours=2)
NOW = AS_OF + timedelta(days=400)  # a retrospective admission long after the historical dates
LIVE_EARLIEST = AS_OF + timedelta(days=5)
HALT = f"{HALT_REASON_PREFIX}daily_loss"
NO_ROOT = b"\x00" * 32
OTHER_ROOT = b"\x11" * 32


def _ts(dt: datetime) -> int:
    return int(dt.timestamp())


class Source:
    def __init__(self, evidence: ScoringEvidence | Exception) -> None:
        self.evidence = evidence
        self.loads = 0

    def load(self, run_id: UUID) -> ScoringEvidence:
        self.loads += 1
        if isinstance(self.evidence, Exception):
            raise self.evidence
        return self.evidence


class Git:
    def __init__(self, result: bool | Exception = True) -> None:
        self.result = result
        self.asked: list[str] = []

    def is_reachable(self, commit: str) -> bool:
        self.asked.append(commit)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class RaisingHeaders:
    def header(self, height: int) -> object:
        raise ConnectionError("no header source")


class Loader:
    """The outcome loader: any call to it is a scoring read."""

    def __init__(self) -> None:
        self.calls: list[s.ScoringTicket] = []

    def __call__(self, ticket: s.ScoringTicket) -> str:
        self.calls.append(ticket)
        return "outcomes"


def evidence(
    *,
    mode: RunMode = RunMode.BACKTEST,
    status: RunStatus = RunStatus.ANCHORED,
    reason: str | None = None,
    events: bool = False,
    proof: str = "confirmed",  # confirmed | pending | other | none
    stored_sha: str | None = None,
    anchor_sha: str | None = None,
    anchored_at: datetime = ANCHORED_AT,
    committed_at: datetime = COMMITTED_AT,
    verified_at: datetime | None = None,
    git_commit: str | None = GIT_COMMIT,
) -> tuple[ScoringEvidence, bytes]:
    run = RunRecord(
        run_id=RUN,
        mode=mode,
        as_of=AS_OF,
        config_hash="c" * 64,
        status=status,
        started_at=AS_OF,
        status_reason=reason,
    )
    art = step(RUN, 1, run=run)
    assert art.portfolio is not None
    digest = commitment_hash(run, art.decisions, art.portfolio)
    root = NO_ROOT
    anchor = None
    if proof != "none":
        if proof == "confirmed":
            blob, root = confirmed_proof(digest)
        elif proof == "pending":
            blob = pending_proof(digest)
        else:
            blob = pending_proof(hashlib.sha256(b"another").hexdigest())
        anchor = CommitmentAnchor(
            run_id=RUN,
            sha256=anchor_sha or digest,
            ots_proof=blob,
            git_commit=git_commit,
            anchored_at=anchored_at,
            verified_at=verified_at,
        )
    return (
        ScoringEvidence(
            run=run,
            commitment=DecisionCommitment(
                run_id=RUN, sha256=stored_sha or digest, committed_at=committed_at
            ),
            decisions=art.decisions,
            snapshot=art.portfolio,
            anchor=anchor,
            kill_switch_events=art.kill_switch if events else (),
        ),
        root,
    )


def admit(
    ev: ScoringEvidence,
    root: bytes,
    *,
    git: Git | None = None,
    earliest: datetime | None = None,
    block_time: datetime = BLOCK_TIME,
    now: datetime = NOW,
) -> s.ScoringTicket:
    return s.admit_run(
        s.ScoringRequest(RUN, earliest),
        source=Source(ev),
        headers=BlockHeaders(root, _ts(block_time)),
        git=git or Git(),
        clock=lambda: now,
    )


def refused(
    ev: ScoringEvidence | Exception,
    root: bytes = NO_ROOT,
    *,
    git: Git | None = None,
    headers: object | None = None,
    earliest: datetime | None = None,
    block_time: datetime = BLOCK_TIME,
    now: datetime = NOW,
) -> s.ScoringRefusedError:
    """Run through ``run_scoring`` and prove the outcome loader was never invoked."""
    loader = Loader()
    with pytest.raises(s.ScoringRefusedError) as info:
        s.run_scoring(
            s.ScoringRequest(RUN, earliest),
            source=Source(ev),
            headers=headers or BlockHeaders(root, _ts(block_time)),  # type: ignore[arg-type]
            git=git or Git(),
            clock=lambda: now,
            load_outcomes=loader,
        )
    assert loader.calls == []  # zero scoring access on every refusal
    return info.value


# --- refusals: missing evidence -----------------------------------------------------------------


def test_missing_run_and_missing_commitment_are_distinct() -> None:
    assert refused(ScoringEvidence()).reason is R.RUN_MISSING
    ev, root = evidence()
    err = refused(ev.model_copy(update={"commitment": None}), root)
    assert err.reason is R.NO_COMMITMENT and err.completeness is s.Completeness.MISSING


def test_an_unreadable_source_is_retryable_and_fails_closed() -> None:
    err = refused(RuntimeError("db down"))
    assert err.reason is R.EVIDENCE_UNAVAILABLE and err.retryable


@pytest.mark.parametrize("what", ["decision", "book", "cio"])
def test_tampering_with_committed_material_is_refused_although_the_stored_digest_matches(
    what: str,
) -> None:
    ev, root = evidence()
    snap = ev.snapshot
    assert snap is not None
    if what == "decision":
        d = ev.decisions[0]
        bumped = d.model_copy(
            update={"decision": d.decision.model_copy(update={"target_weight": 0.07})}
        )
        ev = ev.model_copy(update={"decisions": (bumped, *ev.decisions[1:])})
    elif what == "book":
        pos = snap.book.positions[0].model_copy(update={"target_weight": 0.06})
        book = snap.book.model_copy(update={"positions": (pos,)})
        ev = ev.model_copy(update={"snapshot": snap.model_copy(update={"book": book})})
    else:
        assert snap.cio is not None
        cio = snap.cio.model_copy(update={"rationale": "edited after the fact"})
        ev = ev.model_copy(update={"snapshot": snap.model_copy(update={"cio": cio})})
    assert refused(ev, root).reason is R.COMMITMENT_MISMATCH


def test_a_wrong_stored_digest_is_refused() -> None:
    ev, root = evidence(stored_sha="f" * 64)
    assert refused(ev, root).reason is R.COMMITMENT_MISMATCH


@pytest.mark.parametrize("kind", list(MaterialDefectKind))
def test_material_defects_keep_their_reason(kind: MaterialDefectKind) -> None:
    ev, root = evidence()
    ev = ev.model_copy(update={"defects": (MaterialDefect(kind=kind, detail="row 7"),)})
    err = refused(ev, root)
    assert err.reason is R.MATERIAL_INCOMPLETE and kind.value in err.detail


def test_a_missing_book_is_material_incomplete() -> None:
    ev, root = evidence()
    assert refused(ev.model_copy(update={"snapshot": None}), root).reason is R.MATERIAL_INCOMPLETE


# --- refusals: anchor and Bitcoin ---------------------------------------------------------------


def test_no_anchor_or_an_incomplete_one_is_refused() -> None:
    ev, root = evidence(proof="none")
    assert refused(ev, root).reason is R.ANCHOR_MISSING
    ev, root = evidence(git_commit=None)
    assert refused(ev, root).reason is R.ANCHOR_MISSING


def test_an_anchor_digest_that_differs_from_the_recomputed_hash_is_refused() -> None:
    ev, root = evidence(anchor_sha="e" * 64)
    assert refused(ev, root).reason is R.ANCHOR_INVALID


def test_a_proof_for_another_digest_is_refused() -> None:
    ev, root = evidence(proof="other")
    assert refused(ev, root).reason is R.ANCHOR_INVALID


def test_a_pending_only_proof_is_not_bitcoin_confirmed() -> None:
    ev, root = evidence(proof="pending")
    err = refused(ev, root)
    assert err.reason is R.OTS_NOT_CONFIRMED and not err.retryable


def test_bad_bitcoin_header_evidence_is_refused() -> None:
    ev, _ = evidence()
    err = refused(ev, OTHER_ROOT)  # a header whose merkle root is not the proof's
    assert err.reason is R.HEADER_VERIFICATION_FAILED and not err.retryable


def test_an_unreachable_header_source_is_retryable_not_invalid() -> None:
    ev, _ = evidence()
    err = refused(ev, headers=RaisingHeaders())
    assert err.reason is R.EVIDENCE_UNAVAILABLE and err.retryable


def test_verified_at_is_not_authority() -> None:
    stamp = ANCHORED_AT + timedelta(hours=3)
    ev, root = evidence(proof="pending", verified_at=stamp)
    assert refused(ev, root).reason is R.OTS_NOT_CONFIRMED
    ev, _ = evidence(verified_at=stamp)
    assert refused(ev, OTHER_ROOT).reason is R.HEADER_VERIFICATION_FAILED
    ev, root = evidence(verified_at=None)  # never stamped, yet a fully valid proof is admitted
    assert admit(ev, root).completeness is s.Completeness.COMPLETE


def test_git_unreachable_is_definitive_and_a_probe_failure_is_retryable() -> None:
    ev, root = evidence()
    git = Git(False)
    err = refused(ev, root, git=git)
    assert err.reason is R.GIT_UNREACHABLE and not err.retryable
    assert git.asked == [GIT_COMMIT]
    err = refused(ev, root, git=Git(TimeoutError("remote timed out")))
    assert err.reason is R.EVIDENCE_UNAVAILABLE and err.retryable


# --- status and completeness --------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        RunStatus.PENDING,
        RunStatus.INGEST_OK,
        RunStatus.FEATURES_OK,
        RunStatus.GATED,
        RunStatus.AGENTS_OK,
        RunStatus.COMMITTED,
        RunStatus.FAILED,
        RunStatus.PARTIAL,
    ],
)
def test_non_terminal_failed_and_ordinary_partial_runs_are_missing(status: RunStatus) -> None:
    ev, root = evidence(status=status)
    err = refused(ev, root)
    assert err.reason is R.NOT_SCORABLE_STATUS and err.completeness is s.Completeness.MISSING


def test_live_anchored_only_and_backtest_executed_are_not_scorable() -> None:
    ev, root = evidence(mode=RunMode.LIVE, status=RunStatus.ANCHORED)
    assert refused(ev, root, earliest=LIVE_EARLIEST).reason is R.NOT_SCORABLE_STATUS
    ev, root = evidence(mode=RunMode.BACKTEST, status=RunStatus.EXECUTED)
    assert refused(ev, root).reason is R.NOT_SCORABLE_STATUS


def test_partial_needs_the_halt_reason_and_a_durable_event() -> None:
    ev, root = evidence(status=RunStatus.PARTIAL, reason="budget_exceeded", events=True)
    assert refused(ev, root).reason is R.NOT_SCORABLE_STATUS
    ev, root = evidence(status=RunStatus.PARTIAL, reason=HALT, events=False)
    assert refused(ev, root).reason is R.HALT_EVIDENCE_MISSING


def test_a_halted_run_is_accepted_only_with_commitment_anchor_and_halt_evidence() -> None:
    ev, root = evidence(status=RunStatus.PARTIAL, reason=HALT, events=True)
    ticket = admit(ev, root)
    assert ticket.completeness is s.Completeness.HALTED
    assert ticket.may_become_scored is False
    broken = [
        (ev.model_copy(update={"anchor": None}), R.ANCHOR_MISSING),
        (ev.model_copy(update={"commitment": None}), R.NO_COMMITMENT),
        (ev.model_copy(update={"kill_switch_events": ()}), R.HALT_EVIDENCE_MISSING),
    ]
    for bad, reason in broken:
        assert refused(bad, root).reason is reason
    pend, proot = evidence(status=RunStatus.PARTIAL, reason=HALT, events=True, proof="pending")
    assert refused(pend, proot).reason is R.OTS_NOT_CONFIRMED
    bad_hash, broot = evidence(
        status=RunStatus.PARTIAL, reason=HALT, events=True, stored_sha="d" * 64
    )
    assert refused(bad_hash, broot).reason is R.COMMITMENT_MISMATCH


@pytest.mark.parametrize(
    ("mode", "status", "earliest"),
    [
        (RunMode.BACKTEST, RunStatus.ANCHORED, None),
        (RunMode.ABLATION, RunStatus.ANCHORED, None),
        (RunMode.BACKTEST, RunStatus.SCORED, None),
        (RunMode.LIVE, RunStatus.EXECUTED, LIVE_EARLIEST),
        (RunMode.LIVE, RunStatus.SCORED, LIVE_EARLIEST),
    ],
)
def test_complete_runs_are_admitted_and_the_loader_receives_the_ticket(
    mode: RunMode, status: RunStatus, earliest: datetime | None
) -> None:
    ev, root = evidence(mode=mode, status=status)
    loader = Loader()
    got = s.run_scoring(
        s.ScoringRequest(RUN, earliest),
        source=Source(ev),
        headers=BlockHeaders(root, _ts(BLOCK_TIME)),
        git=Git(),
        clock=lambda: NOW,
        load_outcomes=loader,
    )
    assert got == "outcomes" and len(loader.calls) == 1
    t = loader.calls[0]
    assert (t.run_id, t.mode, t.completeness) == (RUN, mode, s.Completeness.COMPLETE)
    assert t.may_become_scored and t.git_commit == GIT_COMMIT and t.requested_at == NOW
    assert t.bitcoin_height == 700_000 and t.bitcoin_block_time == BLOCK_TIME


# --- timing -------------------------------------------------------------------------------------


def test_live_anchor_must_predate_the_earliest_resolution() -> None:
    ev, root = evidence(mode=RunMode.LIVE, status=RunStatus.EXECUTED)
    late = refused(ev, root, earliest=LIVE_EARLIEST, block_time=LIVE_EARLIEST + timedelta(hours=1))
    assert late.reason is R.ANCHOR_TIMING
    equal = refused(ev, root, earliest=LIVE_EARLIEST, block_time=LIVE_EARLIEST)
    assert equal.reason is R.ANCHOR_TIMING  # strictly earlier
    assert refused(ev, root, earliest=None).reason is R.ANCHOR_TIMING  # fails closed
    assert admit(ev, root, earliest=LIVE_EARLIEST).completeness is s.Completeness.COMPLETE


def test_a_retrospective_anchor_after_the_historical_dates_is_accepted() -> None:
    ev, root = evidence(
        anchored_at=AS_OF + timedelta(days=300), committed_at=AS_OF + timedelta(days=299)
    )
    block = AS_OF + timedelta(days=301)
    ticket = admit(ev, root, block_time=block, now=AS_OF + timedelta(days=302))
    assert ticket.completeness is s.Completeness.COMPLETE
    # a backtest is not compared with an outcome-resolution bound at all
    assert admit(ev, root, earliest=AS_OF, block_time=block, now=NOW).bitcoin_block_time == block


def test_retrospective_ordering_is_enforced_against_the_admission_clock() -> None:
    ev, root = evidence(anchored_at=NOW + timedelta(days=1))
    assert refused(ev, root).reason is R.ANCHOR_TIMING  # anchored after admission
    ev, root = evidence()
    assert refused(ev, root, block_time=NOW + timedelta(hours=1)).reason is R.ANCHOR_TIMING
    ev, root = evidence(committed_at=ANCHORED_AT + timedelta(hours=1))
    assert refused(ev, root).reason is R.ANCHOR_TIMING  # anchored before it was committed


def test_admission_time_comes_from_the_clock_and_cannot_be_chosen_by_the_caller() -> None:
    assert "requested_at" not in {f.name for f in dataclasses.fields(s.ScoringRequest)}
    assert "requested_at" not in inspect.signature(s.admit_run).parameters
    ev, root = evidence()
    assert admit(ev, root, now=NOW + timedelta(seconds=7)).requested_at == NOW + timedelta(
        seconds=7
    )
    with pytest.raises(ValueError, match="aware"):
        s.admit_run(
            s.ScoringRequest(RUN),
            source=Source(ev),
            headers=BlockHeaders(root, _ts(BLOCK_TIME)),
            git=Git(),
            clock=lambda: datetime(2025, 1, 1),
        )


# --- precedence ---------------------------------------------------------------------------------


def test_refusal_precedence_is_stable_when_a_run_is_damaged_several_ways() -> None:
    bad_git = Git(False)
    # a tampered hash beats a pending proof and an unreachable commit
    ev, root = evidence(stored_sha="d" * 64, proof="pending")
    assert refused(ev, root, git=bad_git).reason is R.COMMITMENT_MISMATCH
    defect = MaterialDefect(kind=MaterialDefectKind.SNAPSHOT_INVALID, detail="x")
    assert (
        refused(ev.model_copy(update={"defects": (defect,)}), root, git=bad_git).reason
        is R.MATERIAL_INCOMPLETE
    )  # defects beat mismatch
    ev3, root3 = evidence(status=RunStatus.FAILED, stored_sha="d" * 64)
    assert refused(ev3, root3).reason is R.NOT_SCORABLE_STATUS  # status beats material
    no_commit = ev3.model_copy(update={"commitment": None})
    assert refused(no_commit, root3).reason is R.NO_COMMITMENT  # commitment beats status
    ev9, root9 = evidence(status=RunStatus.PARTIAL, reason=HALT, stored_sha="d" * 64)
    assert refused(ev9, root9).reason is R.HALT_EVIDENCE_MISSING  # halt evidence beats material
    ev4, root4 = evidence(proof="none")
    assert refused(ev4, root4, git=bad_git).reason is R.ANCHOR_MISSING
    ev5, root5 = evidence(proof="other")
    assert refused(ev5, root5, git=bad_git).reason is R.ANCHOR_INVALID
    ev6, root6 = evidence(proof="pending")
    assert refused(ev6, root6, git=bad_git).reason is R.OTS_NOT_CONFIRMED
    ev7, _ = evidence()
    assert refused(ev7, OTHER_ROOT, git=bad_git).reason is R.HEADER_VERIFICATION_FAILED
    ev8, root8 = evidence(anchored_at=NOW + timedelta(days=1))
    assert refused(ev8, root8, git=bad_git).reason is R.GIT_UNREACHABLE  # git beats timing


# --- the boundary -------------------------------------------------------------------------------


def test_run_scoring_admits_before_it_calls_the_loader() -> None:
    order: list[str] = []
    ev, root = evidence()

    class Recording(Source):
        def load(self, run_id: UUID) -> ScoringEvidence:
            order.append("admission read")
            return super().load(run_id)

    def loader(ticket: s.ScoringTicket) -> None:
        order.append("outcome loader")

    s.run_scoring(
        s.ScoringRequest(RUN),
        source=Recording(ev),
        headers=BlockHeaders(root, _ts(BLOCK_TIME)),
        git=Git(),
        clock=lambda: NOW,
        load_outcomes=loader,
    )
    assert order == ["admission read", "outcome loader"]


def test_sanctioned_loaders_take_a_ticket_and_run_scoring_requires_one() -> None:
    param = inspect.signature(s.run_scoring).parameters["load_outcomes"]
    assert param.default is inspect.Parameter.empty and param.kind is param.KEYWORD_ONLY
    assert s.OutcomeLoader.__value__.__args__[0] is s.ScoringTicket


def test_a_ticket_is_a_sequencing_token_not_a_security_boundary() -> None:
    ev, root = evidence()
    ticket = admit(ev, root)
    copy = dataclasses.replace(ticket, completeness=s.Completeness.HALTED)
    assert copy.may_become_scored is False  # copying works; safety is admission-before-loader
    with pytest.raises(dataclasses.FrozenInstanceError):
        ticket.run_id = uuid4()  # type: ignore[misc]


def test_a_scored_run_is_readmitted_read_only() -> None:
    ev, root = evidence(status=RunStatus.SCORED)
    before = ev.model_dump_json()
    src = Source(ev)
    ticket = s.admit_run(
        s.ScoringRequest(RUN),
        source=src,
        headers=BlockHeaders(root, _ts(BLOCK_TIME)),
        git=Git(),
        clock=lambda: NOW,
    )
    assert ticket.completeness is s.Completeness.COMPLETE and src.loads == 1
    assert ev.model_dump_json() == before  # nothing was recomputed into the evidence
    # the evidence source protocol has one method, ``load``: admission has no write to call
    assert [m for m in vars(s.ScoringEvidenceSource) if not m.startswith("_")] == ["load"]


def test_the_gate_module_is_pure_and_never_reads_verified_at() -> None:
    tree = ast.parse(Path(s.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    roots = {m.split(".")[0] for m in imported}
    allowed = {
        "__future__",
        "collections",
        "dataclasses",
        "datetime",
        "enum",
        "typing",
        "uuid",
        "contracts",
        "evaluation",
    }
    assert roots <= allowed
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "verified_at" not in attrs | names
