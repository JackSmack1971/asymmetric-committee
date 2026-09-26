"""Control API: token auth fails closed; reset is pre-commitment only (service-gated)."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from api.main import app, get_sink
from contracts.enums import RunStatus
from orchestration.sink import DecisionSink
from tests.orchestration.test_sink import run_record, step
from tests.store import factories as f

TOKEN = "s3cret-token-value"


@pytest.fixture
def engine(pg_engine: Engine) -> Iterator[Engine]:
    def wipe() -> None:
        with pg_engine.begin() as c:
            c.execute(text("TRUNCATE runs, securities RESTART IDENTITY CASCADE"))

    wipe()
    yield pg_engine
    wipe()


@pytest.fixture
def client(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CONTROL_API_TOKEN", TOKEN)
    app.dependency_overrides[get_sink] = lambda: DecisionSink(engine)
    yield TestClient(app)
    app.dependency_overrides.clear()


AUTH = {"Authorization": f"Bearer {TOKEN}"}
BODY = {"actor": "operator", "reason": "bad partition"}


def test_health_is_open() -> None:
    assert TestClient(app).get("/health").json() == {"status": "ok"}


def test_without_a_configured_token_the_routes_are_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTROL_API_TOKEN", raising=False)
    c = TestClient(app)
    assert c.get(f"/runs/{uuid4()}", headers=AUTH).status_code == 503
    assert c.post(f"/runs/{uuid4()}/reset", json=BODY, headers=AUTH).status_code == 503


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}, {"Authorization": "Bearer "}],
)
def test_a_missing_or_wrong_token_is_401_and_nothing_is_cleared(
    client: TestClient, engine: Engine, headers: dict[str, str]
) -> None:
    sink = DecisionSink(engine)
    with engine.begin() as c:
        sid = f.security(c)
    run_id = uuid4()
    sink.flush_step(step(run_id, sid, commitment=None, kill_switch=()))
    resp = client.post(f"/runs/{run_id}/reset", json=BODY, headers=headers)
    assert resp.status_code == 401 and resp.headers["www-authenticate"] == "Bearer"
    assert client.get(f"/runs/{run_id}", headers=headers).status_code == 401
    assert len(sink.load_verdicts(run_id)) == 2  # untouched


def test_reset_clears_an_uncommitted_run(client: TestClient, engine: Engine) -> None:
    sink = DecisionSink(engine)
    with engine.begin() as c:
        sid = f.security(c)
    run_id = uuid4()
    sink.flush_step(step(run_id, sid, commitment=None, kill_switch=()))
    got = client.get(f"/runs/{run_id}", headers=AUTH)
    assert got.status_code == 200 and got.json()["status"] == "AGENTS_OK"

    resp = client.post(f"/runs/{run_id}/reset", json=BODY, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["cleared"]["agent_verdicts"] == 2 and resp.json()["status"] == "PENDING"
    assert sink.load_verdicts(run_id) == []
    run = sink.load_run(run_id)
    assert run is not None and run.status is RunStatus.PENDING


def test_reset_of_a_committed_run_is_409_and_clears_nothing(
    client: TestClient, engine: Engine
) -> None:
    sink = DecisionSink(engine)
    with engine.begin() as c:
        sid = f.security(c)
    run_id = uuid4()
    run = run_record(run_id, RunStatus.COMMITTED)
    sink.flush_step(step(run_id, sid, run=run, kill_switch=()))
    resp = client.post(f"/runs/{run_id}/reset", json=BODY, headers=AUTH)
    assert resp.status_code == 409 and "immutable" in resp.json()["detail"]
    assert len(sink.load_verdicts(run_id)) == 2 and sink.load_commitment_material(run_id)


def test_unknown_runs_and_bad_bodies(client: TestClient) -> None:
    assert client.get(f"/runs/{uuid4()}", headers=AUTH).status_code == 404
    assert client.post(f"/runs/{uuid4()}/reset", json=BODY, headers=AUTH).status_code == 404
    assert (
        client.post(f"/runs/{uuid4()}/reset", json={"actor": "x"}, headers=AUTH).status_code == 422
    )
    extra = {**BODY, "force": True}
    assert client.post(f"/runs/{uuid4()}/reset", json=extra, headers=AUTH).status_code == 422
    assert client.get("/runs/not-a-uuid", headers=AUTH).status_code == 422
