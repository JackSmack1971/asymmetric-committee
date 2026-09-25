from datetime import UTC, datetime, timedelta

from config.loader import RiskConfig
from contracts.data import FeatureRow
from contracts.enums import BearSeverity
from evaluation.walkforward import WalkForwardInputs, WalkForwardResult, run_walkforward
from gate.model import GateLabel, recall

CFG = RiskConfig(
    long_only=True,
    max_position=0.08,
    max_sector=0.30,
    vol_target_annual=0.12,
    min_position=0.01,
    entry_threshold=0.56,
    k=0.02,
    dispersion_lambda=0.5,
    bear_multiplier={
        BearSeverity.LOW: 1.0,
        BearSeverity.MED: 1.0,
        BearSeverity.HIGH: 0.5,
    },
    cio_veto_budget=0.2,
    kill_switch_daily_loss=0.03,
    modeled_cost_bps=5,
)


def _row(sid: int, at: datetime, signal: float) -> FeatureRow:
    return FeatureRow(
        security_id=sid,
        event_time=at,
        available_at=at,
        source_version="fs_v1",
        feature_set_version="fs_v1",
        values={"momentum_12_1": signal},
    )


def test_twelve_week_smoke_produces_twelve_persisted_books() -> None:
    start = datetime(2025, 1, 3, tzinfo=UTC)
    dates = [start + timedelta(weeks=i) for i in range(12)]
    historical_dates = [start - timedelta(weeks=i + 5) for i in range(9)]
    training = tuple(_row(sid, at, float(sid)) for at in historical_dates for sid in range(1, 4))
    labels = tuple(
        GateLabel(sid, at, at + timedelta(days=22), sid == 3)
        for at in historical_dates
        for sid in range(1, 4)
    )

    def load(at: datetime) -> WalkForwardInputs:
        return WalkForwardInputs(
            rows=tuple(_row(sid, at, float(sid)) for sid in range(1, 4)),
            training_features=training,
            labels=labels,
            sectors={1: "A", 2: "A", 3: "B"},
            volatilities={1: 0.2, 2: 0.2, 3: 0.2},
            entity_tokens={1: "TICKER_01", 2: "TICKER_02", 3: "TICKER_03"},
        )

    persisted: list[WalkForwardResult] = []
    results = run_walkforward(
        dates=dates, load=load, persist=persisted.append, risk_config=CFG, gate_k=1
    )
    assert len(results) == len(persisted) == 12
    assert len({result.book.run_id for result in persisted}) == 12
    assert all(len(result.gate_decisions) == 3 for result in results)
    current_labels = tuple(
        GateLabel(sid, dates[-1], dates[-1] + timedelta(days=22), sid == 3) for sid in range(1, 4)
    )
    assert recall(results[-1].gate_decisions, current_labels) == 1.0
