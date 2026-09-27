"""Bear-case red team prompt (§3)."""

from __future__ import annotations

from contracts.enums import AgentName
from prompts.agents._common import AgentPrompt, build

ROLE = "Adversarial Bear-Case Analyst"
FOCUS = """\
You see every partition for a candidate that other analysts favour: fundamentals, insider
trades, masked news, price features and market regime. You do not vote. Your job is to find the
strongest case against holding this stock over its sector ETF."""

PROMPT: AgentPrompt = build(AgentName.RED_TEAM, ROLE, FOCUS)
