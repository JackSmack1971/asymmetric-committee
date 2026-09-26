"""CIO governance: veto budget, weight immutability, cash routing, strong tier (§8.2)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from agents.base import ChatResponse, LLMTransientError, Message
from agents.cio import (
    CioMiscalibrationAlert,
    CioTierError,
    apply_cio_decision,
    max_vetoes,
    run_cio,
)
from config.loader import load_config
from contracts.enums import ModelTier
from contracts.models import CioDecisionLLM, ProposedBook, ProposedPosition

MODELS = load_config(allow_placeholders=True, env={}).models
STRONG = MODELS.tiers[ModelTier.STRONG].primary
FAST = MODELS.tiers[ModelTier.FAST].primary
USAGE = {"prompt_tokens": 1000, "completion_tokens": 200}
NOW = datetime(2026, 1, 30, tzinfo=UTC)


def _book(n: int = 8, weight: float = 0.08) -> ProposedBook:
    return ProposedBook(
        run_id=uuid4(),
        as_of=NOW,
        positions=tuple(
            ProposedPosition(
                security_id=i + 1,
                entity_token=f"TICKER_{i:02d}",
                sector="tech",
                pooled_p=0.6,
                target_weight=weight,
            )
            for i in range(n)
        ),
    )


def _decision(book: ProposedBook, vetoes: Sequence[str] = ()) -> CioDecisionLLM:
    return CioDecisionLLM.model_validate(
        {
            "decisions": [
                {
                    "entity_token": p.entity_token,
                    "action": "veto" if p.entity_token in vetoes else "approve",
                    "reason": "bear case confirmed" if p.entity_token in vetoes else "",
                }
                for p in book.positions
            ],
            "rationale": "book reviewed",
        }
    )


def test_veto_budget_is_floor() -> None:
    assert max_vetoes(8, 0.20) == 1
    assert max_vetoes(5, 0.20) == 1  # 5 * 0.2 is 1.0000000000000002 in floats
    assert max_vetoes(4, 0.20) == 0


def test_over_budget_raises_and_leaves_book_untouched() -> None:
    book = _book()
    with pytest.raises(CioMiscalibrationAlert) as err:
        apply_cio_decision(book, _decision(book, ["TICKER_01", "TICKER_02"]), max_veto_pct=0.20)
    assert err.value.book is book
    assert (err.value.vetoes, err.value.limit) == (2, 1)


def test_valid_veto_routes_weight_to_cash_only() -> None:
    book = _book()
    assert book.cash_weight == pytest.approx(0.36)
    final = apply_cio_decision(book, _decision(book, ["TICKER_01"]), max_veto_pct=0.20)
    assert "TICKER_01" not in {p.entity_token for p in final.positions}
    assert final.cash_weight == pytest.approx(0.44)
    # Invariant 6: every surviving weight is bit-identical, nothing was redistributed.
    before = {p.entity_token: p.target_weight for p in book.positions}
    assert all(before[p.entity_token] == p.target_weight for p in final.positions)


def test_flag_for_review_keeps_the_name() -> None:
    book = _book()
    raw = _decision(book).model_dump(mode="json")
    raw["decisions"][0].update(action="flag_for_review", reason="check liquidity")
    final = apply_cio_decision(book, CioDecisionLLM.model_validate(raw), max_veto_pct=0.20)
    assert final.positions == book.positions


@pytest.mark.parametrize("drop", [True, False])
def test_decisions_must_cover_exactly_the_book(drop: bool) -> None:
    book = _book()
    raw = _decision(book).model_dump(mode="json")
    if drop:
        raw["decisions"].pop()
    else:
        raw["decisions"][0]["entity_token"] = "TICKER_99"
    with pytest.raises(ValueError, match="cover exactly"):
        apply_cio_decision(book, CioDecisionLLM.model_validate(raw), max_veto_pct=0.20)


class FakeClient:
    def __init__(self, *replies: str | Exception, model: str = STRONG.slug) -> None:
        self.replies, self.model, self.calls = list(replies), model, 0

    def complete(self, messages: Sequence[Message]) -> ChatResponse:
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return ChatResponse(self.model, reply, USAGE)


class ListDlq:
    def __init__(self) -> None:
        self.records: list[dict[str, str]] = []

    def push(self, record: dict[str, str]) -> None:
        self.records.append(record)


def _run(client: FakeClient, book: ProposedBook, dlq: ListDlq | None = None) -> Any:
    return run_cio(client, book, models=MODELS, run_id=book.run_id, max_veto_pct=0.20, dlq=dlq)


def test_run_cio_veto_logs_served_model() -> None:
    book = _book()
    res = _run(FakeClient(_decision(book, ["TICKER_03"]).model_dump_json()), book)
    assert res.decision is not None and res.decision.model_served == STRONG.slug
    assert res.cost_usd > 0
    assert len(res.book.positions) == 7 and res.alert is None


def test_run_cio_alert_keeps_book_and_dead_letters() -> None:
    book, dlq = _book(), ListDlq()
    reply = _decision(book, ["TICKER_01", "TICKER_02"]).model_dump_json()
    res = _run(FakeClient(reply), book, dlq)
    assert res.book is book and isinstance(res.alert, CioMiscalibrationAlert)
    assert [r["error"] for r in dlq.records] == ["cio_miscalibration"]


def test_run_cio_repairs_once_then_dead_letters() -> None:
    book, dlq = _book(), ListDlq()
    good = _decision(book).model_dump_json()
    ok = _run(FakeClient("not json", good), book, dlq)
    assert ok.decision is not None and not dlq.records
    bad = FakeClient("not json", "still not json")
    res = _run(bad, book, dlq)
    assert bad.calls == 2 and res.book is book and res.dead_lettered
    assert [r["error"] for r in dlq.records] == ["unparseable"]


def test_run_cio_transport_failure_keeps_book() -> None:
    book, dlq = _book(), ListDlq()
    res = _run(FakeClient(LLMTransientError("503")), book, dlq)
    assert res.book is book and res.dead_lettered and len(dlq.records) == 1


def test_fast_tier_answer_is_refused() -> None:
    book = _book()
    with pytest.raises(CioTierError):
        _run(FakeClient(_decision(book).model_dump_json(), model=FAST.slug), book)


def test_prompt_and_input_carry_no_weight_instruction_or_identity() -> None:
    from agents.cio import render_input
    from prompts.agents.cio import PROMPT

    assert "cannot change" in PROMPT.system and PROMPT.version.startswith("v1-")
    table = render_input(_book(2), {})
    assert table.splitlines()[1].startswith("TICKER_00\ttech\t0.600\t0.080\tNA")
