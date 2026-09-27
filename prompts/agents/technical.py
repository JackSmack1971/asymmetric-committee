"""Technical agent prompt (§3)."""

from __future__ import annotations

from contracts.enums import AgentName
from prompts.agents._common import AgentPrompt, build

ROLE = "Trend and Volatility Analyst"
FOCUS = """\
You see engineered, stationary price features only: distances to moving averages, momentum,
realized volatility, drawdown, volume z-scores and cross-sectional percentiles. You never see
prices or candles. Judge trend continuation, volatility compression or expansion and momentum
relative to the cross-section. Recent strength is not a reason for high probabilities by
itself; short-horizon reversal and long-horizon momentum can point in different directions."""

PROMPT: AgentPrompt = build(AgentName.TECHNICAL, ROLE, FOCUS)
