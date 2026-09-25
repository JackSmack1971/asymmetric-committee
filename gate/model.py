"""Walk-forward logistic quality gate with explicit label availability."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from contracts.data import FeatureRow, PriceBar
from contracts.models import GateDecision

GATE_MODEL_VERSION: Final = "logistic_v1"


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


@dataclass(frozen=True)
class GateLabel:
    security_id: int
    feature_time: datetime
    label_end: datetime
    is_top_tercile: bool


@dataclass(frozen=True)
class TrainingExample:
    security_id: int
    feature_time: datetime
    label_end: datetime
    values: dict[str, object]
    target: int


def build_labels(
    feature_times: tuple[datetime, ...],
    prices: dict[int, tuple[PriceBar, ...]],
    sectors: dict[int, str],
    *,
    horizon: int = 21,
) -> tuple[GateLabel, ...]:
    """Build top-tercile absolute sector-relative forward-return labels.

    The returned ``label_end`` is the actual horizon bar timestamp and is subsequently subject to
    the strict ``label_end < as_of`` eligibility rule.
    """
    output: list[GateLabel] = []
    for feature_time in feature_times:
        returns: dict[int, tuple[float, datetime]] = {}
        for sid, raw_bars in prices.items():
            bars = sorted(raw_bars, key=lambda bar: bar.event_time)
            starts = [i for i, bar in enumerate(bars) if bar.event_time <= feature_time]
            if not starts:
                continue
            start = starts[-1]
            end = start + horizon
            if end < len(bars) and bars[start].close > 0:
                returns[sid] = (bars[end].close / bars[start].close - 1, bars[end].event_time)
        relative: dict[int, tuple[float, datetime]] = {}
        for sid, (value, label_end) in returns.items():
            peers = [r for peer, (r, _) in returns.items() if sectors[peer] == sectors[sid]]
            relative[sid] = (abs(value - statistics.fmean(peers)), label_end)
        ranked = sorted(relative, key=lambda sid: (-relative[sid][0], sid))
        top_count = math.ceil(len(ranked) / 3)
        top = set(ranked[:top_count])
        output.extend(
            GateLabel(sid, feature_time, relative[sid][1], sid in top) for sid in sorted(relative)
        )
    return tuple(output)


def eligible_training_set(
    features: tuple[FeatureRow, ...], labels: tuple[GateLabel, ...], as_of: datetime
) -> tuple[TrainingExample, ...]:
    """Join features to labels and enforce the strict §6 no-leak boundary."""
    by_key = {(row.security_id, row.event_time): row for row in features}
    return tuple(
        TrainingExample(
            label.security_id,
            label.feature_time,
            label.label_end,
            dict(by_key[(label.security_id, label.feature_time)].values),
            int(label.is_top_tercile),
        )
        for label in labels
        if label.label_end < as_of and (label.security_id, label.feature_time) in by_key
    )


def _sigmoid(value: float) -> float:
    value = max(-35.0, min(35.0, value))
    return 1.0 / (1.0 + math.exp(-value))


@dataclass(frozen=True)
class LogisticGate:
    names: tuple[str, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]
    coefficients: tuple[float, ...]
    intercept: float

    def score(self, values: dict[str, object]) -> float:
        total = self.intercept
        for name, mean, scale, coefficient in zip(
            self.names, self.means, self.scales, self.coefficients, strict=True
        ):
            raw = _number(values.get(name))
            value = raw if raw is not None else mean
            total += coefficient * (value - mean) / scale
        return _sigmoid(total)


def fit_logistic(
    examples: tuple[TrainingExample, ...],
    *,
    iterations: int = 300,
    learning_rate: float = 0.08,
    l2: float = 0.01,
) -> LogisticGate:
    names = tuple(
        sorted(
            {
                name
                for example in examples
                for name, value in example.values.items()
                if isinstance(value, (int, float)) and math.isfinite(value)
            }
        )
    )
    columns: list[list[float]] = []
    means: list[float] = []
    scales: list[float] = []
    for name in names:
        observed = [value for e in examples if (value := _number(e.values.get(name))) is not None]
        mean = statistics.fmean(observed) if observed else 0.0
        scale = statistics.pstdev(observed) if len(observed) > 1 else 1.0
        means.append(mean)
        scales.append(scale or 1.0)
        columns.append(
            [
                value if (value := _number(e.values.get(name))) is not None else mean
                for e in examples
            ]
        )
    if not examples:
        return LogisticGate(names, tuple(means), tuple(scales), tuple(0.0 for _ in names), 0.0)
    weights = [0.0] * len(names)
    rate = min(max(sum(e.target for e in examples) / len(examples), 1e-6), 1 - 1e-6)
    intercept = math.log(rate / (1 - rate))
    for _ in range(iterations):
        errors = []
        for row_index, example in enumerate(examples):
            linear = intercept + sum(
                weights[j] * (columns[j][row_index] - means[j]) / scales[j]
                for j in range(len(names))
            )
            errors.append(_sigmoid(linear) - example.target)
        intercept -= learning_rate * statistics.fmean(errors)
        for j in range(len(names)):
            gradient = (
                statistics.fmean(
                    errors[i] * (columns[j][i] - means[j]) / scales[j] for i in range(len(examples))
                )
                + l2 * weights[j]
            )
            weights[j] -= learning_rate * gradient
    return LogisticGate(names, tuple(means), tuple(scales), tuple(weights), intercept)


def decide(
    *, model: LogisticGate, rows: tuple[FeatureRow, ...], run_id: UUID, as_of: datetime, top_k: int
) -> tuple[GateDecision, ...]:
    scores = {row.security_id: model.score(row.values) for row in rows}
    passed = set(sorted(scores, key=lambda sid: (-scores[sid], sid))[:top_k])
    return tuple(
        GateDecision(
            run_id=run_id,
            security_id=row.security_id,
            as_of=as_of,
            feature_set_version=row.feature_set_version,
            gate_model_version=GATE_MODEL_VERSION,
            score=scores[row.security_id],
            passed=row.security_id in passed,
            components=tuple(
                (name, coefficient)
                for name, coefficient in zip(model.names, model.coefficients, strict=True)
            ),
        )
        for row in rows
    )


def recall(decisions: tuple[GateDecision, ...], labels: tuple[GateLabel, ...]) -> float | None:
    positives = {x.security_id for x in labels if x.is_top_tercile}
    if not positives:
        return None
    passed = {x.security_id for x in decisions if x.passed}
    return len(positives & passed) / len(positives)
