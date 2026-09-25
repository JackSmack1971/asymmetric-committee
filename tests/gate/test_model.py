from datetime import UTC, datetime, timedelta

from contracts.data import FeatureRow
from gate.model import GateLabel, eligible_training_set


def test_training_labels_are_fully_realized_before_as_of() -> None:
    as_of = datetime(2025, 1, 31, tzinfo=UTC)
    starts = [as_of - timedelta(days=60), as_of - timedelta(days=30)]
    features = tuple(
        FeatureRow(
            security_id=i + 1,
            event_time=start,
            available_at=start,
            source_version="fs_v1",
            feature_set_version="fs_v1",
            values={"x": i},
        )
        for i, start in enumerate(starts)
    )
    labels = (
        GateLabel(1, starts[0], as_of - timedelta(seconds=1), True),
        GateLabel(2, starts[1], as_of, True),
    )
    selected = eligible_training_set(features, labels, as_of)
    assert len(selected) == 1
    assert all(example.label_end < as_of for example in selected)
