"""Paper-execution stage: the only bridge from a committed book to the broker (§9, P5 step 4).

A run reaches this stage only when it is LIVE (backtests never execute) and ``ANCHORED``, its
commitment hash *recomputed from the stored rows* equals the stored hash and the anchor, the anchor
holds an OpenTimestamps proof binding that digest and a git commit, and the final book carries a
CIO decision whenever it holds positions. ``COMMITTED`` alone never executes. A commitment mismatch
raises ``CommitmentIntegrityError`` before any broker call and is never retried automatically.
Anything else ineligible raises ``ExecutionNotEligibleError`` and nothing is sent.

Order of operations, chosen so a crash at any point is safe to replay:

1. Rebuild the kill switch from ``kill_switch_events`` (memory is only a cache). A halted run does
   not trade: open orders are cancelled and confirmed, and the halt itself moved the run to
   ``PARTIAL`` in the same transaction as its event row (§9). That state is terminal here.
2. Otherwise evaluate the automatic triggers (daily loss, stale feed) and require equity history
   for the drawdown record; missing evidence raises and nothing trades.
3. Reconcile each name against the broker by its stable client id. A name whose limit order already
   exists is driven from *that order's* side and quantity, never re-planned from today's positions,
   so a replay finishes the existing order instead of sizing (and sending) a second one.
4. Every order state is written by the executor as it arrives. Only after every name is complete
   and its limit-order evidence is stored does ``mark_executed`` run, as one conditional
   ``ANCHORED -> EXECUTED`` update. Any failure before that leaves the run ``ANCHORED``.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from config.loader import AppConfig
from contracts.commitment import CommitmentIntegrityError, CommitmentMaterial, verify_material
from contracts.enums import HALT_REASON_PREFIX, FeedName, OrderSide, RunMode, RunStatus
from contracts.models import (
    DlqRecord,
    ExecutionRecord,
    KillSwitchEvent,
    PortfolioSnapshot,
    ProposedBook,
    RunRecord,
)
from evaluation.anchoring import AnchorBindingError, check_binding
from execution.executor import (
    Clock,
    ExecutionOutcome,
    ExecutionRecorder,
    ExecutionTarget,
    OrderExecutor,
    ReferenceFn,
    Sleeper,
    client_order_id,
    refresh_record,
)
from execution.gateway import BrokerGateway
from execution.halt import HaltController
from risk.kill_switch import KillSwitch, was_flattened

log = logging.getLogger(__name__)


class ExecutionNotEligibleError(RuntimeError):
    """The run may not be executed: wrong mode or status, or no valid committed book."""


class ExecutionPlanError(RuntimeError):
    """The book cannot be turned into orders (unknown symbol or price): nothing is sent."""


class ExecutionStore(ExecutionRecorder, Protocol):
    def load_run(self, run_id: UUID) -> RunRecord | None: ...

    def load_commitment_material(self, run_id: UUID) -> CommitmentMaterial | None: ...

    def load_kill_switch_events(self, run_id: UUID) -> list[KillSwitchEvent]: ...

    def load_open_executions(self, run_id: UUID) -> list[ExecutionRecord]: ...

    def record_halt(self, event: KillSwitchEvent) -> bool: ...

    def record_dlq(self, records: tuple[DlqRecord, ...]) -> None: ...

    def mark_executed(self, run_id: UUID, ended_at: datetime) -> bool: ...


@dataclass(frozen=True)
class MarketView:
    """How to name and price the universe; supplied by the caller, never by the LLM."""

    symbols: Mapping[int, str]
    decision_prices: Mapping[int, float]


@dataclass(frozen=True)
class ExecutionResult:
    run_id: UUID
    status: RunStatus  # EXECUTED only when every name is complete and the run row advanced
    advanced: bool  # True only for the call that performed COMMITTED -> EXECUTED
    reason: str | None = None
    outcomes: tuple[ExecutionOutcome, ...] = ()


def plan_targets(
    *,
    run_id: UUID,
    book: ProposedBook,
    view: MarketView,
    equity: float,
    positions: Mapping[str, float],
) -> list[ExecutionTarget]:
    """Whole-share deltas from held positions to the book. Fails closed on any unknown name."""
    if not (math.isfinite(equity) and equity > 0):
        raise ExecutionPlanError("account equity is not a positive finite number")
    weights = {p.security_id: p.target_weight for p in book.positions}
    unknown = sorted(set(weights) - set(view.symbols))
    if unknown:
        raise ExecutionPlanError(f"book names without a symbol: {unknown}")
    out: list[ExecutionTarget] = []
    for sid in sorted(view.symbols):
        symbol, weight = view.symbols[sid], weights.get(sid, 0.0)
        held = positions.get(symbol, 0.0)
        price = view.decision_prices.get(sid)
        if price is None or not (math.isfinite(price) and price > 0):
            if weight > 0 or held != 0:
                raise ExecutionPlanError(f"no usable decision price for security {sid}")
            continue
        desired = math.floor(weight * equity / price) if weight > 0 else 0
        delta = round(desired - held, 6)
        if delta == 0:
            continue
        side = OrderSide.BUY if delta > 0 else OrderSide.SELL
        out.append(ExecutionTarget(run_id, sid, symbol, side, abs(delta), price))
    return out


class ExecutionStage:
    def __init__(
        self,
        *,
        config: AppConfig,
        gateway: BrokerGateway,
        store: ExecutionStore,
        market: Callable[[RunRecord], MarketView],
        reference: Callable[[str], ReferenceFn],
        feeds: Callable[[], Mapping[FeedName, datetime | None]],
        clock: Clock,
        sleep: Sleeper,
        executor_options: Mapping[str, Any] | None = None,
    ) -> None:
        self._cfg = config
        self._gw = gateway
        self._store = store
        self._market = market
        self._reference = reference
        self._feeds = feeds
        self._clock = clock
        self._sleep = sleep
        self._opts = dict(executor_options or {})

    def execute_run(self, run_id: UUID) -> ExecutionResult:
        run = self._store.load_run(run_id)
        if run is not None and _halted(run):  # terminal: only make sure nothing is left working
            return self._settle_halted(run)
        snapshot = self._eligible(run_id, run)
        assert run is not None
        if run.status is RunStatus.EXECUTED:
            return ExecutionResult(run_id, RunStatus.EXECUTED, advanced=False)

        ks, halt = self._halt_controller(run_id)
        if ks.halted:
            halt.settle_open_orders()
        else:
            halt.check(
                limit=float(self._cfg.risk.kill_switch_daily_loss),
                last_success=self._feeds(),
                sla_hours=self._cfg.pipeline.freshness_sla_hours,
            )
        if ks.halted:
            now = self._store.load_run(run_id)
            return ExecutionResult(
                run_id,
                now.status if now is not None else RunStatus.PARTIAL,
                False,
                f"kill switch engaged ({ks.trigger})",
            )

        executor = OrderExecutor(
            self._gw, ks, self._store, clock=self._clock, sleep=self._sleep, **self._opts
        )
        targets = self._targets(run, snapshot.book)

        def gate() -> None:
            """Re-evaluate the kill switch before new exposure (initial submits, market residuals).

            The durable events are read first, so a halt recorded by another process (an operator's
            manual halt) is honoured; the automatic triggers then run against the live account.
            Engaging it cancels and confirms everything open and records the halt atomically.
            """
            already = ks.halted
            for event in self._store.load_kill_switch_events(run_id):
                ks.halt(event.trigger)
            if ks.halted:
                if not already:
                    halt.settle_open_orders()
                return
            halt.check(
                limit=float(self._cfg.risk.kill_switch_daily_loss),
                last_success=self._feeds(),
                sla_hours=self._cfg.pipeline.freshness_sla_hours,
            )

        try:
            outcomes = executor.execute_book(
                targets, lambda t: self._reference(t.symbol), gate=gate
            )
        except Exception as exc:
            self._dead_letter(run, exc)
            raise
        if ks.halted:  # halted mid-batch: no new exposure was sent; the run is PARTIAL (§9)
            now = self._store.load_run(run_id)
            return ExecutionResult(
                run_id,
                now.status if now is not None else RunStatus.PARTIAL,
                False,
                f"kill switch engaged ({ks.trigger})",
                tuple(outcomes),
            )
        if not all(o.complete and not o.blocked_by_kill_switch for o in outcomes):
            return ExecutionResult(
                run_id, RunStatus.ANCHORED, False, "execution incomplete", tuple(outcomes)
            )
        missing = [
            t.security_id
            for t in targets
            if self._store.find_execution(client_order_id(run_id, t.security_id, "lim")) is None
        ]
        if missing:  # evidence must be durable before the state moves
            raise RuntimeError(f"no stored execution evidence for securities {missing}")
        advanced = self._store.mark_executed(run_id, self._clock())
        return ExecutionResult(run_id, RunStatus.EXECUTED, advanced, None, tuple(outcomes))

    def reconcile_orders(self, run_id: UUID) -> int:
        """Bring every stored order that still looks working up to the broker's state.

        The broker is authoritative. Needed after a halt (whose cancels the run row does not
        see) and after a crash between an order changing and its evidence being written.
        Returns how many rows changed; no order is ever submitted or cancelled here.
        """
        changed = []
        for stored in self._store.load_open_executions(run_id):
            fresh = refresh_record(stored, self._gw.get_order(stored.broker_order_id))
            if fresh != stored:
                changed.append(fresh)
        if changed:
            self._store.record_executions(changed)
        return len(changed)

    def manual_halt(self, run_id: UUID, *, flatten: bool = True) -> ExecutionResult:
        """The operator's kill switch: cancel and confirm every open order, then (by default)
        flatten. The run moves to ``PARTIAL`` through the audited halt transition, even from
        ``EXECUTED``; its commitment, anchor and order evidence stay untouched."""
        run = self._store.load_run(run_id)
        if run is None or run.mode is not RunMode.LIVE:
            raise ExecutionNotEligibleError(f"run {run_id} is not a stored live run")
        _, halt = self._halt_controller(run_id)
        halt.manual_halt(flatten=flatten)
        after = self._store.load_run(run_id)
        assert after is not None
        return ExecutionResult(run_id, after.status, False, after.status_reason)

    # ------------------------------------------------------------------------------------------

    def _halt_controller(self, run_id: UUID) -> tuple[KillSwitch, HaltController]:
        events = self._store.load_kill_switch_events(run_id)
        ks = KillSwitch.from_events(events)
        poll = {k: v for k, v in self._opts.items() if k.startswith("poll_")}
        halt = HaltController(
            self._gw,
            ks,
            self._store.record_halt,
            run_id=run_id,
            clock=self._clock,
            sleep=self._sleep,
            flattened=was_flattened(events),
            **poll,
        )
        return ks, halt

    def _settle_halted(self, run: RunRecord) -> ExecutionResult:
        _, halt = self._halt_controller(run.run_id)
        halt.settle_open_orders()
        return ExecutionResult(run.run_id, run.status, False, run.status_reason)

    def _eligible(self, run_id: UUID, run: RunRecord | None) -> PortfolioSnapshot:
        """Everything that must hold before the first broker call, checked from stored rows."""
        if run is None:
            raise ExecutionNotEligibleError(f"run {run_id} does not exist")
        if run.mode is not RunMode.LIVE:
            raise ExecutionNotEligibleError(f"{run.mode.value} runs never execute (§9)")
        if run.status not in (RunStatus.ANCHORED, RunStatus.EXECUTED):
            raise ExecutionNotEligibleError(f"run {run_id} is {run.status.value}, not ANCHORED")
        material = self._store.load_commitment_material(run_id)
        if material is None:
            raise ExecutionNotEligibleError(f"run {run_id} has no decision commitment")
        try:
            sha = verify_material(material)  # recompute: the stored hash alone is not trusted
            anchor = material.anchor
            if anchor is None or anchor.ots_proof is None or anchor.git_commit is None:
                raise CommitmentIntegrityError(
                    f"run {run_id} is {run.status.value} without a full anchor"
                )
            try:
                check_binding(anchor.ots_proof, sha)
            except AnchorBindingError as exc:
                raise CommitmentIntegrityError(f"run {run_id}: anchor proof: {exc}") from exc
        except CommitmentIntegrityError as exc:
            self._dead_letter(run, exc)
            raise
        snap = material.snapshot
        if snap.run_id != run_id or snap.book.run_id != run_id:
            raise ExecutionNotEligibleError(f"run {run_id} has no stored committed book")
        if snap.book.positions and snap.cio is None:
            raise ExecutionNotEligibleError(f"run {run_id} has positions but no CIO decision")
        return snap

    def _targets(self, run: RunRecord, book: ProposedBook) -> list[ExecutionTarget]:
        """A replay keeps each existing limit order's side and quantity; the rest are planned."""
        view = self._market(run)
        existing: dict[int, ExecutionTarget] = {}
        for sid, symbol in view.symbols.items():
            order = self._gw.find_by_client_id(client_order_id(run.run_id, sid, "lim"))
            if order is not None:
                price = view.decision_prices.get(sid, order.limit_price or 0.0)
                stored = self._store.find_execution(order.client_order_id)
                existing[sid] = ExecutionTarget(
                    run.run_id,
                    sid,
                    symbol,
                    order.side,
                    order.qty,
                    stored.decision_price if stored is not None else price,
                )
        equity, _ = self._gw.account_equity()
        fresh = [
            t
            for t in plan_targets(
                run_id=run.run_id,
                book=book,
                view=view,
                equity=equity,
                positions=self._gw.positions(),
            )
            if t.security_id not in existing
        ]
        return sorted([*existing.values(), *fresh], key=lambda t: t.security_id)

    def _dead_letter(self, run: RunRecord, exc: Exception) -> None:
        try:
            self._store.record_dlq(
                (
                    DlqRecord(
                        run_id=run.run_id,
                        as_of=run.as_of,
                        agent="executor",
                        error_type=type(exc).__name__[:100],
                        payload={"detail": str(exc)[:500]},
                    ),
                )
            )
        except Exception:
            log.exception("could not dead-letter execution failure for run %s", run.run_id)


__all__ = [
    "ExecutionNotEligibleError",
    "ExecutionPlanError",
    "ExecutionResult",
    "ExecutionStage",
    "ExecutionStore",
    "MarketView",
    "plan_targets",
]


def _halted(run: RunRecord) -> bool:
    """A run a kill-switch halt moved to PARTIAL (§9): terminal, recovered only by a new run."""
    return run.status is RunStatus.PARTIAL and (run.status_reason or "").startswith(
        HALT_REASON_PREFIX
    )
