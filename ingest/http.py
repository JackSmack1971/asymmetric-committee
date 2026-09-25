"""Shared HTTP plumbing: record real responses once, replay them in tests (no network in tests).

A fixture file lives at ``<dir>/<host>/<path>`` with ``__<hash>`` appended when the request has a
query string (secrets such as ``apikey`` are left out of the hash). ``RecordingTransport`` writes
successful responses; ``ReplayTransport`` serves them and fails loudly on anything unrecorded.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlencode

import httpx

SECRET_PARAMS = frozenset({"apikey", "api_key", "token"})


class FixtureMissingError(LookupError):
    pass


def fixture_path(root: Path, url: httpx.URL | str) -> Path:
    url = httpx.URL(str(url))
    path = url.path.strip("/") or "index"
    params = sorted((k, v) for k, v in url.params.multi_items() if k.lower() not in SECRET_PARAMS)
    if params:
        path += "__" + hashlib.sha256(urlencode(params).encode()).hexdigest()[:12]
    return root / url.host / path


class ReplayTransport(httpx.BaseTransport):
    def __init__(self, root: Path) -> None:
        self.root = root

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = fixture_path(self.root, request.url)
        if not path.is_file():
            raise FixtureMissingError(f"no recorded response for {request.url} (expected {path})")
        return httpx.Response(200, content=path.read_bytes(), request=request)


class RecordingTransport(httpx.BaseTransport):
    def __init__(self, root: Path, inner: httpx.BaseTransport | None = None) -> None:
        self.root = root
        self.inner = inner or httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self.inner.handle_request(request)
        if response.status_code == 200:
            body = response.read()
            path = fixture_path(self.root, request.url)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        return response
