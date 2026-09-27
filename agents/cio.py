"""CIO veto stage (§8.2): approve / veto / flag per proposed name, weights never touched.

``apply_cio_decision`` is the pure governance rule; ``run_cio`` adds the strong-tier LLM call.
A veto removes the position, so its weight falls into residual cash (``ProposedBook.cash_weight``)
and is never redistributed. More than ``floor(M x budget)`` vetoes is a
``CioMiscalibrationAlert``: all vetoes are discarded and the proposed book stands.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from pydantic import ValidationError

from agents.base import (
    BudgetExceededError,
    ChatClient,
    DeadLetterQueue,
    LLMTransientError,
    Message,
    RunBudget,
    UnknownServedModelError,
)
from config.loader import ModelsConfig
from contracts.enums import AgentName, CioAction, ModelTier
from contracts.models import (
    CioDecision,
    CioDecisionLLM,
    ProposedBook,
    RedTeamVerdict,
)
from prompts.agents.cio import PROMPT


class CioMiscalibrationAlert(RuntimeError):
    """The CIO vetoed more names than the budget allows (§8.2). ``book`` is the untouched input."""

    def __init__(self, vetoes: int, limit: int, book: ProposedBook) -> None:
        super().__init__(f"CIO issued {vetoes} vetoes, budget allows {limit}")
        self.vetoes, self.limit, self.book = vetoes, limit, book


class CioTierError(RuntimeError):
    """The CIO was answered by a model outside the strong tier (§3): fast-tier CIO is prohibited."""


def max_vetoes(names: int, budget: float) -> int:
    """``floor(M x budget)``; the epsilon absorbs float error (5 x 0.2 = 1.0000000000000002)."""
    return math.floor(names * budget + 1e-9)


def _check_coverage(book: ProposedBook, decision: CioDecisionLLM) -> None:
    expected = {p.entity_token for p in book.positions}
    got = {d.entity_token for d in decision.decisions}
    if got != expected:
        raise ValueError(
            f"decisions must cover exactly the proposed names: missing {sorted(expected - got)}, "
            f"unknown {sorted(got - expected)}"
        )


def apply_cio_decision(
    book: ProposedBook, decision: CioDecisionLLM, *, max_veto_pct: float
) -> ProposedBook:
    """The approved book: vetoed names removed, every other weight unchanged.

    Raises ``ValueError`` if the decision does not cover exactly the book's names, and
    ``CioMiscalibrationAlert`` (carrying the untouched ``book``) if vetoes exceed the budget.
    """
    _check_coverage(book, decision)
    vetoed = {d.entity_token for d in decision.decisions if d.action is CioAction.VETO}
    limit = max_vetoes(len(book.positions), max_veto_pct)
    if len(vetoed) > limit:
        raise CioMiscalibrationAlert(len(vetoed), limit, book)
    kept = tuple(p for p in book.positions if p.entity_token not in vetoed)
    return ProposedBook(run_id=book.run_id, as_of=book.as_of, positions=kept)


def render_input(book: ProposedBook, red_team: Mapping[str, RedTeamVerdict]) -> str:
    """Anonymous tab-separated table: the proposed book, committee p and the red-team note."""
    rows = ["entity_token\tsector\tpooled_p\tweight\tbear_severity\tfalsifiable_risk"]
    for p in book.positions:
        rt = red_team.get(p.entity_token)
        bear = (rt.bear_severity.value, rt.falsifiable_risk) if rt else ("NA", "NA")
        rows.append(
            f"{p.entity_token}\t{p.sector}\t{p.pooled_p:.3f}\t{p.target_weight:.3f}\t"
            f"{bear[0]}\t{bear[1]}"
        )
    return "\n".join(rows)


@dataclass(frozen=True)
class CioResult:
    """``book`` proceeds to execution; ``decision`` is what was logged (None if no usable reply)."""

    book: ProposedBook
    decision: CioDecision | None
    alert: CioMiscalibrationAlert | None = None
    cost_usd: float = 0.0
    dead_lettered: bool = False


def run_cio(
    client: ChatClient,
    book: ProposedBook,
    *,
    models: ModelsConfig,
    run_id: UUID,
    red_team: Mapping[str, RedTeamVerdict] = {},
    max_veto_pct: float,
    dlq: DeadLetterQueue | None = None,
    budget: RunBudget | None = None,
) -> CioResult:
    """One strong-tier call plus at most one repair; on any failure the proposed book stands.

    ``client`` must be built for the strong tier; a reply from a model outside that tier raises
    ``CioTierError``. A miscalibration alert or dead-lettered call keeps the proposed weights and
    pushes a DLQ record.
    """
    if not book.positions:
        return CioResult(book, None)
    strong = set(models.tiers[ModelTier.STRONG].all_slugs)
    messages: list[Message] = [
        {"role": "system", "content": PROMPT.system},
        {"role": "user", "content": render_input(book, red_team)},
    ]
    cost = 0.0

    def dead_letter(error: str) -> None:
        if dlq is not None:
            dlq.push(
                {
                    "run_id": str(run_id),
                    "agent": AgentName.CIO.value,
                    "entity_token": "*",
                    "prompt_version": PROMPT.version,
                    "error": error,
                }
            )

    for _ in range(2):
        if budget is not None and budget.exceeded:
            dead_letter("budget_exceeded")
            raise BudgetExceededError("run budget already exhausted")
        try:
            response = client.complete(messages)
        except LLMTransientError as e:
            dead_letter(str(e))
            return CioResult(book, None, cost_usd=cost, dead_lettered=True)
        try:
            served = models.model_for_response(response.model)
        except ValueError as e:
            raise UnknownServedModelError(str(e)) from e
        if served.slug not in strong:
            raise CioTierError(f"CIO answered by {served.slug}, not a strong-tier model")
        call_usd = served.call_cost(response.usage).usd
        cost += call_usd
        if budget is not None:
            try:
                budget.charge(call_usd)
            except BudgetExceededError:
                dead_letter("budget_exceeded")
                raise
        try:
            out = CioDecisionLLM.model_validate(json.loads(response.content))
            approved = apply_cio_decision(book, out, max_veto_pct=max_veto_pct)
        except CioMiscalibrationAlert as alert:
            dead_letter("cio_miscalibration")
            return CioResult(book, _envelope(out, book, served.slug), alert, cost)
        except (json.JSONDecodeError, ValidationError, ValueError) as e:
            messages = [
                *messages,
                {"role": "assistant", "content": response.content},
                {
                    "role": "user",
                    "content": f"Your previous reply was rejected: {e}. Reply again with JSON "
                    "that matches the schema and covers exactly the proposed names.",
                },
            ]
            continue
        return CioResult(approved, _envelope(out, book, served.slug), cost_usd=cost)
    dead_letter("unparseable")
    return CioResult(book, None, cost_usd=cost, dead_lettered=True)


def _envelope(out: CioDecisionLLM, book: ProposedBook, served: str) -> CioDecision:
    return CioDecision.from_llm(
        out,
        run_id=book.run_id,
        as_of=book.as_of,
        prompt_version=PROMPT.version,
        model_served=served,  # invariant 7: response.model, not the requested slug
    )
