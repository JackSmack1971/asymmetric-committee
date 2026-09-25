"""Table definitions (§4.3). Internal to ``store/``: nothing outside this package may import it.

Migrations are written out explicitly in ``store/migrations``; a test compares them to this
metadata so the two cannot drift.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

metadata = MetaData()


def _ts(name: str, *, nullable: bool = False) -> Column[Any]:
    return Column(name, DateTime(timezone=True), nullable=nullable)


def _bitemporal() -> list[Column[Any]]:
    """§4.2 columns carried by every fact table."""
    return [
        _ts("event_time"),
        _ts("available_at"),
        Column("ingested_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("source_version", Text, nullable=False),
    ]


def _sid(*, nullable: bool = False) -> Column[Any]:
    return Column("security_id", Integer, ForeignKey("securities.security_id"), nullable=nullable)


# --- reference ---------------------------------------------------------------------------------

securities = Table(
    "securities",
    metadata,
    Column("security_id", Integer, primary_key=True, autoincrement=True),
    Column("ticker", Text, nullable=False),
    Column("cik", BigInteger, nullable=False),
    Column("name", Text, nullable=False),
    Column("sector", Text),
    Column("industry", Text),
    Column("listed_from", Date),
    Column("listed_to", Date),
    UniqueConstraint("cik", "ticker", name="uq_securities_cik_ticker"),
)

# --- fact tables (bitemporal) ----------------------------------------------------------------

# Hypertable on event_time. Natural key (security_id, event_time); source_version is the feed.
price_bars = Table(
    "price_bars",
    metadata,
    _sid(),
    *_bitemporal(),
    Column("open", Double, nullable=False),
    Column("high", Double, nullable=False),
    Column("low", Double, nullable=False),
    Column("close", Double, nullable=False),
    Column("volume", Double, nullable=False),
    Column("feed", Text, nullable=False),
    UniqueConstraint("security_id", "event_time", "source_version", name="uq_price_bars"),
    Index("ix_price_bars_sid_avail", "security_id", "available_at"),
)

# Natural key (security_id, concept, unit, period_start, period_end); source_version = accession.
# Insert-only: a trigger rejects UPDATE and DELETE.
fundamentals_asfiled = Table(
    "fundamentals_asfiled",
    metadata,
    _sid(),
    *_bitemporal(),
    Column("concept", Text, nullable=False),
    Column("unit", Text, nullable=False),
    Column("period_start", Date),
    Column("period_end", Date, nullable=False),
    Column("fiscal_period", Text),
    Column("form", Text),
    Column("value", Double, nullable=False),
    UniqueConstraint(
        "security_id",
        "concept",
        "unit",
        "period_start",
        "period_end",
        "source_version",
        name="uq_fundamentals_asfiled",
        postgresql_nulls_not_distinct=True,
    ),
    Index("ix_fundamentals_asfiled_sid_avail", "security_id", "available_at"),
)

# Natural key (accession, seq); source_version = accession.
insider_txns = Table(
    "insider_txns",
    metadata,
    _sid(),
    *_bitemporal(),
    Column("accession", Text, nullable=False),
    Column("seq", Integer, nullable=False),
    Column("filer", Text, nullable=False),
    Column("role", Text, nullable=False),
    Column("officer_title", Text),
    Column("txn_date", Date, nullable=False),
    Column("code", Text, nullable=False),
    Column("acquired", Boolean, nullable=False),
    Column("shares", Double, nullable=False),
    Column("price", Double),
    Column("post_holdings", Double),
    Column("is_10b5_1", Boolean, nullable=False),
    UniqueConstraint("accession", "seq", "source_version", name="uq_insider_txns"),
    Index("ix_insider_txns_sid_avail", "security_id", "available_at"),
)

# Natural key item_id; source_version = revision. Many securities per item.
news_items = Table(
    "news_items",
    metadata,
    *_bitemporal(),
    Column("item_id", Text, nullable=False),
    Column("security_ids", ARRAY(Integer), nullable=False),
    Column("published_at", DateTime(timezone=True), nullable=False),
    Column("headline", Text, nullable=False),
    Column("summary", Text, nullable=False),
    Column("body_hash", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("publisher", Text, nullable=False),
    Column("url", Text, nullable=False),
    UniqueConstraint("item_id", "source_version", name="uq_news_items"),
    Index("ix_news_items_sids", "security_ids", postgresql_using="gin"),
    Index("ix_news_items_avail", "available_at"),
)

# Hypertable on event_time (the feature as-of). Filled from P2.
features = Table(
    "features",
    metadata,
    _sid(),
    *_bitemporal(),
    Column("feature_set_version", Text, nullable=False),
    Column("values", JSONB, nullable=False),
    UniqueConstraint(
        "security_id", "event_time", "feature_set_version", "source_version", name="uq_features"
    ),
    Index("ix_features_sid_avail", "security_id", "available_at"),
)

# Survivorship-safe (§4.4): rows are never deleted, so delisted names stay in past snapshots.
universe_snapshots = Table(
    "universe_snapshots",
    metadata,
    _sid(),
    *_bitemporal(),
    Column("snapshot_date", Date, nullable=False),
    Column("mcap_usd", Double),
    Column("mcap_tier", Text),
    Column("adv_usd", Double),
    Column("price", Double),
    Column("score", Double),
    Column("rank", Integer),
    Column("included", Boolean, nullable=False),
    Column("reason", Text, nullable=False),
    UniqueConstraint(
        "snapshot_date", "security_id", "source_version", name="uq_universe_snapshots"
    ),
    Index("ix_universe_snapshots_sid_avail", "security_id", "available_at"),
)

feed_health = Table(
    "feed_health",
    metadata,
    Column("feed", Text, primary_key=True),
    _ts("last_success_at", nullable=True),
    _ts("last_available_at", nullable=True),
    Column("rows", BigInteger, nullable=False, server_default="0"),
    Column("last_error", Text),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

# --- run records (written from P2 on) --------------------------------------------------------

runs = Table(
    "runs",
    metadata,
    Column("run_id", UUID(as_uuid=True), primary_key=True),
    Column("mode", Text, nullable=False),
    _ts("as_of"),
    Column("config_hash", Text, nullable=False),
    Column("status", Text, nullable=False),
    _ts("started_at"),
    _ts("ended_at", nullable=True),
)


def _run_id() -> Column[Any]:
    return Column("run_id", UUID(as_uuid=True), ForeignKey("runs.run_id"), nullable=False)


gate_decisions = Table(
    "gate_decisions",
    metadata,
    _run_id(),
    _sid(),
    Column("score", Double, nullable=False),
    Column("passed", Boolean, nullable=False),
    Column("components", JSONB, nullable=False),
    UniqueConstraint("run_id", "security_id", name="uq_gate_decisions"),
)

agent_verdicts = Table(
    "agent_verdicts",
    metadata,
    _run_id(),
    _sid(),
    Column("agent", Text, nullable=False),
    Column("verdict", JSONB, nullable=False),
    Column("tokens_in", Integer, nullable=False),
    Column("tokens_out", Integer, nullable=False),
    Column("cost_usd", Double, nullable=False),
    Column("latency_ms", Integer, nullable=False),
    UniqueConstraint("run_id", "security_id", "agent", name="uq_agent_verdicts"),
)

committee_decisions = Table(
    "committee_decisions",
    metadata,
    _run_id(),
    _sid(),
    Column("pooled_p", Double, nullable=False),
    Column("dispersion", Double, nullable=False),
    Column("target_weight", Double, nullable=False),
    Column("cio_action", Text),
    Column("rationale", Text),
    UniqueConstraint("run_id", "security_id", name="uq_committee_decisions"),
)

# Append-only: a trigger rejects UPDATE and DELETE (invariant 5).
decision_commitments = Table(
    "decision_commitments",
    metadata,
    Column("run_id", UUID(as_uuid=True), ForeignKey("runs.run_id"), primary_key=True),
    Column("sha256", Text, nullable=False),
    Column("committed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

orders = Table(
    "orders",
    metadata,
    _run_id(),
    Column("broker_order_id", Text, primary_key=True),
    _sid(),
    Column("side", Text, nullable=False),
    Column("qty", Double, nullable=False),
    Column("limit_price", Double),
    Column("status", Text, nullable=False),
    _ts("submitted_at"),
)

fills = Table(
    "fills",
    metadata,
    _run_id(),
    Column("broker_order_id", Text, ForeignKey("orders.broker_order_id"), nullable=False),
    Column("fill_id", Text, primary_key=True),
    Column("qty", Double, nullable=False),
    Column("price", Double, nullable=False),
    _ts("filled_at"),
    Column("slippage_bps", Double),
)

outcomes = Table(
    "outcomes",
    metadata,
    _run_id(),
    _sid(),
    Column("horizon", Integer, nullable=False),
    Column("fwd_return", Double, nullable=False),
    Column("sector_fwd_return", Double),
    _ts("scored_at"),
    UniqueConstraint("run_id", "security_id", "horizon", name="uq_outcomes"),
)

agent_scores = Table(
    "agent_scores",
    metadata,
    Column("agent", Text, nullable=False),
    Column("model_served", Text, nullable=False),
    Column("window", Text, nullable=False),
    Column("brier", Double),
    Column("ic", Double),
    Column("hit_rate", Double),
    Column("n", Integer, nullable=False),
    _ts("computed_at"),
    UniqueConstraint("agent", "model_served", "window", "computed_at", name="uq_agent_scores"),
)

FACT_TABLES: tuple[Table, ...] = (
    price_bars,
    fundamentals_asfiled,
    insider_txns,
    news_items,
    features,
    universe_snapshots,
)
HYPERTABLES: tuple[Table, ...] = (price_bars, features)
IMMUTABLE_TABLES: tuple[Table, ...] = (fundamentals_asfiled, decision_commitments)
