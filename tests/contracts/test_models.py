"""Round-trip, extra-field and range tests for every contract model (invariant 1)."""

from __future__ import annotations

import inspect
import typing
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from contracts import models as m
from contracts.enums import AgentName, CioAction, DataSufficiency, FeedName, Horizon, Stance
from tests.contracts.strategies import STRATEGIES

ALL_MODELS = sorted(
    (
        obj
        for obj in vars(m).values()
        if inspect.isclass(obj) and issubclass(obj, m.Contract) and obj is not m.Contract
    ),
    key=lambda c: c.__name__,
)
IDS = [c.__name__ for c in ALL_MODELS]


def test_every_model_has_a_strategy() -> None:
    assert set(ALL_MODELS) == set(STRATEGIES)


@pytest.mark.parametrize("model", ALL_MODELS, ids=IDS)
def test_config_is_strict(model: type[BaseModel]) -> None:
    assert model.model_config.get("extra") == "forbid"
    assert model.model_config.get("frozen") is True


@pytest.mark.parametrize("model", ALL_MODELS, ids=IDS)
@given(data=st.data())
def test_round_trip(model: type[m.Contract], data: st.DataObject) -> None:
    obj = data.draw(STRATEGIES[model])
    assert type(obj) is model
    assert model.model_validate_json(obj.model_dump_json()) == obj
    assert model.model_validate(obj.model_dump()) == obj


@pytest.mark.parametrize("model", ALL_MODELS, ids=IDS)
@given(data=st.data())
def test_extra_field_rejected(model: type[m.Contract], data: st.DataObject) -> None:
    payload = data.draw(STRATEGIES[model]).model_dump(mode="json")
    payload["unexpected"] = 1
    with pytest.raises(ValidationError, match="extra_forbidden"):
        model.model_validate(payload)


@pytest.mark.parametrize("model", ALL_MODELS, ids=IDS)
@given(data=st.data())
def test_frozen(model: type[m.Contract], data: st.DataObject) -> None:
    obj = data.draw(STRATEGIES[model])
    field = next(iter(model.model_fields))
    with pytest.raises(ValidationError):
        setattr(obj, field, getattr(obj, field))


def _strings_in_literals(tp: Any) -> bool:
    if typing.get_origin(tp) is typing.Literal:
        return any(type(a) is str for a in typing.get_args(tp))
    return any(_strings_in_literals(a) for a in typing.get_args(tp))


@pytest.mark.parametrize("model", ALL_MODELS, ids=IDS)
def test_no_bare_string_categoricals(model: type[BaseModel]) -> None:
    for name, field in model.model_fields.items():
        assert not _strings_in_literals(field.annotation), f"{model.__name__}.{name}"


# --- explicit rejections ---------------------------------------------------------------------

NOW = datetime(2026, 9, 25, 20, 0, tzinfo=UTC)
EVIDENCE = {"source": FeedName.FUNDAMENTALS, "row_id": "f:123", "note": "FCF yield 9%"}
VERDICT: dict[str, Any] = {
    "run_id": uuid4(),
    "agent": AgentName.VALUE,
    "entity_token": "TICKER_07",
    "as_of": NOW,
    "stance": Stance.BUY,
    "p_outperform": 0.6,
    "horizon_days": Horizon.D21,
    "key_evidence": [EVIDENCE],
    "risks": [],
    "data_sufficiency": DataSufficiency.FULL,
    "prompt_version": "v1",
    "model_served": "vendor/model-2026-01",
    "valid": True,
}


def test_valid_verdict_fixture() -> None:
    m.AgentVerdict.model_validate(VERDICT)


def test_verdict_requires_valid_flag() -> None:
    # System-owned and fail-closed: a producer that omits `valid` must not validate.
    payload = {k: v for k, v in VERDICT.items() if k != "valid"}
    with pytest.raises(ValidationError, match="valid"):
        m.AgentVerdict.model_validate(payload)


@pytest.mark.parametrize(
    "patch",
    [
        {"p_outperform": 1.01},
        {"p_outperform": -0.01},
        {"p_outperform": float("nan")},
        {"horizon_days": 42},
        {"stance": "very_bullish"},
        {"data_sufficiency": "some"},
        {"agent": AgentName.RED_TEAM},
        {"agent": AgentName.CIO},
        {"key_evidence": []},
        {"key_evidence": [EVIDENCE] * 6},
        {"risks": ["a", "b", "c", "d"]},
        {"entity_token": "NVDA"},
        {"as_of": datetime(2026, 9, 25)},  # naive
        {"model_served": ""},
    ],
    ids=lambda p: next(iter(p)),
)
def test_verdict_out_of_range_rejected(patch: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        m.AgentVerdict.model_validate({**VERDICT, **patch})


def test_evidence_source_must_be_enum() -> None:
    with pytest.raises(ValidationError):
        m.EvidenceRef.model_validate({**EVIDENCE, "source": "memory"})


COMMITTEE: dict[str, Any] = {
    "run_id": uuid4(),
    "security_id": 1,
    "entity_token": "TICKER_07",
    "as_of": NOW,
    "horizon_days": 21,
    "pooled_p": 0.6,
    "dispersion": 0.3,
    "agent_weights": [{"agent": "value", "weight": 1.0}],
    "bear_severity": None,
    "target_weight": 0.05,
}


@pytest.mark.parametrize(
    "patch",
    [
        {"target_weight": m.MAX_POSITION + 1e-9},
        {"target_weight": -0.01},
        {"pooled_p": 1.5},
        {"dispersion": -1.0},
        {"agent_weights": []},
        {"agent_weights": [{"agent": "red_team", "weight": 1.0}]},
        {"agent_weights": [{"agent": "value", "weight": 1.0}] * 2},
        {"agent_weights": [{"agent": "value", "weight": -1.0}]},
        {"bear_severity": "extreme"},
    ],
    ids=str,
)
def test_committee_out_of_range_rejected(patch: dict[str, Any]) -> None:
    m.CommitteeDecision.model_validate(COMMITTEE)
    with pytest.raises(ValidationError):
        m.CommitteeDecision.model_validate({**COMMITTEE, **patch})


def _position(i: int, w: float) -> dict[str, Any]:
    return {
        "security_id": i,
        "entity_token": f"TICKER_{i:02d}",
        "sector": "Semis",
        "pooled_p": 0.6,
        "target_weight": w,
    }


def test_book_cash_is_residual() -> None:
    book = m.ProposedBook.model_validate(
        {"run_id": uuid4(), "as_of": NOW, "positions": [_position(1, 0.08), _position(2, 0.02)]}
    )
    assert book.gross_exposure == pytest.approx(0.10)
    assert book.cash_weight == pytest.approx(0.90)


@pytest.mark.parametrize(
    "positions",
    [
        [_position(1, 0.05), _position(1, 0.05)],
        [_position(i, 0.08) for i in range(1, 14)],  # 1.04 gross
    ],
    ids=["duplicate", "gross>1"],
)
def test_book_rejects(positions: list[dict[str, Any]]) -> None:
    with pytest.raises(ValidationError):
        m.ProposedBook.model_validate({"run_id": uuid4(), "as_of": NOW, "positions": positions})


@pytest.mark.parametrize("action", [CioAction.VETO, CioAction.FLAG_FOR_REVIEW])
def test_cio_veto_requires_reason(action: CioAction) -> None:
    with pytest.raises(ValidationError, match="requires a reason"):
        m.CioNameDecisionLLM(entity_token="TICKER_01", action=action, reason="  ")


def test_cio_duplicate_names_rejected() -> None:
    d = m.CioNameDecisionLLM(entity_token="TICKER_01", action=CioAction.APPROVE, reason="")
    with pytest.raises(ValidationError, match="duplicate"):
        m.CioDecisionLLM(decisions=(d, d), rationale="x")


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (m.DecisionCommitment, {"run_id": uuid4(), "sha256": "ABC", "committed_at": NOW}),
        (
            m.OutcomeRecord,
            {
                "run_id": uuid4(),
                "security_id": 1,
                "horizon": 21,
                "fwd_return": -1.5,
                "sector_fwd_return": 0.0,
                "scored_at": NOW,
            },
        ),
        (m.TaskKey, {"run_id": uuid4(), "stage": "agents", "security_id": 0}),
        (
            m.RunRecord,
            {
                "run_id": uuid4(),
                "mode": "paper",
                "as_of": NOW,
                "config_hash": "0" * 64,
                "status": "PENDING",
                "started_at": NOW,
            },
        ),
    ],
    ids=lambda x: x.__name__ if isinstance(x, type) else "",
)
def test_misc_rejections(model: type[BaseModel], payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_run_record_end_before_start_rejected() -> None:
    with pytest.raises(ValidationError, match="ended_at"):
        m.RunRecord(
            run_id=uuid4(),
            mode="live",  # type: ignore[arg-type]
            as_of=NOW,
            config_hash="0" * 64,
            status="FAILED",  # type: ignore[arg-type]
            started_at=NOW,
            ended_at=NOW.replace(year=2025),
        )
