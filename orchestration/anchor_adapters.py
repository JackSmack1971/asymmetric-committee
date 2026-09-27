"""Network and git adapters for anchoring (§12.2): OTS calendars, git remote, block headers.

Everything here is a side effect against a system the operator configures; the pure proof logic is
in ``evaluation/anchoring.py``. Rules that apply to every adapter:

- No shell: git is run with an argument list, remote/branch/path values are validated first and
  passed after ``--`` where git allows it, hooks are skipped, prompts are disabled.
- Remote URLs may carry credentials, so error text never contains them.
- Proofs and replies come from the network and are size-bounded and parsed by
  ``evaluation.anchoring`` before anything is trusted.
- Calendar upgrade requests only go to calendars the operator configured, never to a URI that
  arrived inside a proof (SSRF).
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from bitcoin.core import CBlockHeader

from evaluation.anchoring import (
    MAX_FRAGMENT_BYTES,
    AnchorBindingError,
    AnchorUnavailableError,
    check_binding,
    dumps,
    manifest_path,
    new_stamp,
    parse_fragment,
)

_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_MANIFEST = re.compile(
    r"^anchors/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.json$"
)
_OTS_HEADERS = {"Accept": "application/vnd.opentimestamps.v1", "User-Agent": "asymmetric-committee"}


def redact(url: str) -> str:
    """The URL without any ``user:password@`` part, safe to log."""
    parts = urlsplit(url)
    if parts.username or parts.password:
        host = parts.hostname or ""
        netloc = f"{host}:{parts.port}" if parts.port else host
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    return url


class OtsCalendars:
    """Submit a commitment digest to several OpenTimestamps calendars and merge their replies."""

    def __init__(
        self,
        urls: Sequence[str],
        client: httpx.Client,
        *,
        min_success: int = 1,
        nonce: Callable[[], bytes] = lambda: os.urandom(16),
        allow_http: bool = False,
    ) -> None:
        clean = [u.rstrip("/") for u in urls]
        if not clean or min_success < 1 or min_success > len(clean):
            raise ValueError("need at least one calendar and 1 <= min_success <= len(urls)")
        if not allow_http and any(not u.startswith("https://") for u in clean):
            raise ValueError("OpenTimestamps calendars must be https")
        self._urls = tuple(clean)
        self._client = client
        self._min = min_success
        self._nonce = nonce

    def stamp(self, sha256_hex: str) -> bytes:
        """A serialized detached timestamp for the digest, with at least ``min_success`` pending
        calendar attestations. The result is re-parsed and checked before it is returned."""
        file, node = new_stamp(bytes.fromhex(sha256_hex), self._nonce())
        ok, failures = 0, []
        for url in self._urls:
            try:
                resp = self._client.post(f"{url}/digest", content=node.msg, headers=_OTS_HEADERS)
                resp.raise_for_status()
                node.merge(parse_fragment(resp.content, node.msg))
                ok += 1
            except (httpx.HTTPError, AnchorBindingError) as exc:
                failures.append(f"{url}: {type(exc).__name__}")
        if ok < self._min:
            raise AnchorUnavailableError(
                f"only {ok}/{self._min} calendars accepted the digest ({'; '.join(failures)})"
            )
        proof = dumps(file)
        check_binding(proof, sha256_hex)
        return proof

    def fetch(self, uri: str, commitment: bytes) -> bytes | None:
        """The calendar's completed timestamp for ``commitment``, or ``None`` while pending."""
        base = uri.rstrip("/")
        if base not in self._urls:
            return None  # a URI that came from a proof, not from our configuration
        try:
            resp = self._client.get(f"{base}/timestamp/{commitment.hex()}", headers=_OTS_HEADERS)
        except httpx.HTTPError as exc:
            raise AnchorUnavailableError(f"{base}: {type(exc).__name__}") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code != 200 or len(resp.content) > MAX_FRAGMENT_BYTES:
            raise AnchorUnavailableError(f"{base}: unexpected reply {resp.status_code}")
        return resp.content


class EsploraHeaders:
    """Bitcoin block headers from an Esplora-style API (``/block-height/``, ``/block/../header``).

    The header is re-hashed and checked against the block id the API named, so a lying explorer
    cannot substitute a different header for that id (it could still lie about the height; the
    operator chooses the source).
    """

    def __init__(self, base_url: str, client: httpx.Client) -> None:
        self._base = base_url.rstrip("/")
        self._client = client

    def header(self, height: int) -> Any:
        try:
            block_id = self._client.get(f"{self._base}/block-height/{int(height)}")
            block_id.raise_for_status()
            bid = block_id.text.strip()
            if not re.fullmatch(r"[0-9a-f]{64}", bid):
                raise AnchorUnavailableError("explorer returned a malformed block id")
            raw = self._client.get(f"{self._base}/block/{bid}/header")
            raw.raise_for_status()
        except httpx.HTTPError as exc:
            raise AnchorUnavailableError(f"block header source: {type(exc).__name__}") from exc
        text = raw.text.strip()
        if not re.fullmatch(r"[0-9a-f]{160}", text):
            raise AnchorUnavailableError("explorer returned a malformed block header")
        header = CBlockHeader.deserialize(bytes.fromhex(text))
        if bytes(header.GetHash())[::-1].hex() != bid:
            raise AnchorBindingError("block header does not hash to the block id")
        return header


class GitAnchorRepo:
    """A dedicated clone whose ``branch`` on ``remote`` receives one manifest file per run.

    ``remote`` and ``branch`` are owner-supplied configuration. Nothing here pushes anywhere else.
    """

    def __init__(
        self,
        workdir: Path,
        remote: str,
        branch: str,
        *,
        author_name: str = "Anchor Bot",
        author_email: str = "anchor@localhost",
        timeout: float = 60.0,
    ) -> None:
        if not remote or remote.startswith("-") or any(c in remote for c in "\n\r\0"):
            raise ValueError("invalid git remote")
        if not _BRANCH.fullmatch(branch) or ".." in branch or branch.endswith((".lock", "/")):
            raise ValueError("invalid git branch name")
        self._dir = workdir
        self._remote = remote
        self._branch = branch
        self._timeout = timeout
        self._env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
        }

    # -- git plumbing ---------------------------------------------------------------------------

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            proc = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self._dir),
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    f"core.hooksPath={os.devnull}",  # no hook of any kind ever runs here
                    *args,
                ],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                env=self._env,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise AnchorUnavailableError(f"git {args[0]}: {type(exc).__name__}") from exc
        if check and proc.returncode != 0:
            detail = proc.stderr.replace(self._remote, redact(self._remote)).strip()[:300]
            raise AnchorUnavailableError(f"git {args[0]} failed: {detail}")
        return proc

    def _ensure_clone(self) -> None:
        if not (self._dir / ".git").exists():
            self._dir.mkdir(parents=True, exist_ok=True)
            self._git("init", "-q", "-b", self._branch)
            self._git("remote", "add", "origin", self._remote)
        # Manifests are compared byte for byte: no line-ending or attribute conversion, ever.
        self._git("config", "core.autocrlf", "false")
        self._git("config", "core.safecrlf", "false")
        self._git("config", "core.attributesFile", os.devnull)

    def _sync(self) -> bool:
        """Fetch the branch and check it out; False when the remote branch does not exist yet."""
        self._ensure_clone()
        listed = self._git("ls-remote", "--heads", "origin", f"refs/heads/{self._branch}")
        if not listed.stdout.strip():
            return False
        self._git("fetch", "-q", "origin", f"refs/heads/{self._branch}")
        self._git("checkout", "-q", "-f", "-B", self._branch, "FETCH_HEAD")
        return True

    # -- the anchor operations ------------------------------------------------------------------

    def anchor(self, files: Mapping[str, bytes]) -> dict[str, str]:
        """Commit and push the manifests; return ``{path: commit}``.

        A manifest already on the branch with identical bytes is reused (its introducing commit is
        returned), so a retry after a crash never creates a second anchor. Same path, different
        bytes is an ``AnchorBindingError``: a manifest is never rewritten.
        """
        for path in files:
            if not _MANIFEST.fullmatch(path):
                raise ValueError(f"not an anchor manifest path: {path!r}")
        self._sync()
        fresh: list[str] = []
        for path, data in files.items():
            target = self._dir / path
            if target.exists():
                if target.read_bytes() != data:
                    raise AnchorBindingError(f"{path} already exists with different content")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            fresh.append(path)
        if fresh:
            self._git("add", "--", *fresh)
            self._git("commit", "-q", "--no-verify", "-m", f"anchor: {len(fresh)} commitment(s)")
            head = self._git("rev-parse", "HEAD").stdout.strip()
            self._git("push", "-q", "--no-verify", "origin", f"HEAD:refs/heads/{self._branch}")
        out: dict[str, str] = {}
        for path in files:
            if path in fresh:
                out[path] = head
            else:
                log = self._git("log", "-n", "1", "--format=%H", "--", path).stdout.strip()
                if not log:
                    raise AnchorBindingError(f"{path} is present but has no commit")
                out[path] = log
        return out

    def is_reachable(self, commit: str) -> bool:
        """True when ``commit`` is an ancestor of the branch head as the remote reports it."""
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            return False
        listed = self._git("ls-remote", "--heads", "origin", f"refs/heads/{self._branch}")
        parts = listed.stdout.split()
        if not parts or not re.fullmatch(r"[0-9a-f]{40}", parts[0]):
            return False  # nothing on the branch, or output we do not recognise from the remote
        head = parts[0]
        self._git("fetch", "-q", "origin", f"refs/heads/{self._branch}")
        known = self._git("cat-file", "-e", f"{commit}^{{commit}}", check=False)
        if known.returncode != 0:
            return False
        return self._git("merge-base", "--is-ancestor", commit, head, check=False).returncode == 0


def remote_is_publicly_readable(remote: str, *, timeout: float = 30.0) -> bool:
    """Anonymous ``git ls-remote``: credentials, helpers and prompts are all disabled.

    A remote that answers this is readable by anyone, which is what an external anchor needs.
    """
    if not remote or remote.startswith("-"):
        return False
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": ""}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    try:
        proc = subprocess.run(
            ["git", "-c", "credential.helper=", "ls-remote", "--", remote],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


__all__ = [
    "EsploraHeaders",
    "GitAnchorRepo",
    "OtsCalendars",
    "manifest_path",
    "redact",
    "remote_is_publicly_readable",
]
