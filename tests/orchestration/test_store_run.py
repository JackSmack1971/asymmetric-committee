"""StoreStepLoader and the orchestrator against a real Postgres (service-gated like test_sink)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
import respx
from sqlalchemy import Engine, text

from agents.partitions import EntityData, Partition
from contracts.commitment import CommitmentIntegrityError, verify_material
from contracts.enums import AgentName, McapTier, ModelTier, RunStatus
from contracts.models import AgentVerdictLLM, CioDecisionLLM, RedTeamVerdictLLM, StepArtifacts
from orchestration.loader import EmptyUniverseError, StoreStepLoader
from orchestration.pipeline import (
    BacktestOrchestrator,
    StepInputs,
    assert_point_in_time,
)
from orchestration.sink import DecisionSink
from store import write
from tests.agents.world import AS_OF
from tests.orchestration.store_support import seed
from tests.orchestration.test_pipeline import (
    BRANDS,
    URL,
    Provider,
    _client,
    cfg_with,
)


@pytest.fixture
def engine(pg_engine: Engine) -> Iterator[Engine]:
    def wipe() -> None:
        with pg_engine.begin() as c:
            c.execute(text("TRUNCATE runs, securities RESTART IDENTITY CASCADE"))
            c.execute(
                text(
                    "TRUNCATE universe_snapshots, features, fundamentals_asfiled, insider_txns, "
                    "news_items, price_bars RESTART IDENTITY CASCADE"
                )
            )

    wipe()
    yield pg_engine
    wipe()


def test_loader_excludes_every_future_available_fact(engine: Engine) -> None:
    ids = seed(engine)
    inputs = StoreStepLoader(engine, brands=BRANDS).load(AS_OF)

    assert [e.security_id for e in inputs.entities] == ids
    assert_point_in_time(inputs, AS_OF)  # the orchestrator's own re-check agrees
    for e in inputs.entities:
        assert e.features.available_at <= AS_OF and e.features.source_version == "a"
        assert [x.source_version for x in e.facts] == ["0000000001-25-000001"]
        assert all(x.value != 999.0 for x in e.facts)
        assert [t.accession for t in e.insiders] == [f"0000000002-25-{e.security_id:06d}"]
        assert [n.item_id for n in e.news] == [f"n{e.security_id}-old"]
        assert e.adv30_usd == pytest.approx(10.0 * 1e6)  # the 999 correction is not used
        assert e.mcap_tier is McapTier.MID
    # the universe is the snapshot knowable at AS_OF, not the later one
    assert {s.security_id for s in inputs.securities} == set(ids)
    assert isinstance(inputs, StepInputs) and inputs.brands == BRANDS


def test_loader_masks_identity_and_assigns_run_scoped_tokens(engine: Engine) -> None:
    seed(engine)
    inputs = StoreStepLoader(engine, brands=BRANDS).load(AS_OF)
    tokens = [e.entity_token for e in inputs.entities]
    assert tokens == ["TICKER_01", "TICKER_02", "TICKER_03", "TICKER_04"]
    (e,) = inputs.entities[:1]
    part = inputs.partitioner.value(e)
    for word in ("Zephyr", "ZPHR", "1234567", "Owner"):
        assert word.casefold() not in part.text.casefold()


def test_loader_refuses_an_empty_universe_instead_of_committing_nothing(engine: Engine) -> None:
    seed(engine)
    with pytest.raises(EmptyUniverseError):
        StoreStepLoader(engine, brands=BRANDS).load(AS_OF - timedelta(days=60))


# --- the orchestrator over the real sink ---------------------------------------------------------


class Registering:
    """Wraps the store loader and registers each rendered partition for the fake provider."""

    def __init__(self, inner: StoreStepLoader) -> None:
        self.inner = inner
        self.parts: dict[str, Partition] = {}

    def load(self, as_of: datetime) -> StepInputs:
        inputs = self.inner.load(as_of)
        e: EntityData
        for e in inputs.entities:
            for p in [*inputs.partitioner.build_all(e).values(), inputs.partitioner.red_team(e)]:
                self.parts[p.text] = p
        return inputs


def _orch(engine: Engine, loader: Registering) -> BacktestOrchestrator:
    cfg = cfg_with()
    voter = _client(cfg.models.tiers[ModelTier.FAST], AgentVerdictLLM)
    red = _client(cfg.models.tiers[ModelTier.STRONG], RedTeamVerdictLLM)
    return BacktestOrchestrator(
        config=cfg,
        loader=loader,
        sink=DecisionSink(engine),
        client_for=lambda a: red if a is AgentName.RED_TEAM else voter,
        cio_client=_client(cfg.models.tiers[ModelTier.STRONG], CioDecisionLLM),
        concurrency=4,
    )


def _count(engine: Engine, table: str, run_id: UUID | None = None) -> int:
    sql = f"SELECT count(*) FROM {table}" + (" WHERE run_id = :r" if run_id else "")
    with engine.connect() as c:
        return int(c.execute(text(sql), {"r": run_id} if run_id else {}).scalar_one())


def _status(engine: Engine, run_id: UUID) -> str:
    with engine.connect() as c:
        return str(
            c.execute(text("SELECT status FROM runs WHERE run_id = :r"), {"r": run_id}).scalar_one()
        )


@respx.mock
def test_real_persistence_failure_rolls_back_the_commit_and_resume_completes_it(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(engine)
    loader = Registering(StoreStepLoader(engine, brands=BRANDS))
    provider = Provider(loader)
    respx.post(URL).mock(side_effect=provider)
    orch = _orch(engine, loader)

    real = write.insert_portfolio_snapshot

    def boom(*_a: Any, **_k: Any) -> int:
        raise RuntimeError("disk full")

    # Fails after run, verdict and decision rows were written inside the transaction.
    monkeypatch.setattr(write, "insert_portfolio_snapshot", boom)
    with pytest.raises(RuntimeError, match="disk full"):
        orch.run_step(AS_OF)

    with engine.connect() as c:
        run_ids: list[UUID] = list(c.execute(text("SELECT run_id FROM runs")).scalars().all())
    (run_id,) = run_ids
    assert _status(engine, run_id) == RunStatus.FAILED.value  # never COMMITTED
    for table in ("portfolio_snapshots", "decision_commitments", "committee_decisions"):
        assert _count(engine, table) == 0, table  # the rolled-back step left nothing behind
    assert _count(engine, "agent_verdicts", run_id) == 4 * 5 + 4  # completed work kept for resume

    monkeypatch.setattr(write, "insert_portfolio_snapshot", real)
    calls = sum(1 for a in provider.agents if a is not None)
    resumed = orch.run_step(AS_OF, run_id=run_id)
    assert resumed.run_id == run_id and resumed.status is RunStatus.COMMITTED
    assert sum(1 for a in provider.agents if a is not None) == calls  # no voter call repeated
    assert _status(engine, run_id) == RunStatus.COMMITTED.value
    assert _count(engine, "portfolio_snapshots", run_id) == 1
    assert _count(engine, "decision_commitments", run_id) == 1
    assert _count(engine, "agent_verdicts", run_id) == 4 * 5 + 4  # no duplicates


@respx.mock
def test_real_store_never_demotes_a_committed_run_and_a_fresh_run_is_independent(
    engine: Engine,
) -> None:
    seed(engine)
    loader = Registering(StoreStepLoader(engine, brands=BRANDS))
    respx.post(URL).mock(side_effect=Provider(loader))
    orch = _orch(engine, loader)
    first = orch.run_step(AS_OF)
    assert first.status is RunStatus.COMMITTED

    record = DecisionSink(engine).load_run(first.run_id)
    assert record is not None
    DecisionSink(engine).flush_step(
        StepArtifacts(run=record.model_copy(update={"status": RunStatus.FAILED}))
    )
    assert _status(engine, first.run_id) == RunStatus.COMMITTED.value  # late FAILED is ignored

    second = orch.run_step(AS_OF)  # same mode, config and as_of: an independent new run
    assert second.run_id != first.run_id and second.status is RunStatus.COMMITTED
    assert _count(engine, "runs") == 2
    assert _count(engine, "decision_commitments") == 2


def test_committed_run_book_round_trips_through_the_store(engine: Engine) -> None:
    seed(engine)
    loader = Registering(StoreStepLoader(engine, brands=BRANDS))
    with respx.mock:
        respx.post(URL).mock(side_effect=Provider(loader))
        orch = _orch(engine, loader)
        first = orch.run_step(AS_OF)
        again = orch.run_step(AS_OF, run_id=first.run_id)
    assert again.book == first.book and again.status is RunStatus.COMMITTED


def test_a_real_commit_recomputes_to_its_stored_hash_and_any_edit_is_detected(
    engine: Engine,
) -> None:
    """The hash the pipeline wrote equals the hash recomputed from the rows Postgres returns."""
    seed(engine)
    loader = Registering(StoreStepLoader(engine, brands=BRANDS))
    with respx.mock:
        respx.post(URL).mock(side_effect=Provider(loader))
        first = _orch(engine, loader).run_step(AS_OF)
    material = DecisionSink(engine).load_commitment_material(first.run_id)
    assert material is not None and material.decisions  # rows round-tripped through JSONB
    assert verify_material(material) == material.stored_sha256

    for sql in (
        "UPDATE committee_decisions SET pooled_p = pooled_p / 2",
        "UPDATE committee_decisions SET rationale = coalesce(rationale, '') || 'x'",
        "UPDATE committee_decisions SET target_weight = 0.0 WHERE target_weight > 0",
        "UPDATE portfolio_snapshots SET cash_weight = cash_weight - 0.001",
        "UPDATE committee_decisions SET entity_token = NULL",
        "DELETE FROM committee_decisions WHERE horizon = 5",
    ):
        with engine.connect() as c:
            c.execute(text(sql))  # rolled back below: the stored rows stay untouched
            with pytest.raises(CommitmentIntegrityError):
                verify_material(_material(c, first.run_id))
            c.rollback()
    untouched = DecisionSink(engine).load_commitment_material(first.run_id)
    assert untouched is not None and verify_material(untouched) == material.stored_sha256


def _material(conn: Any, run_id: UUID) -> Any:
    found = write.load_commitment_material(conn, run_id)
    assert found is not None
    return found
