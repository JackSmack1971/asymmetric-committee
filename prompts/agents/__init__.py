"""One prompt module per agent (§3); ``PROMPTS`` is the registry the runner uses."""

from __future__ import annotations

from contracts.enums import AgentName
from prompts.agents import insider, macro_narrative, quality_catalyst, red_team, technical, value
from prompts.agents._common import AgentPrompt

PROMPTS: dict[AgentName, AgentPrompt] = {
    m.PROMPT.agent: m.PROMPT
    for m in (value, quality_catalyst, insider, technical, macro_narrative, red_team)
}

__all__ = ["PROMPTS", "AgentPrompt"]
