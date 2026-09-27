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
    ANCHORED = "ANCHORED"
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
    RunStatus.ANCHORED,
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
    # Backtests are never executed: they stop at ANCHORED and may be scored from there (§11).
    RunStatus.ANCHORED: frozenset(
        {RunStatus.EXECUTED, RunStatus.SCORED, RunStatus.FAILED, RunStatus.PARTIAL}
    ),
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


class SizingMode(StrEnum):
    """Which sizing branch produced a book (§8.1)."""

    RANK = "rank"
    CALIBRATED = "calibrated"


class KillTrigger(StrEnum):
    """What halted trading (§9). Only MANUAL flattens."""

    DAILY_LOSS = "daily_loss"
    STALE_FEED = "stale_feed"
    MANUAL = "manual"


# ``runs.status_reason`` of a run a kill-switch halt moved to PARTIAL (§9): terminal, never retried.
HALT_REASON_PREFIX = "kill_switch:"


def halt_reason(trigger: KillTrigger) -> str:
    return f"{HALT_REASON_PREFIX}{trigger.value}"


class Horizon(IntEnum):
    """Scoring horizon in trading days (§3.1, §18.3)."""

    D5 = 5
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


class OrderKind(StrEnum):
    """Order type used by the §9 limit -> cancel -> market sequence."""

    LIMIT = "limit"
    MARKET = "market"


class BrokerOrderStatus(StrEnum):
    """Broker order state, collapsed to what execution decisions need (§9).

    Anything not known to be final maps to OPEN or PENDING_CANCEL, so an unrecognised broker
    status is never mistaken for a confirmed cancel.
    """

    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    PENDING_CANCEL = "pending_cancel"
    FILLED = "filled"
    CANCELED = "canceled"
    EXPIRED = "expired"
    DONE_FOR_DAY = "done_for_day"
    REJECTED = "rejected"

    @property
    def terminal(self) -> bool:
        return self in TERMINAL_ORDER_STATUSES


class ReferenceSource(StrEnum):
    """Where an execution reference price came from (§9)."""

    IEX_MID = "iex_mid"
    SIP_LAST = "sip_last"


class SecurityKind(StrEnum):
    """What a ``securities`` row is (§4.3). Only ``equity`` rows have a real CIK."""

    EQUITY = "equity"
    ETF = "etf"


class Tape(StrEnum):
    """Consolidated tape a trade printed on: A/B follow the CTS spec, C the UTP spec (§4.6)."""

    A = "A"
    B = "B"
    C = "C"


class RefStatus(StrEnum):
    """Outcome of resolving a reference price (§4.6, §9). ``unresolved`` is durable evidence."""

    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class RefMode(StrEnum):
    """How an execution reference was obtained: the live §9 rule or a historical SIP trade."""

    LIVE = "live"
    BACKTEST = "backtest"


class RefReason(StrEnum):
    """Why a reference is unresolved. There is deliberately no daily-price reason or source."""

    NO_REFERENCE_PRICE = "no_reference_price"
    NO_ELIGIBLE_TRADE = "no_eligible_trade"
    UNKNOWN_CONDITION = "unknown_condition"
    PROVIDER_MAPPING_UNVALIDATED = "provider_mapping_unvalidated"
    SIP_ENTITLEMENT = "sip_entitlement"
    INCOMPLETE_TRADES = "incomplete_trades"
    CALENDAR_UNCOVERED = "calendar_uncovered"
    AMBIGUOUS_ORDER = "ambiguous_order"  # same-instant trades that cannot be ordered or priced


class ConditionEligibility(StrEnum):
    """Whether one trade may set the consolidated last sale price (CTS/UTP, §4.6)."""

    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    CONDITIONAL = "conditional"  # only if it is the first/only qualifying last of the day
    UNKNOWN = "unknown"


class ReferenceJob(StrEnum):
    """Market-data acquisition jobs (P6.3). They are not SLA feeds and cannot trigger a halt."""

    CALENDAR = "calendar"
    BENCHMARKS = "benchmarks"
    DGS3MO = "dgs3mo"
    REFERENCES = "references"  # polls for runs whose reference window is due
    HALT_SWEEP = "halt_sweep"  # resolves pending halt-reference requests
    CORPORATE_ACTIONS = "corporate_actions"  # P6.4: actions + per-security coverage
    LISTING_STATUS = "listing_status"  # P6.4: Alpaca asset-status polls + EDGAR Form 25
    DELISTINGS = "delistings"  # P6.4: evidence-hierarchy derivation (§4.6)


class HaltRequestStatus(StrEnum):
    """A halt-reference request is only ever written pending; state is derived from later rows."""

    SYMBOLS_PENDING = "symbols_pending"


class ModelTier(StrEnum):
    """LLM tiers (§10.1)."""

    FAST = "fast"
    STRONG = "strong"
    PROBE = "probe"


class ReasoningEffort(StrEnum):
    """OpenRouter ``reasoning.effort`` levels a model entry may set (§10.1)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


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


class AliasKind(StrEnum):
    """Where an anonymizer alias came from (invariant 4)."""

    NAME = "name"
    TICKER = "ticker"
    CIK = "cik"
    BRAND = "brand"
    PERSON = "person"  # a named insider or executive (Form 4 filer)


TERMINAL_ORDER_STATUSES = frozenset(
    {
        BrokerOrderStatus.FILLED,
        BrokerOrderStatus.CANCELED,
        BrokerOrderStatus.EXPIRED,
        BrokerOrderStatus.DONE_FOR_DAY,
        BrokerOrderStatus.REJECTED,
    }
)


# --- P6.4 corporate actions + delistings (§4.6) -------------------------------------------------


class CorporateActionType(StrEnum):
    """Every type Alpaca ``GET /v1/corporate-actions`` supports (``types`` parameter, 2026-09).

    The value is the singular ``types`` token; `response_key` is the plural response group.
    """

    REVERSE_SPLIT = "reverse_split"
    FORWARD_SPLIT = "forward_split"
    UNIT_SPLIT = "unit_split"
    CASH_DIVIDEND = "cash_dividend"
    STOCK_DIVIDEND = "stock_dividend"
    SPIN_OFF = "spin_off"
    CASH_MERGER = "cash_merger"
    STOCK_MERGER = "stock_merger"
    STOCK_AND_CASH_MERGER = "stock_and_cash_merger"
    REDEMPTION = "redemption"
    NAME_CHANGE = "name_change"
    WORTHLESS_REMOVAL = "worthless_removal"
    RIGHTS_DISTRIBUTION = "rights_distribution"
    PARTIAL_CALL = "partial_call"
    REORGANIZATION = "reorganization"

    @property
    def response_key(self) -> str:
        return f"{self.value}s"


class ActionInterpretation(StrEnum):
    """What later return construction may do with an action. ``uninterpreted`` blocks a window."""

    SPLIT_FACTOR = "split_factor"
    CASH_DIVIDEND = "cash_dividend"
    STOCK_DISTRIBUTION = "stock_distribution"
    TERMINAL = "terminal"
    IDENTITY = "identity"
    UNINTERPRETED = "uninterpreted"


ACTION_INTERPRETATION: Mapping[CorporateActionType, ActionInterpretation] = {
    CorporateActionType.FORWARD_SPLIT: ActionInterpretation.SPLIT_FACTOR,
    CorporateActionType.REVERSE_SPLIT: ActionInterpretation.SPLIT_FACTOR,
    CorporateActionType.CASH_DIVIDEND: ActionInterpretation.CASH_DIVIDEND,
    CorporateActionType.STOCK_DIVIDEND: ActionInterpretation.STOCK_DISTRIBUTION,
    CorporateActionType.CASH_MERGER: ActionInterpretation.TERMINAL,
    CorporateActionType.STOCK_MERGER: ActionInterpretation.TERMINAL,
    CorporateActionType.STOCK_AND_CASH_MERGER: ActionInterpretation.TERMINAL,
    CorporateActionType.REDEMPTION: ActionInterpretation.TERMINAL,
    CorporateActionType.WORTHLESS_REMOVAL: ActionInterpretation.TERMINAL,
    CorporateActionType.NAME_CHANGE: ActionInterpretation.IDENTITY,
    # Stored faithfully; a return window containing one is UNRESOLVED until interpreted.
    CorporateActionType.UNIT_SPLIT: ActionInterpretation.UNINTERPRETED,
    CorporateActionType.SPIN_OFF: ActionInterpretation.UNINTERPRETED,
    CorporateActionType.RIGHTS_DISTRIBUTION: ActionInterpretation.UNINTERPRETED,
    CorporateActionType.PARTIAL_CALL: ActionInterpretation.UNINTERPRETED,
    CorporateActionType.REORGANIZATION: ActionInterpretation.UNINTERPRETED,
}


class KnowledgeBasis(StrEnum):
    """How a row was acquired. ``backfill`` rows are sensitivity/debug evidence only: they never
    feed headline metrics, sequential decisions or forward-test evidence (readers exclude them
    unless asked)."""

    PROSPECTIVE = "prospective"
    BACKFILL = "backfill"


class ActionCoverageFilter(StrEnum):
    """The provider's date filter a coverage range describes. Alpaca ``start``/``end`` are
    inclusive bounds on ``process_date`` (API reference, re-checked 2026-09-27)."""

    PROCESS_DATE = "process_date"


class AssetStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    NOT_FOUND = "not_found"  # the broker answered 404 for the symbol (durable evidence, not error)


class SymbolSource(StrEnum):
    SEED = "seed"  # the ticker a security was created with
    NAME_CHANGE = "name_change"  # an Alpaca name_change action (continuity evidence)


class DelistingStatus(StrEnum):
    DELISTED = "delisted"
    IDENTITY_CHANGED = "identity_changed"
    SUSPECTED_GAP = "suspected_gap"


class DelistingReason(StrEnum):
    CASH_MERGER = "cash_merger"
    STOCK_MERGER = "stock_merger"
    STOCK_AND_CASH_MERGER = "stock_and_cash_merger"
    REDEMPTION = "redemption"
    WORTHLESS_REMOVAL = "worthless_removal"
    NAME_CHANGE = "name_change"
    LISTING_TERMINATED = "listing_terminated"  # inactive polls + Form 25, no terminal action
    MARKET_DATA_ABSENCE = "market_data_absence"  # >= N confirmed sessions without a bar


class TerminalReturnSource(StrEnum):
    CONSIDERATION = "consideration"  # inputs stored; the value is computed at scoring (P6.5)
    WORTHLESS = "worthless"  # -100%
    DEFAULT = "default"  # conservative -30% (Shumway 1997), flagged for sensitivity reporting


class Form25Provision(StrEnum):
    """The 17 CFR 240.12d2-2 paragraph a Form 25 relies on (its checkbox)."""

    A1 = "a1"  # exchange: entire class called for redemption/maturity/retirement
    A2 = "a2"  # exchange: entire class redeemed or paid
    A3 = "a3"  # exchange: substitution (successor may delay effectiveness, (d)(8))
    A4 = "a4"  # exchange: all rights extinguished
    B = "b"  # exchange: delisting under its own rules
    C = "c"  # issuer: voluntary withdrawal
