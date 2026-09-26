"""FastAPI control and read API (§11).

Every ``/runs`` route needs ``Authorization: Bearer $CONTROL_API_TOKEN``. With no token configured
the routes answer 503 rather than run open: an unauthenticated mutation endpoint is not acceptable.
``POST /runs/{id}/reset`` may only reset a run that never committed; anything holding a commitment,
anchor, order or halt event is refused with 409 and nothing is cleared.
"""

from __future__ import annotations

import hmac
import os
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import create_engine

from contracts.errors import ResetRefusedError
from contracts.models import RunRecord
from orchestration.sink import DecisionSink

app = FastAPI(title="Asymmetric Committee")


class ResetRequest(BaseModel):
    """Body of ``POST /runs/{id}/reset``: who did it and why, both kept in the audit row."""

    model_config = ConfigDict(extra="forbid")

    actor: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=1000)


@lru_cache(maxsize=1)
def get_sink() -> DecisionSink:
    return DecisionSink(create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True))


def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
    expected = os.environ.get("CONTROL_API_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="control API is not configured")
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme != "Bearer" or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(
            status_code=401,
            detail="invalid or missing token",
            headers={"WWW-Authenticate": "Bearer"},
        )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/runs/{run_id}", dependencies=[Depends(require_token)])
def get_run(run_id: UUID, sink: Annotated[DecisionSink, Depends(get_sink)]) -> RunRecord:
    run = sink.load_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="no such run")
    return run


@app.post("/runs/{run_id}/reset", dependencies=[Depends(require_token)])
def reset_run(
    run_id: UUID, body: ResetRequest, sink: Annotated[DecisionSink, Depends(get_sink)]
) -> dict[str, object]:
    if sink.load_run(run_id) is None:
        raise HTTPException(status_code=404, detail="no such run")
    try:
        cleared = sink.reset_run(
            run_id, actor=body.actor, reason=body.reason, now=datetime.now(UTC)
        )
    except ResetRefusedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"run_id": str(run_id), "status": "PENDING", "cleared": cleared}
