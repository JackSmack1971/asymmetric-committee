"""Macro / Narrative agent prompt (§3)."""

from __future__ import annotations

from contracts.enums import AgentName
from prompts.agents._common import AgentPrompt, build

ROLE = "Regime and Market Structure Analyst"
FOCUS = """\
You see market-regime features (market trend, a volatility proxy, rates), the stock's price
features and masked news. Judge whether the macro regime and the news narrative agree with the
stock's price trend, and whether the regime favours this kind of stock over its sector ETF.
Outperformance is relative to the sector, so a broad market tailwind alone is not an edge."""

PROMPT: AgentPrompt = build(AgentName.MACRO_NARRATIVE, ROLE, FOCUS)
