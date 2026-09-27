"""Shared prompt machinery: the universal invariants every system prompt carries (§3.1, §12.1).

A prompt's ``version`` is ``v1-<8 hex of sha256(text)>``, so any wording change changes the
``prompt_version`` stored on the verdict and the cache key (§10.2); it cannot be forgotten.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from contracts.enums import AgentName

FAMILY = "v1"

UNIVERSAL_RULES = """\
Rules that always apply:
1. Answer with one JSON object that matches the response schema. No prose outside the JSON.
2. The entity is anonymous (a token such as TICKER_07). Never guess, name or hint at the real
   company, its ticker, its executives or its products. Never output a calendar date, a ticker,
   a person's name or an absolute currency amount. Refer to time only as offsets in the input
   (for example "t-37d") and to size only in the relative terms the input uses.
3. Use only the input tables. Do not use memory of any real company, price or event. If the
   input does not support a view, say so through data_sufficiency and probabilities near 0.5.
4. key_evidence has 1 to 5 items. Each cites a row exactly as shown: source is the feed the
   row's table belongs to, row_id is the value in the table's evidence_id column. Never cite a
   row that is not in the input. Add a one-sentence note saying what the row shows.
5. Rows are tab-separated. "NA" means not available; do not fill it in.
"""

VOTER_OUTPUT = """\
Output fields:
- stance: strong_sell, sell, hold, buy or strong_buy. Descriptive only.
- p_outperform_5, p_outperform_21, p_outperform_63: your probability, between 0 and 1, that this
  stock's return beats the return of its sector ETF over the next 5, 21 and 63 trading days.
  These are three separate probabilities, not a schedule. Roughly half of stocks beat their
  sector over any horizon, so 0.5 means no edge. Move away from 0.5 only as far as the evidence
  justifies, and do not put probabilities at 0 or 1. Do not repeat one number across all three
  horizons unless the evidence gives no reason to separate them.
- key_evidence: see rule 4.
- risks: up to 3 short phrases naming what would make you wrong.
- data_sufficiency: full when the input covers what your role needs; partial when key parts are
  missing or thin; insufficient when you cannot form a view (then stay near 0.5).
"""

RED_TEAM_OUTPUT = """\
Output fields:
- bear_severity: low, med or high. How damaging the strongest honest bear case is.
- falsifiable_risk: one specific risk that later data could confirm or refute, in one sentence.
- horizon_days: 21 or 63, the horizon over which that risk should show up.
- key_evidence: see rule 4.
You are not asked whether to buy. Argue against a long position with the strongest case the
input supports, and do not invent problems the input does not show. If the input shows little
risk, say low.
"""


@dataclass(frozen=True)
class AgentPrompt:
    agent: AgentName
    system: str

    @property
    def version(self) -> str:
        return f"{FAMILY}-{hashlib.sha256(self.system.encode()).hexdigest()[:8]}"


def build(agent: AgentName, role: str, focus: str) -> AgentPrompt:
    output = RED_TEAM_OUTPUT if agent is AgentName.RED_TEAM else VOTER_OUTPUT
    return AgentPrompt(agent, f"You are the {role}.\n\n{focus}\n\n{UNIVERSAL_RULES}\n{output}")
