"""P5 end to end: Postgres, the real pipeline, real anchoring and the real Alpaca gateway.

Everything external is a local stand-in and nothing leaves the machine: OpenRouter and the
OpenTimestamps calendars are respx mocks, Alpaca is a stateful ``MockTransport`` behind the real
``AlpacaPaperGateway``, and the "public" git remote is a bare repository on disk. Only the wiring
under test is real: Celery task logic, the sink and its transactions, the loader over the store, the
anchor and execution stages, the kill switch and the calendar.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation
from sqlalchemy import Engine, text

from contracts.commitment import CommitmentIntegrityError, verify_material
from contracts.enums import AgentName, ModelTier, RunMode, RunStatus
from contracts.models import AgentVerdictLLM, CioDecisionLLM, RedTeamVerdictLLM
from evaluation import anchoring
from execution.alpaca import AlpacaPaperGateway
from orchestration import tasks
from orchestration.anchor_adapters import GitAnchorRepo, OtsCalendars
from orchestration.anchor_stage import AnchorStage, AnchorUpgrader
from orchestration.execution_stage import ExecutionNotEligibleError, ExecutionStage
from orchestration.loader import StoreStepLoader
from orchestration.pipeline import BacktestOrchestrator, config_fingerprint
from orchestration.providers import StoreMarketView, reference_provider, store_feeds
from orchestration.sink import DecisionSink
from store import write
from tests.agents.world import AS_OF
from tests.execution.fake_alpaca import FakeAlpaca, weekday_calendar
from tests.execution.fakes import FakeClock
from tests.orchestration.anchoring_support import calendar_reply
from tests.orchestration.store_support import seed
from tests.orchestration.test_pipeline import BRANDS, URL, Provider, _client, cfg_with
from tests.orchestration.test_store_run import Registering

FRIDAY_NIGHT = datetime(2025, 3, 7, 22, 0, tzinfo=UTC)  # after the 16:00 ET close (= AS_OF)
MONDAY_1030 = datetime(2025, 3, 10, 14, 30, tzinfo=UTC)  # 10:30 EDT, inside the window
CALS = ["https://a.cal.example", "https://b.cal.example"]


class Quotes:
    """A tight IEX quote around the decision price, so every reference is an IEX midpoint."""

    def iex_quote(self, symbol: str) -> tuple[float, float] | None:
        return 9.99, 10.01

    def sip_last_trade(self, symbol: str, *, at_or_before: datetime) -> float | None:
        return None


@pytest.fixture
def engine(pg_engine: Engine) -> Iterator[Engine]:
    def wipe() -> None:
        with pg_engine.begin() as c:
            c.execute(text("TRUNCATE runs, securities RESTART IDENTITY CASCADE"))
            c.execute(
                text(
                    "TRUNCATE universe_snapshots, features, fundamentals_asfiled, insider_txns, "
                    "news_items, price_bars, feed_health RESTART IDENTITY CASCADE"
                )
            )

    wipe()
    yield pg_engine
    wipe()


class Rig:
    def __init__(self, engine: Engine, tmp_path: Path, *, budget: float = 1000.0) -> None:
        seed(engine)
        cfg = cfg_with(budget)
        self.cfg, self.engine = cfg, engine
        self.sink = DecisionSink(engine)
        self.clock = FakeClock()
        self.clock.now = MONDAY_1030
        # the store says every required feed ingested minutes ago (the beat polls news every 15 min;
        # the kill switch is re-checked after the 15-minute limit window, so it must still be fresh)
        with engine.begin() as c:
            for feed in cfg.pipeline.freshness_sla_hours:
                write.record_feed_run(c, feed, at=MONDAY_1030 - timedelta(minutes=5), rows=1)

        self.remote = tmp_path / "public.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True)
        self.git = GitAnchorRepo(tmp_path / "clone", str(self.remote), "main")
        self.http = httpx.Client()
        self.ots = OtsCalendars(CALS, self.http, min_success=2)
        self.anchor = AnchorStage(
            store=self.sink, stamper=self.ots, git=self.git, clock=lambda: FRIDAY_NIGHT
        )

        self.alpaca = FakeAlpaca(self.clock)
        self.alpaca.calendar = weekday_calendar(date(2025, 3, 3), 21)
        self.gateway = AlpacaPaperGateway(self.alpaca.transport(), env={})
        self.execution = ExecutionStage(
            config=cfg,
            gateway=self.gateway,
            store=self.sink,
            market=StoreMarketView(engine, self.gateway.positions),
            reference=reference_provider(Quotes(), self.clock),
            feeds=store_feeds(engine),
            clock=self.clock,
            sleep=self.clock.sleep,
            executor_options={"poll_attempts": 3},
        )
        self.loader = Registering(StoreStepLoader(engine, brands=BRANDS))
        self.provider = Provider(self.loader)
        self.route = respx.post(URL).mock(side_effect=self.provider)
        for cal in CALS:
            respx.post(f"{cal}/digest").mock(side_effect=_accept(cal))
        self.rt = self.runtime(cfg)

    def orchestrator(
        self, cfg: Any, mode: RunMode = RunMode.LIVE, *, anchoring_enabled: bool = True
    ) -> BacktestOrchestrator:
        voter = _client(cfg.models.tiers[ModelTier.FAST], AgentVerdictLLM)
        red = _client(cfg.models.tiers[ModelTier.STRONG], RedTeamVerdictLLM)
        return BacktestOrchestrator(
            config=cfg,
            loader=self.loader,
            sink=self.sink,
            client_for=lambda a: red if a is AgentName.RED_TEAM else voter,
            cio_client=_client(cfg.models.tiers[ModelTier.STRONG], CioDecisionLLM),
            concurrency=4,
            mode=mode,
            anchoring=self.anchor if anchoring_enabled else None,
            clock=lambda: (
                FRIDAY_NIGHT + timedelta(hours=6)
            ),  # later than every claim in these tests
        )

    def runtime(self, cfg: Any, *, commit_only: bool = False) -> tasks.Runtime:
        return tasks.Runtime(
            sink=self.sink,
            # anchors after committing and never executes (Friday sends no orders); ``commit_only``
            # simulates a crash between the commit and the anchoring stage
            pipeline=self.orchestrator(cfg, anchoring_enabled=not commit_only),
            anchoring=self.anchor,
            execution=self.execution,
            upgrader=AnchorUpgrader(
                store=self.sink, stamper=self.ots, headers=None, clock=lambda: MONDAY_1030
            ),
            calendar=self.gateway,
            config_hash=config_fingerprint(cfg),
            clock=self.clock,
        )

    def count(self, table: str, run_id: UUID | None = None) -> int:
        sql = f"SELECT count(*) FROM {table}" + (" WHERE run_id = :r" if run_id else "")
        with self.engine.connect() as c:
            return int(c.execute(text(sql), {"r": run_id} if run_id else {}).scalar_one())

    def trading_requests(self) -> list[str]:
        """Every request except the (scheduling-only) calendar: orders, positions, account."""
        return [
            f"{r.method} {r.url.path}" for r in self.alpaca.requests if r.url.path != "/v2/calendar"
        ]

    def remote_files(self) -> list[str]:
        out = subprocess.run(
            ["git", "-C", str(self.remote), "ls-tree", "-r", "--name-only", "main"],
            capture_output=True,
            text=True,
        )
        return out.stdout.split() if out.returncode == 0 else []


def _accept(cal: str) -> Any:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=calendar_reply(request.content, PendingAttestation(cal)))

    return answer


def friday_week(rig: Rig) -> UUID:
    out = tasks.weekly_run(rig.rt, now=FRIDAY_NIGHT)
    assert out["status"] == "ANCHORED", out
    return UUID(out["run_id"])


# --- the full week ----------------------------------------------------------------------------


@respx.mock
def test_a_week_from_friday_close_to_reconciled_paper_orders(
    engine: Engine, tmp_path: Path
) -> None:
    rig = Rig(engine, tmp_path)
    run_id = friday_week(rig)

    # Friday: committed, then anchored before anything could execute.
    assert rig.alpaca.submitted == []  # no order on Friday
    material = rig.sink.load_commitment_material(run_id)
    assert material is not None and material.anchor is not None
    sha = verify_material(material)  # recomputed from the Postgres rows
    assert sha == material.stored_sha256 == material.anchor.sha256
    assert anchoring.check_binding(material.anchor.ots_proof or b"", sha).pending
    assert rig.remote_files() == [anchoring.manifest_path(run_id)]  # on the "public" remote
    assert rig.git.is_reachable(material.anchor.git_commit or "")
    run = rig.sink.load_run(run_id)
    assert run is not None and run.status is RunStatus.ANCHORED and run.mode is RunMode.LIVE
    assert rig.count("agent_verdicts", run_id) > 0 and rig.count("committee_decisions", run_id) > 0
    positions = [p for p in material.snapshot.book.positions if p.target_weight > 0]
    assert positions, "the fixture world should produce a book worth trading"

    # A duplicate delivery of the weekly beat changes nothing.
    calls = rig.route.call_count
    dup = tasks.weekly_run(rig.rt, now=FRIDAY_NIGHT + timedelta(minutes=5))
    assert dup["created"] is False and rig.count("runs") == 1 and rig.route.call_count == calls

    # Too early on the weekend, then Monday inside the window.
    assert (
        tasks.execute(rig.rt, run_id, now=datetime(2025, 3, 8, 15, tzinfo=UTC))["skipped"]
        == "too early"
    )
    out = tasks.execute(rig.rt, run_id, now=MONDAY_1030)
    assert out["status"] == "EXECUTED" and out["advanced"] is True

    # Paper orders exist at the broker, are stored with their evidence, and match each other.
    lim_ids = sorted(c for c in rig.alpaca.submitted if c.endswith("-lim"))
    assert len(lim_ids) == len(positions)
    with engine.connect() as c:
        rows = c.execute(
            text(
                "SELECT client_order_id, status, filled_qty, reference_source, decision_price, "
                "reference_price FROM orders WHERE run_id = :r ORDER BY 1"
            ),
            {"r": run_id},
        ).all()
    assert [r[0] for r in rows] == sorted(rig.alpaca.submitted)
    assert {r[1] for r in rows} == {"filled"} and {r[3] for r in rows} == {"iex_mid"}
    assert all(r[4] == 10.0 and abs(r[5] - 10.0) < 1e-9 for r in rows)
    assert sum(rig.alpaca.positions.values()) > 0  # shares were bought at the broker
    executed = rig.sink.load_run(run_id)
    assert executed is not None and executed.status is RunStatus.EXECUTED

    # Reconciliation finds nothing to change, and a redelivered execute sends nothing new.
    assert tasks.reconcile(rig.rt, run_id)["changed"] == 0
    sent = list(rig.alpaca.submitted)
    assert tasks.execute(rig.rt, run_id, now=MONDAY_1030)["status"] == "EXECUTED"
    assert rig.alpaca.submitted == sent
    assert rig.count("kill_switch_events") == 0 and rig.count("dlq_records") == 0


@respx.mock
def test_a_limit_order_that_only_part_fills_is_cancelled_then_completed_at_market(
    engine: Engine, tmp_path: Path
) -> None:
    rig = Rig(engine, tmp_path)
    run_id = friday_week(rig)
    material = rig.sink.load_commitment_material(run_id)
    assert material is not None
    held = [p for p in material.snapshot.book.positions if p.target_weight > 0]
    with engine.connect() as c:
        tickers: dict[int, str] = {
            r[0]: r[1] for r in c.execute(text("SELECT security_id, ticker FROM securities"))
        }
    rig.alpaca.partial = {tickers[held[0].security_id]}

    out = tasks.execute(rig.rt, run_id, now=MONDAY_1030)
    assert out["status"] == "EXECUTED"
    ids = rig.alpaca.submitted
    assert (
        f"{run_id}-{held[0].security_id}-lim" in ids
        and f"{run_id}-{held[0].security_id}-mkt" in ids
    )
    lim = rig.alpaca._by_client(f"{run_id}-{held[0].security_id}-lim")
    assert lim is not None and lim["status"] == "canceled"  # cancelled and confirmed first
    assert rig.clock.now >= MONDAY_1030 + timedelta(minutes=15)  # the 15-minute limit window
    with engine.connect() as c:
        stored: dict[str, str] = {
            r[0]: r[1]
            for r in c.execute(
                text("SELECT client_order_id, status FROM orders WHERE run_id = :r"), {"r": run_id}
            )
        }
    assert stored[f"{run_id}-{held[0].security_id}-lim"] == "canceled"
    assert stored[f"{run_id}-{held[0].security_id}-mkt"] == "filled"


# --- failed run -> fresh run ------------------------------------------------------------------


@respx.mock
def test_a_failed_run_is_followed_by_a_fresh_run_that_queues_every_name(
    engine: Engine, tmp_path: Path
) -> None:
    rig = Rig(engine, tmp_path)
    tiny = cfg_with(2.0)  # a budget the first attempt exceeds mid-run
    rig.rt = rig.runtime(tiny)
    first = tasks.weekly_run(rig.rt, now=FRIDAY_NIGHT)
    first_id = UUID(first["run_id"])
    assert first["status"] == "PARTIAL"
    assert rig.count("decision_commitments", first_id) == 0  # never committed, never anchored
    partial_verdicts = rig.count("agent_verdicts", first_id)
    calls_before = rig.route.call_count

    rig.rt = rig.runtime(rig.cfg)  # the operator's budget is fixed; the beat fires again
    second = tasks.weekly_run(rig.rt, now=FRIDAY_NIGHT + timedelta(minutes=30))
    second_id = UUID(second["run_id"])
    assert second["created"] is True and second_id != first_id  # a fresh run_id, not a resume
    assert second["status"] == "ANCHORED"
    assert rig.count("agent_verdicts", second_id) > partial_verdicts  # every name queued again
    assert rig.route.call_count > calls_before
    old = rig.sink.load_run(first_id)
    assert old is not None and old.status is RunStatus.PARTIAL  # the old run is left as it was
    assert (
        rig.count("agent_verdicts", first_id) == partial_verdicts
    )  # and not mutated by the new one


# --- kill switch ------------------------------------------------------------------------------


@respx.mock
def test_a_halt_is_partial_and_recovery_is_a_fresh_run(engine: Engine, tmp_path: Path) -> None:
    rig = Rig(engine, tmp_path)
    run_id = friday_week(rig)
    rig.alpaca.equity, rig.alpaca.last_equity = 90_000.0, 100_000.0  # down 10% on the day

    out = tasks.execute(rig.rt, run_id, now=MONDAY_1030)
    assert out["status"] == "PARTIAL"
    assert rig.alpaca.submitted == []  # a halt before any order
    halted = rig.sink.load_run(run_id)
    assert halted is not None and halted.status_reason == "kill_switch:daily_loss"
    assert rig.count("kill_switch_events", run_id) == 1
    assert (
        rig.count("decision_commitments", run_id) == 1
        and rig.count("commitment_anchors", run_id) == 1
    )
    material = rig.sink.load_commitment_material(run_id)
    assert material is not None and verify_material(material)  # still verifiable after the halt

    # The account recovers; the halted run stays halted and nothing trades under it.
    rig.alpaca.equity = rig.alpaca.last_equity = 100_000.0
    assert tasks.execute(rig.rt, run_id, now=MONDAY_1030)["halted"] is True
    assert rig.alpaca.submitted == []
    assert tasks.advance_run(rig.rt, run_id)["status"] == "PARTIAL"
    assert tasks.weekly_run(rig.rt, now=FRIDAY_NIGHT + timedelta(hours=1))["skipped"] == "halted"
    with pytest.raises(Exception, match="immutable"):  # reset can never erase halt evidence
        rig.sink.reset_run(run_id, actor="ops", reason="x", now=MONDAY_1030)

    # Recovery: a fresh run for the same as_of, anchored on its own, executes normally.
    fresh = tasks.weekly_run(rig.rt, now=FRIDAY_NIGHT + timedelta(hours=1), fresh=True)
    fresh_id = UUID(fresh["run_id"])
    assert fresh["created"] is True and fresh_id != run_id and fresh["status"] == "ANCHORED"
    assert tasks.execute(rig.rt, fresh_id, now=MONDAY_1030)["status"] == "EXECUTED"
    assert all(cid.startswith(str(fresh_id)) for cid in rig.alpaca.submitted)
    assert rig.count("kill_switch_events", fresh_id) == 0


@respx.mock
def test_a_manual_flatten_after_execution_marks_the_run_partial_and_keeps_its_evidence(
    engine: Engine, tmp_path: Path
) -> None:
    rig = Rig(engine, tmp_path)
    run_id = friday_week(rig)
    assert tasks.execute(rig.rt, run_id, now=MONDAY_1030)["status"] == "EXECUTED"
    orders_before = rig.count("orders", run_id)

    result = rig.execution.manual_halt(run_id)
    assert result.status is RunStatus.PARTIAL and result.reason == "kill_switch:manual"
    assert rig.alpaca.closed and all(q == 0 for q in rig.alpaca.positions.values())
    assert rig.count("orders", run_id) == orders_before
    assert (
        rig.count("commitment_anchors", run_id) == 1
        and rig.count("decision_commitments", run_id) == 1
    )
    (event,) = rig.sink.load_kill_switch_events(run_id)
    assert event.flattened and event.trigger.value == "manual"


# --- integrity --------------------------------------------------------------------------------


@respx.mock
def test_a_tampered_commitment_is_never_anchored_or_executed(
    engine: Engine, tmp_path: Path
) -> None:
    rig = Rig(engine, tmp_path)
    rig.rt = rig.runtime(rig.cfg, commit_only=True)
    out = tasks.weekly_run(rig.rt, now=FRIDAY_NIGHT)
    run_id = UUID(out["run_id"])
    assert out["status"] == "COMMITTED"
    with engine.begin() as c:
        c.execute(
            text("UPDATE committee_decisions SET target_weight = 0.08 WHERE target_weight > 0")
        )

    with pytest.raises(CommitmentIntegrityError):
        tasks.advance_run(rig.rt, run_id)
    assert rig.remote_files() == [] and not [
        r for r in respx.calls if "digest" in str(r.request.url)
    ]
    with pytest.raises(ExecutionNotEligibleError, match="not ANCHORED"):
        rig.execution.execute_run(run_id)  # COMMITTED never executes, whatever its hash
    assert rig.alpaca.submitted == [] and rig.trading_requests() == []
    assert not tasks.is_transient(CommitmentIntegrityError("x"))  # and Celery would never retry it
    run = rig.sink.load_run(run_id)
    assert run is not None and run.status is RunStatus.COMMITTED
    assert rig.count("commitment_anchors") == 0


@respx.mock
def test_tampering_after_anchoring_stops_execution_before_any_broker_call(
    engine: Engine, tmp_path: Path
) -> None:
    rig = Rig(engine, tmp_path)
    run_id = friday_week(rig)
    with engine.begin() as c:  # rows edited after the anchor: heavier weights than were committed
        c.execute(
            text("UPDATE committee_decisions SET target_weight = 0.08 WHERE target_weight > 0")
        )
    with pytest.raises(CommitmentIntegrityError):
        tasks.execute(rig.rt, run_id, now=MONDAY_1030)
    assert rig.trading_requests() == []  # not one call to the broker, not even a read
    run = rig.sink.load_run(run_id)
    assert run is not None and run.status is RunStatus.ANCHORED  # left as it was, for an operator
    assert [d for d in _dlq(rig, run_id)] == ["CommitmentIntegrityError"]
    assert not tasks.is_transient(CommitmentIntegrityError("x"))  # and never auto-retried


def _dlq(rig: Rig, run_id: UUID) -> list[str]:
    with rig.engine.connect() as c:
        return [
            r[0]
            for r in c.execute(
                text("SELECT error_type FROM dlq_records WHERE run_id = :r"), {"r": run_id}
            )
        ]


@respx.mock
def test_concurrent_deliveries_of_execute_send_each_order_once(
    engine: Engine, tmp_path: Path
) -> None:
    rig = Rig(engine, tmp_path)
    run_id = friday_week(rig)
    with rig.sink.run_lock(run_id, "execute") as held:  # another worker is executing right now
        assert held is True
        assert tasks.execute(rig.rt, run_id, now=MONDAY_1030)["skipped"] == "locked"
        assert rig.alpaca.submitted == []
    assert tasks.execute(rig.rt, run_id, now=MONDAY_1030)["status"] == "EXECUTED"
    assert len(rig.alpaca.submitted) == len(set(rig.alpaca.submitted))


@respx.mock
def test_a_backtest_run_is_anchored_and_stops(engine: Engine, tmp_path: Path) -> None:
    rig = Rig(engine, tmp_path)
    results = rig.orchestrator(rig.cfg, RunMode.BACKTEST).run_backtest(AS_OF, AS_OF)
    (result,) = results
    assert result.status is RunStatus.ANCHORED  # never COMMITTED-and-stuck, never EXECUTED
    material = rig.sink.load_commitment_material(result.run_id)
    assert material is not None and material.anchor is not None and verify_material(material)
    assert rig.alpaca.submitted == [] and rig.trading_requests() == []  # no broker contact at all


# --- the sweeper and Bitcoin confirmation -----------------------------------------------------


@respx.mock
def test_the_sweeper_finishes_what_a_lost_queue_left_and_execution_stays_idempotent(
    engine: Engine, tmp_path: Path
) -> None:
    rig = Rig(engine, tmp_path)
    rig.rt = rig.runtime(rig.cfg, commit_only=True)
    out = tasks.weekly_run(rig.rt, now=FRIDAY_NIGHT)  # commits, and the "queue" then loses the rest
    run_id = UUID(out["run_id"])
    assert out["status"] == "COMMITTED"

    queued: dict[str, list[str]] = {"advance": [], "execute": [], "reconcile": []}

    def sweep(now: datetime) -> dict[str, list[str]]:
        return tasks.sweep(
            rig.rt,
            advance=queued["advance"].append,
            execute_=queued["execute"].append,
            reconcile_=queued["reconcile"].append,
            now=now,
        )

    assert sweep(FRIDAY_NIGHT)["advance"] == [str(run_id)]  # found in Postgres, not in Redis
    tasks.advance_run(rig.rt, run_id)  # the re-enqueued task runs: anchoring only
    assert rig.sink.load_run(run_id).status is RunStatus.ANCHORED  # type: ignore[union-attr]
    assert sweep(MONDAY_1030 - timedelta(hours=1))["execute"] == []
    assert sweep(MONDAY_1030)["execute"] == [str(run_id)]
    tasks.execute(rig.rt, run_id, now=MONDAY_1030)
    assert sweep(MONDAY_1030)["execute"] == []  # executed: nothing left to do
    assert sweep(MONDAY_1030)["reconcile"] == []  # every order terminal


@respx.mock
def test_bitcoin_confirmation_is_verified_against_a_block_header_before_it_counts(
    engine: Engine, tmp_path: Path
) -> None:
    rig = Rig(engine, tmp_path)
    run_id = friday_week(rig)
    (pending,) = rig.sink.anchors_awaiting_confirmation()
    file = anchoring.loads(pending.ots_proof or b"")
    leaf = next(iter(file.timestamp.all_attestations()))[0]
    for cal in CALS:  # the calendars now hold the completed proof
        respx.get(f"{cal}/timestamp/{leaf.hex()}").mock(
            return_value=httpx.Response(
                200, content=calendar_reply(leaf, BitcoinBlockHeaderAttestation(800_000))
            )
        )

    class Headers:
        def header(self, height: int) -> Any:
            from types import SimpleNamespace

            return SimpleNamespace(hashMerkleRoot=leaf, nTime=1_700_000_000)

    upgrader = AnchorUpgrader(
        store=rig.sink, stamper=rig.ots, headers=Headers(), clock=lambda: MONDAY_1030
    )
    summary = upgrader.upgrade_all()
    assert (summary.upgraded, summary.confirmed, summary.failed) == (1, 1, 0)
    assert rig.sink.anchors_awaiting_confirmation() == []
    anchor = rig.sink.load_anchor(run_id)
    assert anchor is not None and anchor.verified_at == MONDAY_1030
    assert anchoring.check_binding(anchor.ots_proof or b"", anchor.sha256).confirmed
    run = rig.sink.load_run(run_id)
    assert run is not None and run.status is RunStatus.ANCHORED  # confirmation never moves the run
