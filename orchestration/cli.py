"""Operator CLI: ``python -m orchestration.cli <command>`` (same style as ``ingest.backfill``).

Commands act on the same Postgres-backed logic the Celery tasks use; nothing here has a private
code path. Every command prints one JSON document; exit status is 0 on success, 2 when the startup
gates refuse (owner configuration missing), and 1 for any other failure.

- ``run-backtest --start D --end D``  weekly steps, each ending ``ANCHORED`` (never executed)
- ``run-live [--fresh]``              the latest week-final close; ``--fresh`` starts a new run for
                                      an ``as_of`` whose earlier run was halted (recovery)
- ``anchor RUN`` / ``execute RUN`` / ``reconcile RUN`` / ``upgrade-anchors``
- ``inspect-run RUN``                 status, recomputed commitment, anchor, halts, open orders
- ``manual-halt RUN [--no-flatten]``  the operator kill switch (§9): cancel, confirm, flatten
- ``reset-run RUN --actor A --reason R``  pre-commitment runs only; audit rows are never cleared
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time
from typing import Any, TextIO
from uuid import UUID

from contracts.commitment import CommitmentIntegrityError, verify_material
from evaluation.anchoring import AnchorBindingError, check_binding
from orchestration import tasks
from orchestration.pipeline import BacktestOrchestrator
from orchestration.startup import StartupConfigError


def _dt(text: str) -> datetime:
    """``YYYY-MM-DD`` (21:00 UTC that day, after the US close) or an ISO timestamp with a zone."""
    try:
        day = date.fromisoformat(text)
    except ValueError:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            raise argparse.ArgumentTypeError("timestamps need a UTC offset") from None
        return parsed
    return datetime.combine(day, time(21, 0), UTC)


def _run_id(text: str) -> UUID:
    try:
        return UUID(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a run id: {text!r}") from exc


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orchestration.cli", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)
    bt = sub.add_parser("run-backtest", help="weekly backtest steps, anchored, never executed")
    bt.add_argument("--start", type=_dt, required=True)
    bt.add_argument("--end", type=_dt, required=True)
    live = sub.add_parser("run-live", help="run the latest week-final close (no orders yet)")
    live.add_argument("--fresh", action="store_true", help="new run for a halted as_of (recovery)")
    for name in ("anchor", "execute", "reconcile", "inspect-run"):
        sub.add_parser(name).add_argument("run_id", type=_run_id)
    sub.add_parser("upgrade-anchors")
    halt = sub.add_parser("manual-halt", help="operator kill switch: cancel, confirm, flatten")
    halt.add_argument("run_id", type=_run_id)
    halt.add_argument("--no-flatten", action="store_true")
    reset = sub.add_parser("reset-run", help="reset a run that never committed")
    reset.add_argument("run_id", type=_run_id)
    reset.add_argument("--actor", required=True)
    reset.add_argument("--reason", required=True)
    return p


def inspect(rt: tasks.Runtime, run_id: UUID) -> dict[str, Any]:
    run = rt.sink.load_run(run_id)
    if run is None:
        raise LookupError(f"run {run_id} does not exist")
    out: dict[str, Any] = {
        "run_id": str(run_id),
        "mode": run.mode.value,
        "as_of": run.as_of.isoformat(),
        "status": run.status.value,
        "status_reason": run.status_reason,
        "total_cost_usd": run.total_cost_usd,
    }
    try:
        material = rt.sink.load_commitment_material(run_id)
        if material is None:
            out["commitment"] = {"present": False}
        else:
            sha = verify_material(material)  # recomputed from the stored rows, not trusted
            out["commitment"] = {"present": True, "sha256": sha, "verified": True}
            anchor = material.anchor
            if anchor is not None and anchor.ots_proof is not None:
                status = check_binding(anchor.ots_proof, sha)
                out["anchor"] = {
                    "git_commit": anchor.git_commit,
                    "ots_pending": list(status.pending),
                    "bitcoin_heights": list(status.bitcoin_heights),
                    "bitcoin_verified_at": None
                    if anchor.verified_at is None
                    else anchor.verified_at.isoformat(),
                }
            else:
                out["anchor"] = None
    except (CommitmentIntegrityError, AnchorBindingError) as exc:
        out["commitment"] = {"present": True, "verified": False, "error": str(exc)}
    out["halts"] = [
        {"trigger": e.trigger.value, "at": e.triggered_at.isoformat(), "flattened": e.flattened}
        for e in rt.sink.load_kill_switch_events(run_id)
    ]
    out["open_orders"] = [r.broker_order_id for r in rt.sink.load_open_executions(run_id)]
    return out


def run(
    argv: Sequence[str] | None = None,
    *,
    runtime_factory: Callable[[], tasks.Runtime],
    backtest_factory: Callable[[], BacktestOrchestrator],
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    args = parser().parse_args(argv)
    try:
        result = _dispatch(args, runtime_factory, backtest_factory)
    except StartupConfigError as exc:
        print(str(exc), file=err)
        return 2
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=err)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True, default=str), file=out)
    return 0


def _dispatch(
    args: argparse.Namespace,
    runtime_factory: Callable[[], tasks.Runtime],
    backtest_factory: Callable[[], BacktestOrchestrator],
) -> dict[str, Any]:
    if args.command == "run-backtest":
        results = backtest_factory().run_backtest(args.start, args.end)
        return {
            "steps": [
                {"run_id": str(r.run_id), "as_of": r.as_of.isoformat(), "status": r.status.value,
                 "reason": r.reason, "cost_usd": r.cost_usd}
                for r in results
            ]
        }  # fmt: skip
    rt = runtime_factory()
    now = rt.clock()
    if args.command == "run-live":
        return tasks.weekly_run(rt, now=now, fresh=args.fresh)
    if args.command == "anchor":
        return tasks.advance_run(rt, args.run_id)
    if args.command == "execute":
        return tasks.execute(rt, args.run_id, now=now)
    if args.command == "reconcile":
        return tasks.reconcile(rt, args.run_id)
    if args.command == "upgrade-anchors":
        s = rt.upgrader.upgrade_all()
        return {
            "checked": s.checked,
            "upgraded": s.upgraded,
            "confirmed": s.confirmed,
            "failed": s.failed,
        }
    if args.command == "inspect-run":
        return inspect(rt, args.run_id)
    if args.command == "manual-halt":
        res = rt.execution.manual_halt(args.run_id, flatten=not args.no_flatten)
        return {"run_id": str(args.run_id), "status": res.status.value, "reason": res.reason}
    if args.command == "reset-run":
        cleared = rt.sink.reset_run(args.run_id, actor=args.actor, reason=args.reason, now=now)
        return {"run_id": str(args.run_id), "cleared": cleared}
    raise AssertionError(args.command)  # pragma: no cover - argparse enforces the choices


def main(argv: Sequence[str] | None = None) -> int:
    from contracts.enums import RunMode
    from orchestration.bootstrap import build_runtime, build_services

    return run(
        argv,
        runtime_factory=build_runtime,
        backtest_factory=lambda: build_services(live=False).orchestrator(RunMode.BACKTEST),
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
