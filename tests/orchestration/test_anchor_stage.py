"""Anchoring adapters (mocked calendars, local bare git repo) and the AnchorStage/AnchorUpgrader.

No network: OpenTimestamps calendars and the block-header source are respx mocks; the "public
remote" is a bare repository on disk.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from bitcoin.core import CBlockHeader
from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation

from contracts.commitment import CommitmentIntegrityError, CommitmentMaterial
from contracts.enums import RunMode, RunStatus
from contracts.models import (
    CommitmentAnchor,
    DlqRecord,
    PortfolioSnapshot,
    ProposedBook,
    RunRecord,
)
from evaluation import anchoring
from orchestration.anchor_adapters import (
    EsploraHeaders,
    GitAnchorRepo,
    OtsCalendars,
    redact,
    remote_is_publicly_readable,
)
from orchestration.anchor_stage import (
    AnchorNotEligibleError,
    AnchorStage,
    AnchorUpgrader,
)
from tests.orchestration.anchoring_support import calendar_reply, material_for

AS_OF = datetime(2024, 3, 1, 21, 0, tzinfo=UTC)
CALS = ["https://a.cal.example", "https://b.cal.example", "https://c.cal.example"]
RUN = UUID("11111111-2222-3333-4444-555555555555")
SHA = hashlib.sha256(b"c").hexdigest()


def _git(cwd: Path, *args: str) -> str:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test User",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test User",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
    )
    out = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True, env=env
    )
    return out.stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    bare = tmp_path / "public.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    return bare


def repo(remote: Path, tmp_path: Path, name: str = "clone") -> GitAnchorRepo:
    return GitAnchorRepo(tmp_path / name, str(remote), "main")


# --- OpenTimestamps calendars -------------------------------------------------------------------


def _accept(cal: str) -> Callable[[httpx.Request], httpx.Response]:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=calendar_reply(request.content, PendingAttestation(cal)))

    return answer


@respx.mock
def test_stamp_needs_the_required_number_of_calendars_and_hides_the_digest() -> None:
    seen: list[bytes] = []

    def a(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return _accept(CALS[0])(request)

    respx.post(f"{CALS[0]}/digest").mock(side_effect=a)
    respx.post(f"{CALS[1]}/digest").mock(return_value=httpx.Response(503))
    respx.post(f"{CALS[2]}/digest").mock(side_effect=_accept(CALS[2]))
    with httpx.Client() as client:
        proof = OtsCalendars(CALS, client, min_success=2).stamp(SHA)
        status = anchoring.check_binding(proof, SHA)
        assert status.pending == tuple(sorted([CALS[0], CALS[2]]))
        assert seen and bytes.fromhex(SHA) not in seen  # the calendar never sees the digest itself

        with pytest.raises(anchoring.AnchorUnavailableError, match=r"only 1/2"):
            OtsCalendars(CALS[:2], client, min_success=2).stamp(SHA)


@respx.mock
def test_stamp_fails_when_no_calendar_answers_or_answers_garbage() -> None:
    respx.post(f"{CALS[0]}/digest").mock(return_value=httpx.Response(200, content=b"junk"))
    with httpx.Client() as client, pytest.raises(anchoring.AnchorUnavailableError):
        OtsCalendars(CALS[:1], client).stamp(SHA)


def test_calendars_must_be_https_and_sane() -> None:
    with httpx.Client() as client:
        with pytest.raises(ValueError, match="https"):
            OtsCalendars(["http://cal.example"], client)
        with pytest.raises(ValueError):
            OtsCalendars([], client)
        with pytest.raises(ValueError):
            OtsCalendars(CALS[:1], client, min_success=2)


@respx.mock
def test_upgrade_requests_only_go_to_configured_calendars() -> None:
    route = respx.get(f"{CALS[0]}/timestamp/{'ab' * 32}").mock(return_value=httpx.Response(404))
    with httpx.Client() as client:
        cal = OtsCalendars(CALS[:1], client)
        assert cal.fetch("https://evil.example", bytes.fromhex("ab" * 32)) is None
        assert not respx.calls  # a URI that came from inside a proof is never contacted
        assert cal.fetch(CALS[0] + "/", bytes.fromhex("ab" * 32)) is None  # 404: still pending
        assert route.call_count == 1
        route.mock(return_value=httpx.Response(200, content=b"ok"))
        assert cal.fetch(CALS[0], bytes.fromhex("ab" * 32)) == b"ok"
        route.mock(return_value=httpx.Response(500))
        with pytest.raises(anchoring.AnchorUnavailableError):
            cal.fetch(CALS[0], bytes.fromhex("ab" * 32))


# --- block headers ------------------------------------------------------------------------------


def _header(root: bytes) -> tuple[CBlockHeader, str, str]:
    h = CBlockHeader(
        nVersion=1, hashPrevBlock=b"\x00" * 32, hashMerkleRoot=root, nTime=1_700_000_000
    )
    return h, h.serialize().hex(), bytes(h.GetHash())[::-1].hex()


@respx.mock
def test_headers_are_rehashed_against_the_block_id() -> None:
    root = hashlib.sha256(b"root").digest()
    _, raw, block_id = _header(root)
    respx.get("https://explorer.example/api/block-height/700000").mock(
        return_value=httpx.Response(200, text=block_id)
    )
    respx.get(f"https://explorer.example/api/block/{block_id}/header").mock(
        return_value=httpx.Response(200, text=raw)
    )
    with httpx.Client() as client:
        got = EsploraHeaders("https://explorer.example/api", client).header(700_000)
        assert bytes(got.hashMerkleRoot) == root and got.nTime == 1_700_000_000

    lie = _header(hashlib.sha256(b"other").digest())[1]  # a header that does not hash to the id
    respx.get(f"https://explorer.example/api/block/{block_id}/header").mock(
        return_value=httpx.Response(200, text=lie)
    )
    with httpx.Client() as client, pytest.raises(anchoring.AnchorBindingError):
        EsploraHeaders("https://explorer.example/api", client).header(700_000)


@respx.mock
def test_malformed_explorer_replies_are_refused() -> None:
    respx.get("https://explorer.example/api/block-height/1").mock(
        return_value=httpx.Response(200, text="not a hash")
    )
    with httpx.Client() as client, pytest.raises(anchoring.AnchorUnavailableError):
        EsploraHeaders("https://explorer.example/api", client).header(1)


# --- git remote ----------------------------------------------------------------------------------


def test_anchor_commits_and_pushes_a_manifest_and_is_reachable(
    remote: Path, tmp_path: Path
) -> None:
    manifest = anchoring.anchor_manifest(RUN, SHA, AS_OF)
    path = anchoring.manifest_path(RUN)
    r = repo(remote, tmp_path)
    commit = r.anchor({path: manifest})[path]
    assert r.is_reachable(commit)
    assert _git(remote, "show", f"main:{path}") == manifest.decode().strip()  # it is on the remote
    assert _git(remote, "rev-parse", "main") == commit


def test_reanchoring_reuses_the_existing_commit_and_never_rewrites_a_manifest(
    remote: Path, tmp_path: Path
) -> None:
    manifest = anchoring.anchor_manifest(RUN, SHA, AS_OF)
    path = anchoring.manifest_path(RUN)
    first = repo(remote, tmp_path).anchor({path: manifest})[path]
    again = repo(remote, tmp_path).anchor({path: manifest})[path]  # same clone, a retry
    fresh = repo(remote, tmp_path, "other-clone").anchor({path: manifest})[path]  # lost clone
    assert first == again == fresh
    assert _git(remote, "rev-list", "--count", "main") == "1"  # still a single commit
    other = anchoring.anchor_manifest(RUN, hashlib.sha256(b"x").hexdigest(), AS_OF)
    with pytest.raises(anchoring.AnchorBindingError, match="different content"):
        repo(remote, tmp_path).anchor({path: other})
    assert _git(remote, "rev-list", "--count", "main") == "1"


def test_many_runs_share_one_commit_per_call(remote: Path, tmp_path: Path) -> None:
    files = {
        anchoring.manifest_path(u): anchoring.anchor_manifest(u, SHA, AS_OF)
        for u in (uuid4(), uuid4(), uuid4())
    }
    out = repo(remote, tmp_path).anchor(files)
    assert len(set(out.values())) == 1 and _git(remote, "rev-list", "--count", "main") == "1"


def test_a_commit_that_is_not_on_the_remote_is_not_reachable(remote: Path, tmp_path: Path) -> None:
    r = repo(remote, tmp_path)
    r.anchor({anchoring.manifest_path(RUN): anchoring.anchor_manifest(RUN, SHA, AS_OF)})
    _git(tmp_path / "clone", "commit", "-q", "--allow-empty", "-m", "local only")
    local_only = _git(tmp_path / "clone", "rev-parse", "HEAD")
    assert not r.is_reachable(local_only)
    assert not r.is_reachable("0" * 40) and not r.is_reachable("not-a-sha")


@pytest.mark.parametrize(
    "path", ["../evil.json", "anchors/x.json", "anchors/../../x", "/abs/path.json", "README.md"]
)
def test_only_manifest_paths_are_written(remote: Path, tmp_path: Path, path: str) -> None:
    with pytest.raises(ValueError, match="manifest path"):
        repo(remote, tmp_path).anchor({path: b"x"})
    assert not (tmp_path / "clone" / "README.md").exists()


@pytest.mark.parametrize("branch", ["-x", "a..b", "", "a b", "a;b", "x.lock", "x/"])
def test_branch_names_are_validated(tmp_path: Path, branch: str) -> None:
    with pytest.raises(ValueError, match="branch"):
        GitAnchorRepo(tmp_path / "c", "remote", branch)


@pytest.mark.parametrize("bad", ["", "--upload-pack=evil", "a\nb"])
def test_remote_values_are_validated(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError, match="remote"):
        GitAnchorRepo(tmp_path / "c", bad, "main")


def test_an_unreachable_remote_is_a_transient_error(tmp_path: Path) -> None:
    r = GitAnchorRepo(tmp_path / "c", str(tmp_path / "missing.git"), "main")
    with pytest.raises(anchoring.AnchorUnavailableError):
        r.anchor({anchoring.manifest_path(RUN): b"x"})


def test_public_readability_is_an_anonymous_check(remote: Path, tmp_path: Path) -> None:
    assert remote_is_publicly_readable(str(remote))
    assert not remote_is_publicly_readable(str(tmp_path / "nope.git"))
    assert not remote_is_publicly_readable("--upload-pack=evil")


def test_credentials_never_appear_in_redacted_urls() -> None:
    assert redact("https://user:secret@example.com/o/r.git") == "https://example.com/o/r.git"
    assert redact("https://example.com/o/r.git") == "https://example.com/o/r.git"
    assert "secret" not in redact("https://user:secret@example.com:8443/o/r.git")


# --- the stage ----------------------------------------------------------------------------------


def _run(status: RunStatus = RunStatus.COMMITTED) -> RunRecord:
    return RunRecord(
        run_id=RUN,
        mode=RunMode.LIVE,
        as_of=AS_OF,
        config_hash="c" * 64,
        status=status,
        started_at=AS_OF,
        ended_at=AS_OF,
    )


def _snapshot() -> PortfolioSnapshot:
    book = ProposedBook(run_id=RUN, as_of=AS_OF, positions=())
    return PortfolioSnapshot(run_id=RUN, as_of=AS_OF, book=book, cash_weight=1.0)


class MemAnchorStore:
    def __init__(self, status: RunStatus = RunStatus.COMMITTED) -> None:
        self.run = _run(status)
        self.material: CommitmentMaterial | None = material_for(
            self.run, _snapshot(), committed_at=AS_OF, anchored=False
        )
        self.dlq: list[DlqRecord] = []
        self.marked: list[CommitmentAnchor] = []
        self.fail_mark = False

    def load_run(self, run_id: UUID) -> RunRecord | None:
        return self.run

    def load_commitment_material(self, run_id: UUID) -> CommitmentMaterial | None:
        return self.material

    def mark_anchored(self, anchor: CommitmentAnchor) -> bool:
        if self.fail_mark:
            raise RuntimeError("database unavailable")
        if self.run.status is not RunStatus.COMMITTED:
            return False
        self.marked.append(anchor)
        self.run = self.run.model_copy(update={"status": RunStatus.ANCHORED})
        assert self.material is not None
        self.material = self.material.model_copy(update={"anchor": anchor})
        return True

    def record_dlq(self, records: tuple[DlqRecord, ...]) -> None:
        self.dlq.extend(records)


class CountingStamper:
    def __init__(self) -> None:
        self.stamps = 0
        self.fail = False

    def stamp(self, sha256_hex: str) -> bytes:
        if self.fail:
            raise anchoring.AnchorUnavailableError("no calendar reachable")
        self.stamps += 1
        from tests.orchestration.anchoring_support import pending_proof

        return pending_proof(sha256_hex, nonce=bytes([self.stamps]) * 16)

    def fetch(self, uri: str, commitment: bytes) -> bytes | None:
        return None


class FakeGit:
    def __init__(self) -> None:
        self.anchored: dict[str, bytes] = {}
        self.reachable = True

    def anchor(self, files: dict[str, bytes]) -> dict[str, str]:
        out = {}
        for path, data in files.items():
            assert self.anchored.setdefault(path, data) == data  # never rewritten
            out[path] = hashlib.sha1(path.encode()).hexdigest()
        return out

    def is_reachable(self, commit: str) -> bool:
        return self.reachable


def _stage(store: MemAnchorStore, stamper: CountingStamper, git: FakeGit) -> AnchorStage:
    return AnchorStage(store=store, stamper=stamper, git=git, clock=lambda: AS_OF)


def test_a_verified_commitment_is_anchored_with_both_proofs() -> None:
    store, stamper, git = MemAnchorStore(), CountingStamper(), FakeGit()
    res = _stage(store, stamper, git).anchor_run(RUN)
    assert res.status is RunStatus.ANCHORED and res.advanced and stamper.stamps == 1
    (anchor,) = store.marked
    assert store.material is not None and anchor.sha256 == store.material.stored_sha256
    assert anchor.git_commit == res.git_commit and anchor.ots_proof is not None
    anchoring.check_binding(anchor.ots_proof, anchor.sha256)
    manifest = git.anchored[anchoring.manifest_path(RUN)]
    assert anchor.sha256.encode() in manifest and str(RUN).encode() in manifest


def test_replaying_an_anchored_run_stamps_and_pushes_nothing_new() -> None:
    store, stamper, git = MemAnchorStore(), CountingStamper(), FakeGit()
    stage = _stage(store, stamper, git)
    stage.anchor_run(RUN)
    again = stage.anchor_run(RUN)
    assert again.status is RunStatus.ANCHORED and not again.advanced
    assert stamper.stamps == 1 and len(store.marked) == 1


def test_a_crash_after_the_external_effects_is_repaired_by_running_again() -> None:
    store, stamper, git = MemAnchorStore(), CountingStamper(), FakeGit()
    store.fail_mark = True
    with pytest.raises(RuntimeError, match="database unavailable"):
        _stage(store, stamper, git).anchor_run(RUN)
    assert (
        store.run.status is RunStatus.COMMITTED and len(git.anchored) == 1
    )  # pushed, not recorded
    store.fail_mark = False
    res = _stage(store, stamper, git).anchor_run(RUN)  # the retry reuses the manifest
    assert res.status is RunStatus.ANCHORED and len(git.anchored) == 1


@pytest.mark.parametrize("how", ["hash", "book"])
def test_a_commitment_mismatch_anchors_nothing_and_is_evidenced(how: str) -> None:
    store, stamper, git = MemAnchorStore(), CountingStamper(), FakeGit()
    m = store.material
    assert m is not None
    if how == "hash":
        store.material = m.model_copy(update={"stored_sha256": "f" * 64})
    else:
        store.material = m.model_copy(
            update={"snapshot": m.snapshot.model_copy(update={"cash_weight": 0.5})}
        )
    with pytest.raises(CommitmentIntegrityError):
        _stage(store, stamper, git).anchor_run(RUN)
    assert stamper.stamps == 0 and git.anchored == {} and store.marked == []  # nothing external
    assert store.run.status is RunStatus.COMMITTED
    assert [d.error_type for d in store.dlq] == ["commitment_mismatch"]


def test_uncommitted_halted_or_unknown_runs_are_not_anchored() -> None:
    for status in (RunStatus.PARTIAL, RunStatus.FAILED, RunStatus.AGENTS_OK):
        store, stamper, git = MemAnchorStore(status), CountingStamper(), FakeGit()
        with pytest.raises(AnchorNotEligibleError):
            _stage(store, stamper, git).anchor_run(RUN)
        assert stamper.stamps == 0 and git.anchored == {}
    store = MemAnchorStore()
    store.material = None
    with pytest.raises(AnchorNotEligibleError, match="no decision commitment"):
        _stage(store, CountingStamper(), FakeGit()).anchor_run(RUN)


def test_a_missing_calendar_or_unreachable_remote_never_anchors() -> None:
    store, stamper, git = MemAnchorStore(), CountingStamper(), FakeGit()
    stamper.fail = True
    with pytest.raises(anchoring.AnchorUnavailableError):
        _stage(store, stamper, git).anchor_run(RUN)
    assert store.run.status is RunStatus.COMMITTED and git.anchored == {}

    stamper.fail = False
    git.reachable = False  # pushed but the remote does not show the commit
    with pytest.raises(anchoring.AnchorBindingError, match="not reachable"):
        _stage(store, stamper, git).anchor_run(RUN)
    assert store.run.status is RunStatus.COMMITTED and store.marked == []


def test_an_anchored_run_whose_stored_proof_no_longer_binds_is_an_integrity_failure() -> None:
    store, stamper, git = MemAnchorStore(), CountingStamper(), FakeGit()
    _stage(store, stamper, git).anchor_run(RUN)
    assert store.material is not None and store.material.anchor is not None
    foreign = CountingStamper().stamp("d" * 64)
    store.material = store.material.model_copy(
        update={"anchor": store.material.anchor.model_copy(update={"ots_proof": foreign})}
    )
    with pytest.raises(anchoring.AnchorBindingError):
        _stage(store, stamper, git).anchor_run(RUN)


# --- Bitcoin confirmation -------------------------------------------------------------------------


class MemConfirmStore:
    def __init__(self, anchors: list[CommitmentAnchor]) -> None:
        self.anchors = anchors
        self.recorded: list[CommitmentAnchor] = []

    def anchors_awaiting_confirmation(self) -> list[CommitmentAnchor]:
        return [a for a in self.anchors if a.verified_at is None]

    def record_anchor(self, anchor: CommitmentAnchor) -> None:
        self.recorded.append(anchor)


class Calendar:
    """Answers upgrade requests with a Bitcoin attestation once ``ready``."""

    def __init__(self) -> None:
        self.ready = False

    def stamp(self, sha256_hex: str) -> bytes:  # pragma: no cover - unused
        raise AssertionError

    def fetch(self, uri: str, commitment: bytes) -> bytes | None:
        return (
            calendar_reply(commitment, BitcoinBlockHeaderAttestation(700_000))
            if self.ready
            else None
        )


def _pending_anchor() -> tuple[CommitmentAnchor, bytes]:
    material = material_for(_run(), _snapshot(), committed_at=AS_OF)
    assert material.anchor is not None
    file = anchoring.loads(material.anchor.ots_proof or b"")
    leaf = next(iter(file.timestamp.all_attestations()))[0]
    return material.anchor, leaf


def test_the_upgrader_sets_verified_at_only_after_a_header_verifies() -> None:
    anchor, _ = _pending_anchor()
    store, cal = MemConfirmStore([anchor]), Calendar()
    roots: dict[str, bytes] = {}

    class Headers:
        def header(self, height: int) -> object:
            return SimpleNamespace(hashMerkleRoot=roots["root"], nTime=1_700_000_000)

    upgrader = AnchorUpgrader(store=store, stamper=cal, headers=Headers(), clock=lambda: AS_OF)
    assert upgrader.upgrade_all().upgraded == 0 and store.recorded == []  # nothing ready yet

    cal.ready = True
    file = anchoring.loads(anchor.ots_proof or b"")
    roots["root"] = next(iter(file.timestamp.all_attestations()))[0]
    summary = upgrader.upgrade_all()
    assert (summary.checked, summary.upgraded, summary.confirmed, summary.failed) == (1, 1, 1, 0)
    (done,) = store.recorded
    assert done.verified_at == AS_OF and done.sha256 == anchor.sha256
    status = anchoring.check_binding(done.ots_proof or b"", done.sha256)
    assert status.confirmed and status.pending == ()


def test_a_header_that_does_not_verify_leaves_the_anchor_unconfirmed() -> None:
    anchor, _ = _pending_anchor()
    store, cal = MemConfirmStore([anchor]), Calendar()
    cal.ready = True

    class WrongHeaders:
        def header(self, height: int) -> object:
            return SimpleNamespace(hashMerkleRoot=b"\x00" * 32, nTime=1)

    summary = AnchorUpgrader(
        store=store, stamper=cal, headers=WrongHeaders(), clock=lambda: AS_OF
    ).upgrade_all()
    assert summary.failed == 1 and summary.confirmed == 0 and store.recorded == []


def test_without_a_header_source_a_proof_is_upgraded_but_never_marked_verified() -> None:
    anchor, _ = _pending_anchor()
    store, cal = MemConfirmStore([anchor]), Calendar()
    cal.ready = True
    summary = AnchorUpgrader(
        store=store, stamper=cal, headers=None, clock=lambda: AS_OF
    ).upgrade_all()
    assert summary.upgraded == 1 and summary.confirmed == 0
    assert store.recorded[0].verified_at is None
