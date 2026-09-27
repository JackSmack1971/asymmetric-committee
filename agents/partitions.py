"""Agent input partitions (§3 "Sees" / "Blinded to"; rule set N, §12.1).

One builder per agent. Each reads only the feeds in its §3 row, so a leak across the information
boundary is a code path that does not exist, and ``tests/agents/test_partitions.py`` asserts it.
Every row carries an ``evidence_id`` the agent may cite; the validator (``agents/base.py``) accepts
only IDs that appear in the partition it was given (§3.1, §10.2).

Inputs are contract rows the caller read through ``store.as_of``. Every feed is filtered by
``available_at <= as_of`` again here (invariant 3). Identity (ticker, name, CIK, brands, executive
names) is removed by the ``AliasMasker`` before any text is rendered; the CIK is used only to derive
insider pseudonyms and is never rendered.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from agents.partitioner import AliasMasker, person_token
from contracts.data import FeatureRow, FundamentalFact, InsiderTxn, NewsItem
from contracts.enums import AgentName, FeedName, InsiderRole, InsiderTxnCode, McapTier
from features.builder import CONCEPTS
from features.renderer import (
    FUNDAMENTAL_FEATURES,
    NA,
    PRICE_FEATURES,
    RenderError,
    fiscal_index,
    mcap_tier_label,
    relative_days,
    render_features,
    scrub_text,
    sig2,
)

MAX_QUARTERS = 20
MAX_INSIDER_ROWS = 25
INSIDER_LOOKBACK_DAYS = 180
CLUSTER_DAYS = 14
NEWS_LOOKBACK_DAYS = 30
MAX_NEWS_ROWS = 10
MAX_NEWS_CHARS = 400
ROUTINE_YEARS = 3
QUARTER_DAYS = (80, 100)

REGIME_FEATURES = (
    "spy_return_1m",
    "spy_return_3m",
    "spy_sma_50d_distance",
    "spy_sma_200d_distance",
    "spy_realized_vol_20d",
    "vix_proxy_zscore",
    "yield_10y_pct",
    "yield_3m_pct",
)

_EVIDENCE_FEED = {
    "FIN": FeedName.FUNDAMENTALS,
    "INS": FeedName.INSIDER_TRADES,
    "NEWS": FeedName.NEWS,
    "TECH": FeedName.FEATURES,
    "MACRO": FeedName.REGIME,
}
_EVIDENCE_LINE = re.compile(r"^((FIN|INS|NEWS|TECH|MACRO)_[A-Za-z0-9_]+)\t", re.MULTILINE)
# Analyst rating and price-target items are removed: they induce herding (§3, Fin-Bias 2025).
ANALYST_RE = re.compile(
    r"\b(upgrade[sd]?|downgrade[sd]?|price[- ]targets?|target price|initiat\w+ coverage"
    r"|reiterat\w+|analyst\w*|overweight|underweight|outperform|underperform|(?:buy|sell|hold)"
    r"[- ]rating|rating (?:of|to|from)|raises? target|lowers? target|cuts? target)\b",
    re.IGNORECASE,
)


def is_analyst_item(headline: str) -> bool:
    return ANALYST_RE.search(headline) is not None


@dataclass(frozen=True)
class Partition:
    """The exact text one agent is shown, and the evidence rows it may cite."""

    agent: AgentName
    entity_token: str
    as_of: datetime
    text: str
    evidence: frozenset[tuple[FeedName, str]]

    @property
    def input_hash(self) -> str:
        """Cache key component (§10.2): identical inputs are never re-billed."""
        return hashlib.sha256(self.text.encode()).hexdigest()


@dataclass(frozen=True)
class EntityData:
    """Everything the system holds for one security. Builders take only the parts they may use."""

    security_id: int
    entity_token: str
    cik: int  # identity: hashed into insider pseudonyms, never rendered
    sector: str
    mcap_tier: McapTier | None  # the renderer prints an unknown size explicitly
    features: FeatureRow
    facts: Sequence[FundamentalFact]
    insiders: Sequence[InsiderTxn]
    news: Sequence[NewsItem]
    adv30_usd: float | None  # system-computed liquidity; only a ratio of it is ever rendered


# --- helpers ---------------------------------------------------------------------------------


def _cell(value: float | None) -> str:
    return NA if value is None or not math.isfinite(value) else sig2(value)


def _ratio(num: float | None, den: float | None) -> float | None:
    return None if num is None or not den else num / den


def _day_start(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=UTC)


def _evidence(text: str) -> frozenset[tuple[FeedName, str]]:
    return frozenset(
        (_EVIDENCE_FEED[m.group(2)], m.group(1)) for m in _EVIDENCE_LINE.finditer(text)
    )


def _clean(text: str) -> str:
    return " ".join(text.split())


def _section(title: str, body: str) -> str:
    return f"## {title}\n{body}"


# --- fundamentals ----------------------------------------------------------------------------


def _is_quarterly(fact: FundamentalFact) -> bool:
    if fact.period_start is None:  # instants (balance sheet) and undated facts
        return True
    return QUARTER_DAYS[0] <= (fact.period_end - fact.period_start).days <= QUARTER_DAYS[1]


def quarterly_history(
    facts: Sequence[FundamentalFact], as_of: datetime
) -> list[tuple[str, date, dict[str, float | None]]]:
    """Per-quarter dimensionless ratios from as-filed facts, newest first: ``(Q-n, period_end,
    ratios)``. Year-to-date rows are ignored; a missing input gives ``None``, never a guess."""
    latest: dict[tuple[str, date], FundamentalFact] = {}
    for f in facts:
        if f.available_at > as_of:
            continue
        key = (f.concept, f.period_end)
        cur = latest.get(key)
        if cur is None or (f.available_at, f.source_version) > (
            cur.available_at,
            cur.source_version,
        ):
            latest[key] = f

    def series(name: str) -> dict[date, float]:
        for concept in CONCEPTS[name]:
            rows = {
                pe: f.value for (c, pe), f in latest.items() if c == concept and _is_quarterly(f)
            }
            if rows:
                return rows
        return {}

    revenue, gross, op = series("revenue"), series("gross_profit"), series("operating_income")
    net, cfo, assets, shares = (
        series("net_income"),
        series("cash_flow"),
        series("assets"),
        series("shares"),
    )

    def year_ago(s: Mapping[date, float], pe: date) -> float | None:
        target = pe - timedelta(days=365)
        near = [d for d in s if abs((d - target).days) <= 10]
        return s[min(near, key=lambda d: abs((d - target).days))] if near else None

    out: list[tuple[str, date, dict[str, float | None]]] = []
    for pe in sorted(revenue, reverse=True):
        try:
            label = fiscal_index(pe, as_of)
        except RenderError:
            continue
        rev, prev_rev = revenue[pe], year_ago(revenue, pe)
        prev_shares = year_ago(shares, pe)
        accrual = net[pe] - cfo[pe] if pe in net and pe in cfo else None
        out.append(
            (
                label,
                pe,
                {
                    "gross_margin": _ratio(gross.get(pe), rev),
                    "operating_margin": _ratio(op.get(pe), rev),
                    "net_margin": _ratio(net.get(pe), rev),
                    "cfo_margin": _ratio(cfo.get(pe), rev),
                    "revenue_growth_yoy": None
                    if prev_rev is None or not prev_rev
                    else rev / prev_rev - 1,
                    "share_change_yoy": None
                    if pe not in shares or not prev_shares
                    else shares[pe] / prev_shares - 1,
                    "accruals_ratio": _ratio(accrual, assets.get(pe)),
                },
            )
        )
        if len(out) == MAX_QUARTERS:
            break
    return out


HISTORY_COLUMNS = (
    "gross_margin",
    "operating_margin",
    "net_margin",
    "cfo_margin",
    "revenue_growth_yoy",
    "share_change_yoy",
    "accruals_ratio",
)


def _history_table(facts: Sequence[FundamentalFact], as_of: datetime) -> str:
    lines = ["evidence_id\tfiscal_period\tperiod_age\t" + "\t".join(HISTORY_COLUMNS)]
    for label, pe, ratios in quarterly_history(facts, as_of):
        cells = "\t".join(_cell(ratios[c]) for c in HISTORY_COLUMNS)
        age = relative_days(_day_start(pe), as_of)
        lines.append(f"FIN_{label.replace('-', '')}\t{label}\t{age}\t{cells}")
    return "\n".join(lines)


# --- insiders --------------------------------------------------------------------------------


def role_token(txn: InsiderTxn) -> str:
    title = (txn.officer_title or "").upper()
    if txn.role is InsiderRole.OFFICER:
        if "CEO" in title or "CHIEF EXECUTIVE" in title:
            return "ROLE_CEO"
        if "CFO" in title or "CHIEF FINANCIAL" in title:
            return "ROLE_CFO"
        return "ROLE_OFFICER"
    return {
        InsiderRole.DIRECTOR: "ROLE_DIRECTOR",
        InsiderRole.TEN_PERCENT_OWNER: "ROLE_10PCT",
    }.get(txn.role, "ROLE_OTHER")


def _direction(txn: InsiderTxn) -> str:
    if txn.code is InsiderTxnCode.P and txn.acquired:
        return "buy"
    if txn.code is InsiderTxnCode.S and not txn.acquired:
        return "sell"
    return f"other_{txn.code.value}"


def _traded_in(txns: Sequence[InsiderTxn], filer: str, year: int, month: int) -> bool:
    return any(
        t.filer == filer
        and t.code in (InsiderTxnCode.P, InsiderTxnCode.S)
        and (t.txn_date.year, t.txn_date.month) == (year, month)
        for t in txns
    )


def trade_tag(txn: InsiderTxn, visible: Sequence[InsiderTxn]) -> str:
    """Cohen-Malloy-Pomorski (2012): ROUTINE if the filer traded in the same calendar month in each
    of the prior three years, OPPORTUNISTIC if not, UNKNOWN when the visible Form 4 history is too
    short to tell (it never guesses OPPORTUNISTIC)."""
    y, m = txn.txn_date.year, txn.txn_date.month
    if all(_traded_in(visible, txn.filer, y - k, m) for k in range(1, ROUTINE_YEARS + 1)):
        return "ROUTINE"
    earliest = min(t.txn_date for t in visible)
    covered = earliest <= date(y - ROUTINE_YEARS, m, 1)
    return "OPPORTUNISTIC" if covered else "UNKNOWN"


def position_change(txn: InsiderTxn) -> float | None:
    """Shares traded as a fraction of the position held before the trade."""
    if txn.post_holdings is None:
        return None
    prior = txn.post_holdings - txn.shares if txn.acquired else txn.post_holdings + txn.shares
    return txn.shares / prior if prior > 0 else None


def adv_intensity(txn: InsiderTxn, adv30_usd: float | None) -> float | None:
    """Dollar value traded over 30-day average dollar volume (dimensionless)."""
    if txn.price is None or not adv30_usd or adv30_usd <= 0:
        return None
    return txn.shares * txn.price / adv30_usd


INSIDER_COLUMNS = (
    "evidence_id",
    "exec",
    "role",
    "direction",
    "code",
    "txn_age",
    "filed_age",
    "pct_of_holdings",
    "adv_intensity",
    "plan_10b5_1",
    "tag",
    "cluster",
    "signal",
)


def _insider_table(
    txns: Sequence[InsiderTxn], cik: int, adv30_usd: float | None, as_of: datetime
) -> str:
    visible = [t for t in txns if t.available_at <= as_of]
    lines = ["\t".join(INSIDER_COLUMNS)]
    if not visible:
        return lines[0]
    recent = sorted(
        (t for t in visible if 0 <= (as_of.date() - t.txn_date).days <= INSIDER_LOOKBACK_DAYS),
        key=lambda t: (t.available_at, t.accession, t.seq),
        reverse=True,
    )[:MAX_INSIDER_ROWS]
    buyers = [t for t in visible if _direction(t) == "buy"]
    for n, t in enumerate(recent, start=1):
        tag = trade_tag(t, visible)
        cluster = len(
            {b.filer for b in buyers if abs((b.txn_date - t.txn_date).days) <= CLUSTER_DAYS}
        )
        direction = _direction(t)
        signal = int(direction == "buy" and not t.is_10b5_1 and tag == "OPPORTUNISTIC")
        cells = (
            f"INS_{n}",
            person_token(t.filer, cik),
            role_token(t),
            direction,
            t.code.value,
            relative_days(_day_start(t.txn_date), as_of),
            relative_days(t.available_at, as_of),
            _cell(position_change(t)),
            _cell(adv_intensity(t, adv30_usd)),
            str(int(t.is_10b5_1)),
            tag,
            str(cluster) if direction == "buy" else "0",
            str(signal),
        )
        lines.append("\t".join(cells))
    return "\n".join(lines)


# --- news ------------------------------------------------------------------------------------


def _visible_news(news: Sequence[NewsItem], security_id: int, as_of: datetime) -> list[NewsItem]:
    latest: dict[str, NewsItem] = {}
    for item in news:
        if security_id not in item.security_ids or item.available_at > as_of:
            continue
        cur = latest.get(item.item_id)
        if cur is None or item.available_at > cur.available_at:
            latest[item.item_id] = item
    recent = [
        i
        for i in latest.values()
        if timedelta(0) <= as_of - i.event_time <= timedelta(days=NEWS_LOOKBACK_DAYS)
        and not is_analyst_item(i.headline)
    ]
    return sorted(recent, key=lambda i: (i.event_time, i.item_id), reverse=True)[:MAX_NEWS_ROWS]


def _news_table(
    news: Sequence[NewsItem], security_id: int, as_of: datetime, masker: AliasMasker
) -> str:
    def text(raw: str) -> str:
        return _clean(scrub_text(masker.mask(raw)))

    lines = ["evidence_id\tage\theadline\tsummary"]
    for n, item in enumerate(_visible_news(news, security_id, as_of), start=1):
        summary = text(item.summary)[:MAX_NEWS_CHARS]
        lines.append(
            f"NEWS_{n}\t{relative_days(item.event_time, as_of)}\t{text(item.headline)}\t{summary}"
        )
    return "\n".join(lines)


# --- the partitioner -------------------------------------------------------------------------


class Partitioner:
    """Builds every agent's partition for one ``as_of``.

    ``universe`` and ``sectors`` are the point-in-time included snapshot used for cross-sectional
    z-scores and percentiles. ``regime`` holds market-wide features keyed by ``REGIME_FEATURES``.
    """

    def __init__(
        self,
        *,
        as_of: datetime,
        masker: AliasMasker,
        universe: Sequence[FeatureRow],
        sectors: Mapping[int, str],
        regime: Mapping[str, float | None] | None = None,
    ) -> None:
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        unknown = set(regime or {}) - set(REGIME_FEATURES)
        if unknown:
            raise RenderError(f"regime features not allowed: {sorted(unknown)}")
        self._as_of = as_of
        self._masker = masker
        self._universe = [r for r in universe if r.available_at <= as_of]
        self._sectors = dict(sectors)
        self._regime = dict(regime or {})

    # shared pieces -------------------------------------------------------------------------

    def _header(self, e: EntityData) -> str:
        body = f"entity\t{e.entity_token}\nsector\t{e.sector}\nsize\t{mcap_tier_label(e.mcap_tier)}"
        return _section("entity", body)

    def _features(self, e: EntityData, names: Sequence[str], prefix: str) -> str:
        return render_features(
            e.features,
            universe=self._universe,
            sectors=self._sectors,
            names=names,
            evidence_prefix=prefix,
        )

    def _fundamentals(self, e: EntityData) -> list[str]:
        return [
            _section("fundamentals_summary", self._features(e, FUNDAMENTAL_FEATURES, "FIN_SUM")),
            _section("fundamentals_history", _history_table(e.facts, self._as_of)),
        ]

    def _regime_table(self) -> str:
        lines = ["evidence_id\tfeature\tvalue"]
        for n, name in enumerate(REGIME_FEATURES, start=1):
            if name in self._regime:
                lines.append(f"MACRO_R_{n}\t{name}\t{_cell(self._regime[name])}")
        return "\n".join(lines)

    def _finish(self, agent: AgentName, e: EntityData, sections: list[str]) -> Partition:
        text = "\n\n".join([self._header(e), *sections])
        return Partition(agent, e.entity_token, self._as_of, text, _evidence(text))

    # one builder per agent -----------------------------------------------------------------

    def value(self, e: EntityData) -> Partition:
        """Fundamentals only. Blind to price, news, insiders and identity."""
        return self._finish(AgentName.VALUE, e, self._fundamentals(e))

    def quality_catalyst(self, e: EntityData) -> Partition:
        """Fundamentals and masked news. Blind to price and insiders."""
        news = _news_table(e.news, e.security_id, self._as_of, self._masker)
        return self._finish(
            AgentName.QUALITY_CATALYST, e, [*self._fundamentals(e), _section("news", news)]
        )

    def insider(self, e: EntityData) -> Partition:
        """Fundamentals summary and Form 4 rows. Blind to price and news."""
        summary = self._features(e, FUNDAMENTAL_FEATURES, "FIN_SUM")
        table = _insider_table(e.insiders, e.cik, e.adv30_usd, self._as_of)
        return self._finish(
            AgentName.INSIDER,
            e,
            [_section("fundamentals_summary", summary), _section("insider_trades", table)],
        )

    def technical(self, e: EntityData) -> Partition:
        """Engineered price features only. Never raw bars."""
        table = self._features(e, PRICE_FEATURES, "TECH")
        return self._finish(AgentName.TECHNICAL, e, [_section("price_features", table)])

    def macro_narrative(self, e: EntityData) -> Partition:
        """Regime, price features and masked news. Blind to fundamentals and insiders."""
        news = _news_table(e.news, e.security_id, self._as_of, self._masker)
        return self._finish(
            AgentName.MACRO_NARRATIVE,
            e,
            [
                _section("market_regime", self._regime_table()),
                _section("price_features", self._features(e, PRICE_FEATURES, "MACRO_P")),
                _section("news", news),
            ],
        )

    def red_team(self, e: EntityData) -> Partition:
        """All partitions, for the top candidates only (§7.4). Same masking, same rules."""
        news = _news_table(e.news, e.security_id, self._as_of, self._masker)
        return self._finish(
            AgentName.RED_TEAM,
            e,
            [
                *self._fundamentals(e),
                _section(
                    "insider_trades", _insider_table(e.insiders, e.cik, e.adv30_usd, self._as_of)
                ),
                _section("news", news),
                _section("price_features", self._features(e, PRICE_FEATURES, "TECH")),
                _section("market_regime", self._regime_table()),
            ],
        )

    def build_all(self, e: EntityData) -> dict[AgentName, Partition]:
        """The five voting agents' partitions (the red team is built for candidates only)."""
        return {
            AgentName.VALUE: self.value(e),
            AgentName.QUALITY_CATALYST: self.quality_catalyst(e),
            AgentName.INSIDER: self.insider(e),
            AgentName.TECHNICAL: self.technical(e),
            AgentName.MACRO_NARRATIVE: self.macro_narrative(e),
        }


def assign_entity_tokens(security_ids: Sequence[int], prefix: str = "TICKER") -> dict[int, str]:
    """Run-scoped pseudonyms by stable rank of ``security_id`` (never derived from the ticker).
    Numbering starts at 01; ``00`` is reserved (``ENTITY_00`` marks shared aliases)."""
    ordered = sorted(set(security_ids))
    return {sid: f"{prefix}_{i:02d}" for i, sid in enumerate(ordered, start=1)}


__all__ = [
    "REGIME_FEATURES",
    "EntityData",
    "Partition",
    "Partitioner",
    "assign_entity_tokens",
    "is_analyst_item",
    "quarterly_history",
    "role_token",
    "trade_tag",
]
