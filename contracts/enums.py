"""Shared categorical types. Producers and consumers import these; never copy the strings (§1.3)."""

from __future__ import annotations

from collections.abc import Mapping
from enum import IntEnum, StrEnum
from itertools import pairwise


class AgentName(StrEnum):
    VALUE = "value"
    QUALITY_CATALYST = "quality_catalyst"
    INSIDER = "insider"
    TECHNICAL = "technical"
    MACRO_NARRATIVE = "macro_narrative"
    RED_TEAM = "red_team"
    CIO = "cio"
    QUANT_BASELINE = "quant_baseline"


# Agents whose p_outperform enters the committee pool (§7). Red team and CIO do not vote.
VOTING_AGENTS: frozenset[AgentName] = frozenset(
    {
        AgentName.VALUE,
        AgentName.QUALITY_CATALYST,
        AgentName.INSIDER,
        AgentName.TECHNICAL,
        AgentName.MACRO_NARRATIVE,
    }
)


class Stance(StrEnum):
    STRONG_SELL = "strong_sell"
    SELL = "sell"
    HOLD = "hold"
    BUY = "buy"
    STRONG_BUY = "strong_buy"


class RunMode(StrEnum):
    LIVE = "live"
    BACKTEST = "backtest"
    ABLATION = "ablation"


class RunStatus(StrEnum):
    """Run state machine (§11)."""

    PENDING = "PENDING"
    INGEST_OK = "INGEST_OK"
    FEATURES_OK = "FEATURES_OK"
    GATED = "GATED"
    AGENTS_OK = "AGENTS_OK"
    COMMITTED = "COMMITTED"
    EXECUTED = "EXECUTED"
    SCORED = "SCORED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"


_RUN_PATH = (
    RunStatus.PENDING,
    RunStatus.INGEST_OK,
    RunStatus.FEATURES_OK,
    RunStatus.GATED,
    RunStatus.AGENTS_OK,
    RunStatus.COMMITTED,
    RunStatus.EXECUTED,
    RunStatus.SCORED,
)
TERMINAL_RUN_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.SCORED, RunStatus.FAILED, RunStatus.PARTIAL}
)
# Each non-terminal state may advance one step, or end in FAILED / PARTIAL.
RUN_TRANSITIONS: Mapping[RunStatus, frozenset[RunStatus]] = {
    **{
        cur: frozenset({nxt, RunStatus.FAILED, RunStatus.PARTIAL})
        for cur, nxt in pairwise(_RUN_PATH)
    },
    **{s: frozenset() for s in TERMINAL_RUN_STATUSES},
}


def can_transition(src: RunStatus, dst: RunStatus) -> bool:
    return dst in RUN_TRANSITIONS[src]


class Stage(StrEnum):
    """Pipeline stage; part of the idempotency key (invariant 8)."""

    INGEST = "ingest"
    FEATURES = "features"
    GATE = "gate"
    AGENTS = "agents"
    COMMITTEE = "committee"
    RISK = "risk"
    CIO = "cio"
    COMMIT = "commit"
    EXECUTE = "execute"
    SCORE = "score"


class TaskStatus(StrEnum):
    """Per-task status. Only COMPLETED short-circuits (invariant 8)."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"


class DataSufficiency(StrEnum):
    FULL = "full"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"


class BearSeverity(StrEnum):
    LOW = "low"
    MED = "med"
    HIGH = "high"


class CioAction(StrEnum):
    APPROVE = "approve"
    VETO = "veto"
    FLAG_FOR_REVIEW = "flag_for_review"


class Horizon(IntEnum):
    """Scoring horizon in trading days (§3.1, §18.3)."""

    D21 = 21
    D63 = 63


class FeedName(StrEnum):
    """Data feeds (§4.1). Also names the input partition an evidence row came from."""

    PRICE_BARS = "price_bars"
    FUNDAMENTALS = "fundamentals"
    INSIDER_TRADES = "insider_trades"
    NEWS = "news"
    REGIME = "regime"
    FEATURES = "features"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class ModelTier(StrEnum):
    """LLM tiers (§10.1)."""

    FAST = "fast"
    STRONG = "strong"
    PROBE = "probe"


class PriceFeed(StrEnum):
    """Alpaca bar feed (§4.1). IEX is the free live feed; SIP is used for history > 15 min old."""

    IEX = "iex"
    SIP = "sip"


class InsiderRole(StrEnum):
    """Reporting-owner relationship on Form 4, most senior first."""

    OFFICER = "officer"
    DIRECTOR = "director"
    TEN_PERCENT_OWNER = "ten_percent_owner"
    OTHER = "other"


class InsiderTxnCode(StrEnum):
    """Form 4 transaction codes (SEC Form 4 General Instructions, item 8)."""

    P = "P"  # open-market purchase
    S = "S"  # open-market sale
    A = "A"  # grant or award
    D = "D"  # disposition to the issuer
    F = "F"  # tax withholding
    I = "I"  # discretionary  # noqa: E741
    M = "M"  # option exercise
    C = "C"  # conversion
    E = "E"  # expiration of short derivative
    H = "H"  # expiration of long derivative
    O = "O"  # out-of-the-money exercise  # noqa: E741
    X = "X"  # in-the-money exercise
    G = "G"  # gift
    L = "L"  # small acquisition
    W = "W"  # will or laws of descent
    Z = "Z"  # voting trust
    J = "J"  # other
    K = "K"  # equity swap
    U = "U"  # tender of shares
    V = "V"  # voluntarily reported early


class NewsProviderName(StrEnum):
    """`NEWS_PROVIDER` values (§4.1, §18.1)."""

    ALPACA = "alpaca"
    ALPHAVANTAGE = "alphavantage"


class McapTier(StrEnum):
    SMALL = "small"  # < $2B
    MID = "mid"  # $2B to $10B
    LARGE = "large"  # >= $10B
