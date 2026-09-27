"""OpenTimestamps proof handling: binding, upgrade and Bitcoin confirmation (§12.2)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from opentimestamps.core.notary import (
    BitcoinBlockHeaderAttestation,
    LitecoinBlockHeaderAttestation,
    PendingAttestation,
)
from opentimestamps.core.serialize import BytesSerializationContext
from opentimestamps.core.timestamp import Timestamp

from evaluation import anchoring as a

DIGEST = hashlib.sha256(b"commitment").digest()
HEX = DIGEST.hex()
NONCE = bytes(range(16))
CAL = "https://alice.btc.calendar.opentimestamps.org"
RUN = UUID("12345678-1234-5678-1234-567812345678")


def _reply(msg: bytes, *attestations: object) -> bytes:
    ts = Timestamp(msg)
    for att in attestations:
        ts.attestations.add(att)
    ctx = BytesSerializationContext()
    ts.serialize(ctx)
    return bytes(ctx.getbytes())


def _stamped() -> tuple[bytes, Timestamp]:
    file, node = a.new_stamp(DIGEST, NONCE)
    node.merge(a.parse_fragment(_reply(node.msg, PendingAttestation(CAL)), node.msg))
    return a.dumps(file), node


def test_a_calendar_stamp_binds_the_exact_digest() -> None:
    proof, _ = _stamped()
    status = a.check_binding(proof, HEX)
    assert status.pending == (CAL,) and not status.confirmed


def test_a_proof_for_another_digest_is_refused() -> None:
    proof, _ = _stamped()
    other = hashlib.sha256(b"tampered").hexdigest()
    with pytest.raises(a.AnchorBindingError, match="different digest"):
        a.check_binding(proof, other)


@pytest.mark.parametrize("junk", [b"", b"\x00" * 10, b"OpenTimestamps" + b"\xff" * 40])
def test_garbage_proofs_are_refused(junk: bytes) -> None:
    with pytest.raises(a.AnchorBindingError):
        a.check_binding(junk, HEX)


def test_oversized_proofs_and_replies_are_refused() -> None:
    with pytest.raises(a.AnchorBindingError):
        a.loads(b"x" * (a.MAX_PROOF_BYTES + 1))
    with pytest.raises(a.AnchorBindingError):
        a.parse_fragment(b"x" * (a.MAX_FRAGMENT_BYTES + 1), DIGEST)


def test_a_reply_with_trailing_bytes_is_refused() -> None:
    _, node = a.new_stamp(DIGEST, NONCE)
    good = _reply(node.msg, PendingAttestation(CAL))
    with pytest.raises(a.AnchorBindingError):
        a.parse_fragment(good + b"\x00", node.msg)


def test_a_proof_with_only_a_foreign_chain_attestation_is_refused() -> None:
    file, node = a.new_stamp(DIGEST, NONCE)
    node.attestations.add(LitecoinBlockHeaderAttestation(1))
    with pytest.raises(a.AnchorBindingError, match="no calendar or Bitcoin"):
        a.check_binding(a.dumps(file), HEX)


def test_upgrade_merges_a_bitcoin_attestation_and_drops_the_pending_one() -> None:
    proof, node = _stamped()
    file = a.loads(proof)
    calls: list[tuple[str, bytes]] = []

    def fetch(uri: str, commitment: bytes) -> bytes | None:
        calls.append((uri, commitment))
        return _reply(commitment, BitcoinBlockHeaderAttestation(700_000))

    assert a.upgrade(file, fetch) is True
    assert calls == [(CAL, node.msg)]
    status = a.check_binding(a.dumps(file), HEX)
    assert status.confirmed and status.bitcoin_heights == (700_000,) and status.pending == ()


def test_upgrade_leaves_a_not_yet_ready_proof_untouched() -> None:
    proof, _ = _stamped()
    file = a.loads(proof)
    assert a.upgrade(file, lambda _uri, _msg: None) is False
    assert a.dumps(file) == proof


class _Headers:
    def __init__(self, root: bytes | None, time: int = 1_700_000_000) -> None:
        self.root, self.time = root, time

    def header(self, height: int) -> object:
        return SimpleNamespace(hashMerkleRoot=self.root, nTime=self.time)


def _confirmed() -> tuple[bytes, bytes]:
    proof, node = _stamped()
    file = a.loads(proof)
    a.upgrade(file, lambda _u, m: _reply(m, BitcoinBlockHeaderAttestation(700_000)))
    return a.dumps(file), node.msg


def test_bitcoin_confirmation_is_checked_against_the_block_header() -> None:
    proof, root = _confirmed()
    got = a.confirm_bitcoin(proof, HEX, _Headers(root))
    assert (got.height, got.block_time) == (700_000, 1_700_000_000)


def test_a_header_with_a_different_merkle_root_does_not_confirm() -> None:
    proof, _ = _confirmed()
    with pytest.raises(a.AnchorBindingError, match="no Bitcoin attestation verifies"):
        a.confirm_bitcoin(proof, HEX, _Headers(b"\x00" * 32))


def test_a_pending_only_proof_is_not_confirmed() -> None:
    proof, _ = _stamped()
    with pytest.raises(a.AnchorBindingError):
        a.confirm_bitcoin(proof, HEX, _Headers(b"\x00" * 32))


def test_the_manifest_is_deterministic_and_names_the_run() -> None:
    at = datetime(2024, 6, 28, 20, 0, tzinfo=UTC)
    one = a.anchor_manifest(RUN, HEX, at)
    assert one == a.anchor_manifest(RUN, HEX, at)
    assert HEX.encode() in one and str(RUN).encode() in one
    assert a.manifest_path(RUN) == f"anchors/{RUN}.json"
    with pytest.raises(ValueError):
        a.anchor_manifest(RUN, HEX, datetime(2024, 6, 28))
