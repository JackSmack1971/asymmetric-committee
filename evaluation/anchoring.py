"""External anchoring primitives (§11, §12.2): the git manifest and OpenTimestamps proof handling.

Pure functions only: no network, database, git or clock. Adapters live in
``orchestration/anchor_adapters.py`` and orchestration in ``orchestration/anchor_stage.py``.

What an anchor proves. The commitment hash (``contracts.commitment``) is the file digest of a
detached OpenTimestamps file (SHA-256). Before a run may be ``ANCHORED`` the proof must bind that
exact digest and carry at least one attestation (a calendar's pending promise is enough to
execute). Before a run may be *scored* the proof must be Bitcoin-confirmed and verified against
block headers (``confirm_bitcoin``). Proofs come from the network, so every parse is bounded and
any malformed input is an ``AnchorBindingError``, never a partial result.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from opentimestamps.core.notary import (
    BitcoinBlockHeaderAttestation,
    PendingAttestation,
    VerificationError,
)
from opentimestamps.core.op import OpAppend, OpSHA256
from opentimestamps.core.serialize import (
    BytesDeserializationContext,
    BytesSerializationContext,
    StreamDeserializationContext,
)
from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

MANIFEST_VERSION = "anchor_v1"
MAX_PROOF_BYTES = 256 * 1024
MAX_FRAGMENT_BYTES = 64 * 1024


class AnchorBindingError(ValueError):
    """A proof or manifest does not bind the commitment it claims to. Never auto-retried."""


class AnchorUnavailableError(RuntimeError):
    """A calendar, git remote or block-header source could not be reached: transient, retryable."""


class HeaderSource(Protocol):
    """Bitcoin block headers by height: anything with ``hashMerkleRoot`` and ``nTime``."""

    def header(self, height: int) -> Any: ...


@dataclass(frozen=True)
class ProofStatus:
    pending: tuple[str, ...]  # calendar URIs still promising an attestation
    bitcoin_heights: tuple[int, ...]  # block heights of Bitcoin attestations (unverified here)

    @property
    def confirmed(self) -> bool:
        return bool(self.bitcoin_heights)


@dataclass(frozen=True)
class BitcoinConfirmation:
    height: int
    block_time: int  # unix seconds from the verified header


def anchor_manifest(run_id: UUID, sha256: str, committed_at: datetime) -> bytes:
    """The file committed to the public git remote: deterministic, one per run."""
    if committed_at.tzinfo is None:
        raise ValueError("committed_at must be timezone-aware")
    doc = {
        "v": MANIFEST_VERSION,
        "run_id": str(run_id),
        "sha256": sha256,
        "committed_at": committed_at.isoformat(timespec="microseconds"),
    }
    return (
        json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode()


def manifest_path(run_id: UUID) -> str:
    return f"anchors/{run_id}.json"


def new_stamp(digest: bytes, nonce: bytes) -> tuple[DetachedTimestampFile, Timestamp]:
    """A detached file for ``digest`` plus the node to submit to calendars.

    Like ``ots stamp``: a random nonce is appended and hashed so a calendar never sees the digest
    itself. The returned node's ``msg`` is what is POSTed; its reply is merged into that node.
    """
    if len(digest) != 32 or len(nonce) < 16:
        raise ValueError("digest must be 32 bytes and nonce at least 16")
    file = DetachedTimestampFile(OpSHA256(), Timestamp(digest))
    node = file.timestamp.ops.add(OpAppend(nonce)).ops.add(OpSHA256())
    return file, node


def parse_fragment(data: bytes, msg: bytes) -> Timestamp:
    """A calendar reply for ``msg``. Bounded; malformed or trailing data is an error."""
    if not data or len(data) > MAX_FRAGMENT_BYTES:
        raise AnchorBindingError("calendar reply is empty or too large")
    try:
        ctx = StreamDeserializationContext(io.BytesIO(data))
        fragment = Timestamp.deserialize(ctx, msg)
        ctx.assert_eof()
    except Exception as exc:
        raise AnchorBindingError(f"calendar reply is not a valid timestamp: {exc}") from exc
    if not list(fragment.all_attestations()):
        raise AnchorBindingError("calendar reply carries no attestation")
    return fragment


def dumps(file: DetachedTimestampFile) -> bytes:
    ctx = BytesSerializationContext()
    file.serialize(ctx)
    return bytes(ctx.getbytes())


def loads(proof: bytes) -> DetachedTimestampFile:
    if not proof or len(proof) > MAX_PROOF_BYTES:
        raise AnchorBindingError("OpenTimestamps proof is empty or too large")
    try:
        return DetachedTimestampFile.deserialize(BytesDeserializationContext(proof))
    except Exception as exc:
        raise AnchorBindingError(f"not a valid OpenTimestamps file: {exc}") from exc


def check_binding(proof: bytes, sha256_hex: str) -> ProofStatus:
    """The proof must be a SHA-256 detached file for exactly this digest, with an attestation."""
    file = loads(proof)
    digest = bytes.fromhex(sha256_hex)
    if not isinstance(file.file_hash_op, OpSHA256):
        raise AnchorBindingError("proof does not use SHA-256")
    if file.file_digest != digest or file.timestamp.msg != digest:
        raise AnchorBindingError("proof binds a different digest than the commitment")
    pending: list[str] = []
    heights: list[int] = []
    for _msg, att in file.timestamp.all_attestations():
        if isinstance(att, PendingAttestation):
            pending.append(att.uri.decode() if isinstance(att.uri, bytes) else str(att.uri))
        elif isinstance(att, BitcoinBlockHeaderAttestation):
            heights.append(int(att.height))
    if not pending and not heights:
        raise AnchorBindingError("proof carries no calendar or Bitcoin attestation")
    return ProofStatus(tuple(sorted(pending)), tuple(sorted(heights)))


def _walk(ts: Timestamp) -> Iterator[Timestamp]:
    yield ts
    for child in ts.ops.values():
        yield from _walk(child)


def upgrade(file: DetachedTimestampFile, fetch: Callable[[str, bytes], bytes | None]) -> bool:
    """Ask each pending calendar for its completed timestamp and merge what comes back.

    ``fetch(uri, commitment)`` returns the calendar's reply or ``None`` if not ready yet. Returns
    True when the file changed. A pending attestation is dropped once its leaf gained a Bitcoin one.
    """
    changed = False
    for node in list(_walk(file.timestamp)):
        for att in list(node.attestations):
            if not isinstance(att, PendingAttestation):
                continue
            uri = att.uri.decode() if isinstance(att.uri, bytes) else str(att.uri)
            data = fetch(uri, node.msg)
            if data is None:
                continue
            fragment = parse_fragment(data, node.msg)
            node.merge(fragment)
            changed = True
            if any(
                isinstance(a, BitcoinBlockHeaderAttestation)
                for _m, a in fragment.all_attestations()
            ):
                node.attestations.discard(att)
    return changed


def confirm_bitcoin(proof: bytes, sha256_hex: str, headers: HeaderSource) -> BitcoinConfirmation:
    """Verify a Bitcoin attestation against the real block header (required before scoring).

    Raises ``AnchorBindingError`` if the proof does not bind the digest, has no Bitcoin
    attestation, or no attestation matches its block's merkle root.
    """
    check_binding(proof, sha256_hex)
    file = loads(proof)
    best: BitcoinConfirmation | None = None
    for msg, att in file.timestamp.all_attestations():
        if not isinstance(att, BitcoinBlockHeaderAttestation):
            continue
        try:
            block_time = int(att.verify_against_blockheader(msg, headers.header(int(att.height))))
        except VerificationError:
            continue
        found = BitcoinConfirmation(int(att.height), block_time)
        if best is None or found.height < best.height:
            best = found
    if best is None:
        raise AnchorBindingError("no Bitcoin attestation verifies against its block header")
    return best
