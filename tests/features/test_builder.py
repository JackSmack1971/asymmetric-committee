from datetime import UTC, datetime

from features import build_feature_rows


def test_same_as_of_produces_identical_features() -> None:
    as_of = datetime(2025, 1, 3, tzinfo=UTC)
    first = build_feature_rows(
        as_of=as_of,
        security_ids=[2, 1],
        sectors={1: "A", 2: "A"},
        market_caps={1: 100.0, 2: 200.0},
        prices={},
        fundamentals={},
        insiders=(),
        news=(),
    )
    second = build_feature_rows(
        as_of=as_of,
        security_ids=[2, 1],
        sectors={1: "A", 2: "A"},
        market_caps={1: 100.0, 2: 200.0},
        prices={},
        fundamentals={},
        insiders=(),
        news=(),
    )
    assert first == second
