"""Test builders for anchored runs: real commitment hashes and pending OpenTimestamps proofs."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from types import SimpleNamespace

from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation
from opentimestamps.core.serialize import BytesSerializationContext

from contracts.commitment import CommitmentMaterial, commitment_hash
from contracts.models import (
    CommitmentAnchor,
    CommitteeDecisionRecord,
    PortfolioSnapshot,
    RunRecord,
)
from evaluation import anchoring

CALENDAR = "https://alice.btc.calendar.opentimestamps.org"
GIT_COMMIT = "a" * 40


def calendar_reply(msg: bytes, *attestations: object) -> bytes:
    """What a calendar would answer for ``msg``: a bare timestamp holding the attestations."""
    from opentimestamps.core.timestamp import Timestamp

    ts = Timestamp(msg)
    for att in attestations:
        ts.attestations.add(att)
    ctx = BytesSerializationContext()
    ts.serialize(ctx)
    return bytes(ctx.getbytes())


def pending_proof(sha256_hex: str, *, nonce: bytes = bytes(range(16))) -> bytes:
    """A valid detached OpenTimestamps file for the digest with one pending calendar attestation."""
    file, node = anchoring.new_stamp(bytes.fromhex(sha256_hex), nonce)
    node.merge(
        anchoring.parse_fragment(calendar_reply(node.msg, PendingAttestation(CALENDAR)), node.msg)
    )
    return anchoring.dumps(file)


def material_for(
    run: RunRecord,
    snapshot: PortfolioSnapshot,
    decisions: Sequence[CommitteeDecisionRecord] = (),
    *,
    committed_at: datetime,
    anchored: bool = True,
) -> CommitmentMaterial:
    sha = commitment_hash(run, decisions, snapshot)
    anchor = (
        CommitmentAnchor(
            run_id=run.run_id,
            sha256=sha,
            ots_proof=pending_proof(sha),
            git_commit=GIT_COMMIT,
            anchored_at=committed_at,
        )
        if anchored
        else None
    )
    return CommitmentMaterial(
        run=run,
        decisions=tuple(decisions),
        snapshot=snapshot,
        stored_sha256=sha,
        committed_at=committed_at,
        anchor=anchor,
    )


class BlockHeaders:
    """A stand-in block-header source: every height returns one merkle root and time."""

    def __init__(self, root: bytes | None, time: int) -> None:
        self.root, self.time = root, time

    def header(self, height: int) -> object:
        return SimpleNamespace(hashMerkleRoot=self.root, nTime=self.time)


def confirmed_proof(sha256_hex: str, *, height: int = 700_000) -> tuple[bytes, bytes]:
    """A proof upgraded to a Bitcoin attestation, and the merkle root a header must carry."""
    file, node = anchoring.new_stamp(bytes.fromhex(sha256_hex), bytes(range(16)))
    node.merge(
        anchoring.parse_fragment(calendar_reply(node.msg, PendingAttestation(CALENDAR)), node.msg)
    )
    anchoring.upgrade(
        file, lambda _uri, msg: calendar_reply(msg, BitcoinBlockHeaderAttestation(height))
    )
    return anchoring.dumps(file), node.msg
