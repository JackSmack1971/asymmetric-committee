"""Wiring: builds the real services from the environment behind the startup gates.

Everything here runs only after ``assert_startup_ready`` has passed, so a missing owner decision
(model slugs, aliases, sector sign-off, the public anchor remote) stops the process before any
network call, LLM spend or order. Tests build the same objects from fakes instead.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import redis
from sqlalchemy import Engine, create_engine

from agents.base import ChatClient
from agents.llm.openrouter import OpenRouterClient
from agents.llm.ratelimit import ModelRateLimiter
from agents.llm.store import RedisVerdictCache
from config.aliases import load_brand_aliases
from config.loader import AppConfig
from contracts.enums import AgentName, ModelTier, RunMode
from contracts.models import AgentVerdictLLM, CioDecisionLLM, Contract, RedTeamVerdictLLM
from execution.alpaca import AlpacaPaperGateway
from execution.reference import AlpacaMarketData
from execution.trade_conditions import load_provider_map
from ingest.alpaca_trades import AlpacaTrades
from ingest.backfill import build_sources
from ingest.fred import AlfredClient
from ingest.reference_data import ReferenceDataIngestor, benchmark_tickers
from ingest.scheduled import FeedIngestor
from orchestration.anchor_adapters import EsploraHeaders, GitAnchorRepo, OtsCalendars
from orchestration.anchor_stage import AnchorStage, AnchorUpgrader
from orchestration.execution_stage import ExecutionStage
from orchestration.loader import StoreStepLoader
from orchestration.pipeline import BacktestOrchestrator, config_fingerprint
from orchestration.providers import StoreMarketView, reference_provider, store_feeds
from orchestration.references import ReferenceCapture
from orchestration.sink import DecisionSink
from orchestration.startup import assert_startup_ready, calendars
from orchestration.tasks import Runtime

DEFAULT_EXPLORER = "https://blockstream.info/api"
LLM_TIMEOUT_S = 120.0
ANCHOR_TIMEOUT_S = 30.0


class Services:
    """Long-lived clients shared by one process (engine, Redis, HTTP pools)."""

    def __init__(self, env: Mapping[str, str], cfg: AppConfig, engine: Engine) -> None:
        self.env, self.cfg, self.engine = env, cfg, engine
        self.sink = DecisionSink(engine)
        self.redis = redis.Redis.from_url(env["REDIS_URL"])
        self.llm_http = httpx.Client(timeout=LLM_TIMEOUT_S)
        self.anchor_http = httpx.Client(timeout=ANCHOR_TIMEOUT_S)
        self.clock = lambda: datetime.now(UTC)

    def chat_client(self, tier: ModelTier, out: type[Contract]) -> ChatClient:
        entry = self.cfg.models.tiers[tier]
        limiter = ModelRateLimiter(
            self.redis, entry.primary.slug, entry.primary.rpm, entry.primary.tpm
        )
        return OpenRouterClient(
            self.llm_http,
            api_key=self.env["OPENROUTER_API_KEY"],
            entry=entry,
            out_model=out,
            limiter=limiter,
        )

    def stamper(self) -> OtsCalendars:
        return OtsCalendars(
            calendars(self.env),
            self.anchor_http,
            min_success=int(self.env.get("ANCHOR_OTS_MIN_CALENDARS", "1")),
        )

    def git(self) -> GitAnchorRepo:
        return GitAnchorRepo(
            Path(self.env["ANCHOR_GIT_DIR"]),
            self.env["ANCHOR_GIT_REMOTE"],
            self.env["ANCHOR_GIT_BRANCH"],
        )

    def anchor_stage(self) -> AnchorStage:
        return AnchorStage(
            store=self.sink, stamper=self.stamper(), git=self.git(), clock=self.clock
        )

    def gateway(self) -> AlpacaPaperGateway:
        return AlpacaPaperGateway(
            key_id=self.env["ALPACA_API_KEY_ID"], secret=self.env["ALPACA_API_SECRET"]
        )

    def orchestrator(self, mode: RunMode) -> BacktestOrchestrator:
        voter = self.chat_client(ModelTier.FAST, AgentVerdictLLM)
        red = self.chat_client(ModelTier.STRONG, RedTeamVerdictLLM)
        return BacktestOrchestrator(
            config=self.cfg,
            loader=StoreStepLoader(self.engine, brands=load_brand_aliases().by_cik()),
            sink=self.sink,
            client_for=lambda agent: red if agent is AgentName.RED_TEAM else voter,
            cio_client=self.chat_client(ModelTier.STRONG, CioDecisionLLM),
            cache=RedisVerdictCache(self.redis),
            mode=mode,
            anchoring=self.anchor_stage(),  # every run is anchored; only LIVE runs go on to execute
            clock=self.clock,
        )


def build_services(env: Mapping[str, str] | None = None, *, live: bool) -> Services:
    """Run every startup gate (including the database-backed alias check), then connect."""
    env = dict(os.environ) if env is None else env
    engine = create_engine(env.get("DATABASE_URL", ""), pool_pre_ping=True)
    cfg = assert_startup_ready(env, live=live, engine=engine)
    return Services(env, cfg, engine)


def build_runtime(env: Mapping[str, str] | None = None) -> Runtime:
    """The live worker's runtime: a pipeline that stops at ANCHORED, plus the execution stage."""
    svc = build_services(env, live=True)
    gateway = svc.gateway()
    market = AlpacaMarketData(
        key_id=svc.env["ALPACA_API_KEY_ID"], secret=svc.env["ALPACA_API_SECRET"]
    )
    execution = ExecutionStage(
        config=svc.cfg,
        gateway=gateway,
        store=svc.sink,
        market=StoreMarketView(svc.engine, gateway.positions),
        reference=reference_provider(market, svc.clock),
        feeds=store_feeds(svc.engine),
        clock=svc.clock,
        sleep=time.sleep,
    )
    explorer = svc.env.get("ANCHOR_BLOCK_EXPLORER_URL", DEFAULT_EXPLORER)
    sources = build_sources(None, replay=False, env=dict(svc.env))
    ref_cfg = svc.cfg.pipeline.reference_data
    fred_key = svc.env.get("FRED_API_KEY", "")
    return Runtime(
        sink=svc.sink,
        pipeline=svc.orchestrator(RunMode.LIVE),
        anchoring=svc.anchor_stage(),
        execution=execution,
        upgrader=AnchorUpgrader(
            store=svc.sink,
            stamper=svc.stamper(),
            headers=EsploraHeaders(explorer, svc.anchor_http),
            clock=svc.clock,
        ),
        calendar=gateway,
        config_hash=config_fingerprint(svc.cfg),
        clock=svc.clock,
        ingest=FeedIngestor(
            svc.engine,
            sources,
            lookback=timedelta(days=svc.cfg.pipeline.ingest.lookback_days),
        ),
        reference_data=ReferenceDataIngestor(
            svc.engine,
            calendar=gateway,
            bars=sources.bars,
            alfred=AlfredClient(api_key=fred_key) if fred_key else None,
            benchmarks=benchmark_tickers([s.etf for s in svc.cfg.sectors.sectors]),
            forward_days=ref_cfg.calendar_forward_days,
            back_days=ref_cfg.calendar_back_days,
            bar_lookback_days=svc.cfg.pipeline.ingest.lookback_days,
        ),
        references=ReferenceCapture(
            svc.engine,
            svc.sink,
            ref_cfg,
            delay_minutes=svc.cfg.pipeline.rebalance.minutes_after_open,
            config_hash=config_fingerprint(svc.cfg),
            quotes=market,
            trades=AlpacaTrades(
                key_id=svc.env["ALPACA_API_KEY_ID"], secret=svc.env["ALPACA_API_SECRET"]
            ),
            provider_map=load_provider_map(),
        ),
    )
