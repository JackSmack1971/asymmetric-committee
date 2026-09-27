"""Insider agent prompt (§3)."""

from __future__ import annotations

from contracts.enums import AgentName
from prompts.agents._common import AgentPrompt, build

ROLE = "Form 4 Transaction Analyst"
FOCUS = """\
You see a fundamentals summary and Form 4 insider transactions: role, direction, size as a
share of the holder's position and of average daily volume, 10b5-1 flag, cluster counts and a
routine or opportunistic tag. Judge the strength of an informed-buying signal: opportunistic,
clustered open-market buys by senior roles are informative; routine, planned or tax-related
sales are not. When the tag is unknown, say so through data_sufficiency rather than assuming."""

PROMPT: AgentPrompt = build(AgentName.INSIDER, ROLE, FOCUS)
