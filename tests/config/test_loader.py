"""config/*.yaml validates at startup and fails loudly (§8.1, §10.1, §4.4)."""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from config.loader import (
    CONFIG_DIR,
    ConfigError,
    CostSource,
    ServedModel,
    UsageUnavailableError,
    load_config,
)
from contracts.enums import BearSeverity, Horizon, ModelTier
from contracts.models import MAX_POSITION


def test_repo_config_loads_with_placeholders() -> None:
    cfg = load_config(allow_placeholders=True, env={})
    assert cfg.pipeline.gate.k == 15
    assert cfg.risk.max_position == MAX_POSITION == 0.08
    assert cfg.risk.max_sector == 0.30
    assert cfg.risk.vol_target_annual == 0.12
    assert cfg.risk.min_position == 0.01
    assert cfg.risk.entry_threshold == 0.56
    assert cfg.risk.bear_multiplier[BearSeverity.HIGH] == 0.5
    assert cfg.risk.cio_veto_budget == 0.20
    assert cfg.universe.top_n == 40
    assert cfg.universe.market_cap_min_usd == 500e6
    assert cfg.universe.adv20_min_usd == 10e6
    assert cfg.universe.price_min_usd == 5
    assert set(cfg.pipeline.horizons) == set(Horizon)
    assert set(cfg.models.tiers) == set(ModelTier)


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    for f in CONFIG_DIR.glob("*.yaml"):
        shutil.copy(f, tmp_path / f.name)
    return tmp_path


def edit(cfg_dir: Path, name: str, path: list[str], value: object) -> None:
    file = cfg_dir / f"{name}.yaml"
    data = yaml.safe_load(file.read_text())
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    file.write_text(yaml.safe_dump(data))


def fill_placeholders(cfg_dir: Path) -> None:
    edit(cfg_dir, "universe", ["sectors"], ["Semiconductors"])
    for tier, slug in [("fast", "a/fast"), ("strong", "a/strong"), ("probe", "a/fast")]:
        edit(cfg_dir, "models", ["tiers", tier, "primary", "slug"], slug)
        edit(cfg_dir, "models", ["tiers", tier, "fallbacks"], [])


def test_placeholders_rejected_by_default() -> None:
    with pytest.raises(ConfigError, match="placeholders"):
        load_config(env={})


def test_filled_config_loads_strict(cfg_dir: Path) -> None:
    fill_placeholders(cfg_dir)
    edit(cfg_dir, "models", ["tiers", "strong", "primary", "stated_training_cutoff"], "2026-03-01")
    cfg = load_config(cfg_dir, env={})
    assert cfg.models.latest_stated_cutoff() == date(2026, 3, 1)


def test_response_model_lookup_handles_primary_and_fallback_independently(cfg_dir: Path) -> None:
    edit(
        cfg_dir,
        "models",
        ["tiers", "strong", "primary", "stated_training_cutoff"],
        "2023-01-01",
    )
    edit(
        cfg_dir,
        "models",
        [
            "tiers",
            "strong",
            "fallbacks",
        ],
        [
            {
                "slug": "provider/served-fallback",
                "stated_training_cutoff": "2024-06-30",
                "supports_structured_outputs": True,
                "rpm": 30,
                "tpm": 100_000,
                "input_price_usd_per_mtok": 0.5,
                "output_price_usd_per_mtok": 1.5,
            }
        ],
    )
    cfg = load_config(cfg_dir, allow_placeholders=True, env={})

    primary = cfg.models.model_for_response("TODO/strong-model")
    fallback = cfg.models.model_for_response("provider/served-fallback")
    assert primary.stated_training_cutoff == date(2023, 1, 1)
    assert fallback.stated_training_cutoff == date(2024, 6, 30)
    assert fallback.supports_structured_outputs is True


def test_unknown_response_model_is_rejected() -> None:
    cfg = load_config(allow_placeholders=True, env={})

    with pytest.raises(ValueError, match="unknown served model"):
        cfg.models.model_for_response("provider/unconfigured-model")


@pytest.mark.parametrize(
    "served", ["todo/strong-model", " TODO/strong-model", "TODO/strong-model:v2"]
)
def test_response_model_lookup_is_exact_match_only(served: str) -> None:
    cfg = load_config(allow_placeholders=True, env={})

    with pytest.raises(ValueError, match="unknown served model"):
        cfg.models.model_for_response(served)


def test_primary_and_fallback_lookup_return_own_metadata() -> None:
    cfg = load_config(allow_placeholders=True, env={})
    entry = cfg.models.tiers[ModelTier.FAST]

    assert cfg.models.model_for_response(entry.primary.slug) == entry.primary
    assert cfg.models.model_for_response(entry.fallbacks[0].slug) == entry.fallbacks[0]


def test_shared_slug_across_tiers_with_identical_metadata_is_allowed(cfg_dir: Path) -> None:
    # The probe tier reuses the fast primary (§10.1); lookup resolves to the same metadata.
    cfg = load_config(cfg_dir, allow_placeholders=True, env={})
    probe = cfg.models.tiers[ModelTier.PROBE].primary
    assert cfg.models.model_for_response(probe.slug) == cfg.models.tiers[ModelTier.FAST].primary


def test_shared_slug_across_tiers_with_conflicting_metadata_is_rejected(cfg_dir: Path) -> None:
    edit(cfg_dir, "models", ["tiers", "probe", "primary", "stated_training_cutoff"], "2001-01-01")

    with pytest.raises(ConfigError, match="conflicting metadata"):
        load_config(cfg_dir, allow_placeholders=True, env={})


def test_duplicate_slugs_within_tier_are_rejected(cfg_dir: Path) -> None:
    primary = yaml.safe_load((cfg_dir / "models.yaml").read_text())["tiers"]["fast"]["primary"]
    edit(cfg_dir, "models", ["tiers", "fast", "fallbacks"], [primary])

    with pytest.raises(ConfigError, match="duplicate model slug"):
        load_config(cfg_dir, allow_placeholders=True, env={})


def test_env_budget_override(cfg_dir: Path) -> None:
    cfg = load_config(cfg_dir, allow_placeholders=True, env={"RUN_BUDGET_USD": "3.5"})
    assert cfg.pipeline.budgets.run_budget_usd == 3.5


@pytest.mark.parametrize(
    ("name", "path", "value", "match"),
    [
        ("risk", ["max_position"], 0.10, "max_position"),
        ("risk", ["min_position"], 0.09, "min_position"),
        ("risk", ["entry_threshold"], 0.4, "entry_threshold"),
        ("risk", ["long_only"], False, "long_only"),
        ("risk", ["bear_multiplier"], {"low": 1.0, "high": 0.5}, "bear_multiplier"),
        ("risk", ["surprise"], 1, "extra"),
        (
            "models",
            ["tiers", "fast", "primary", "supports_structured_outputs"],
            False,
            "structured",
        ),
        ("models", ["tiers", "probe", "primary", "slug"], "TODO/other", "probe"),
        ("models", ["tiers", "fast", "primary", "stated_training_cutoff"], "soon", "date"),
        ("universe", ["market_cap_min_usd"], 6e10, "market_cap"),
        ("universe", ["top_n"], 0, "top_n"),
        ("pipeline", ["gate", "k"], 0, "k"),
        ("pipeline", ["horizons"], [21, 42], "horizons"),
        ("pipeline", ["rebalance", "weekday"], "sunday", "weekday"),
        ("pipeline", ["llm", "max_retries"], 5, "max_retries"),
        ("pipeline", ["committee", "logit_clip"], [0.6, 0.9], "logit_clip"),
    ],
)
def test_invalid_config_fails_loudly(
    cfg_dir: Path, name: str, path: list[str], value: object, match: str
) -> None:
    edit(cfg_dir, name, path, value)
    with pytest.raises(ConfigError, match=match):
        load_config(cfg_dir, allow_placeholders=True, env={})


def test_missing_file_fails(cfg_dir: Path) -> None:
    (cfg_dir / "risk.yaml").unlink()
    with pytest.raises(ConfigError, match=r"risk\.yaml"):
        load_config(cfg_dir, allow_placeholders=True, env={})


def test_missing_tier_fails(cfg_dir: Path) -> None:
    data = yaml.safe_load((cfg_dir / "models.yaml").read_text())
    del data["tiers"]["probe"]
    (cfg_dir / "models.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="missing tiers"):
        load_config(cfg_dir, allow_placeholders=True, env={})


def test_freshness_slas_cover_every_ingested_feed() -> None:
    from contracts.enums import FeedName

    slas = load_config(allow_placeholders=True, env={}).pipeline.freshness_sla_hours
    ingested = {FeedName.PRICE_BARS, FeedName.FUNDAMENTALS, FeedName.INSIDER_TRADES, FeedName.NEWS}
    assert ingested <= set(slas)
    assert all(h > 0 for h in slas.values())


def test_load_universe_file() -> None:
    from config.loader import load_universe

    assert load_universe(CONFIG_DIR / "universe.yaml").top_n == 40


# --- per-slug limits, pricing and cost policy (§10.2) ------------------------------------------


def edit_shared_primary(cfg_dir: Path, field: str, value: object) -> None:
    """The probe primary reuses the fast primary slug, so both must change together."""
    for tier in ("fast", "probe"):
        edit(cfg_dir, "models", ["tiers", tier, "primary", field], value)


LIMIT_FIELDS = ["rpm", "tpm"]
PRICE_FIELDS = ["input_price_usd_per_mtok", "output_price_usd_per_mtok"]
ALL_META_FIELDS = LIMIT_FIELDS + PRICE_FIELDS


@pytest.mark.parametrize("field", LIMIT_FIELDS)
@pytest.mark.parametrize("bad", [0, -1, 1.5, "fast"])
def test_limits_must_be_positive_ints(cfg_dir: Path, field: str, bad: object) -> None:
    edit(cfg_dir, "models", ["tiers", "fast", "primary", field], bad)

    with pytest.raises(ConfigError, match=field):
        load_config(cfg_dir, allow_placeholders=True, env={})


@pytest.mark.parametrize("field", PRICE_FIELDS)
@pytest.mark.parametrize("bad", [-0.01, float("nan"), float("inf"), "free"])
def test_prices_must_be_finite_and_non_negative(cfg_dir: Path, field: str, bad: object) -> None:
    edit(cfg_dir, "models", ["tiers", "fast", "primary", field], bad)

    with pytest.raises(ConfigError, match=field):
        load_config(cfg_dir, allow_placeholders=True, env={})


@pytest.mark.parametrize("field", PRICE_FIELDS)
def test_zero_price_is_allowed(cfg_dir: Path, field: str) -> None:
    edit_shared_primary(cfg_dir, field, 0)

    cfg = load_config(cfg_dir, allow_placeholders=True, env={})
    assert getattr(cfg.models.tiers[ModelTier.FAST].primary, field) == 0


@pytest.mark.parametrize("field", ALL_META_FIELDS)
@pytest.mark.parametrize("where", ["primary", "fallback"])
def test_every_primary_and_fallback_needs_complete_metadata(
    cfg_dir: Path, field: str, where: str
) -> None:
    file = cfg_dir / "models.yaml"
    data = yaml.safe_load(file.read_text())
    entry = data["tiers"]["fast"]
    del (entry["primary"] if where == "primary" else entry["fallbacks"][0])[field]
    file.write_text(yaml.safe_dump(data))

    with pytest.raises(ConfigError, match=field):
        load_config(cfg_dir, allow_placeholders=True, env={})


def test_shared_slug_with_conflicting_pricing_is_rejected(cfg_dir: Path) -> None:
    edit(cfg_dir, "models", ["tiers", "probe", "primary", "rpm"], 5)

    with pytest.raises(ConfigError, match="conflicting metadata"):
        load_config(cfg_dir, allow_placeholders=True, env={})


def test_repo_models_carry_limits_and_prices() -> None:
    cfg = load_config(allow_placeholders=True, env={})
    for entry in cfg.models.tiers.values():
        for model in entry.models:
            assert model.rpm > 0 and model.tpm > 0
            assert model.input_price_usd_per_mtok >= 0 and model.output_price_usd_per_mtok >= 0


def _fast_primary(cfg_dir: Path) -> ServedModel:
    edit_shared_primary(cfg_dir, "input_price_usd_per_mtok", 2.0)
    edit_shared_primary(cfg_dir, "output_price_usd_per_mtok", 10.0)
    return (
        load_config(cfg_dir, allow_placeholders=True, env={}).models.tiers[ModelTier.FAST].primary
    )


def test_local_cost_is_tokens_times_configured_prices(cfg_dir: Path) -> None:
    model = _fast_primary(cfg_dir)

    cost = model.call_cost({"prompt_tokens": 1_000, "completion_tokens": 200})

    assert cost.source is CostSource.LOCAL
    assert cost.usd == pytest.approx(0.002 + 0.002)


def test_local_cost_is_authoritative_over_provider_cost(cfg_dir: Path) -> None:
    model = _fast_primary(cfg_dir)

    cost = model.call_cost({"prompt_tokens": 1_000_000, "completion_tokens": 0, "cost": 99.0})

    assert cost.source is CostSource.LOCAL
    assert cost.usd == pytest.approx(2.0)


def test_zero_tokens_cost_zero_locally(cfg_dir: Path) -> None:
    model = _fast_primary(cfg_dir)

    assert model.call_cost({"prompt_tokens": 0, "completion_tokens": 0}).usd == 0.0


@pytest.mark.parametrize(
    "usage", [{"completion_tokens": 10, "cost": 0.25}, {"prompt_tokens": None, "cost": 0.25}]
)
def test_missing_token_counts_fall_back_to_provider_cost(
    cfg_dir: Path, usage: dict[str, Any]
) -> None:
    model = _fast_primary(cfg_dir)

    cost = model.call_cost(usage)

    assert cost.source is CostSource.PROVIDER_FALLBACK
    assert cost.usd == 0.25


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"prompt_tokens": 5},
        {"prompt_tokens": -1, "completion_tokens": 1},
        {"prompt_tokens": True, "completion_tokens": 1},
        {"completion_tokens": 10, "cost": -1},
        {"completion_tokens": 10, "cost": float("nan")},
        {"completion_tokens": 10, "cost": "0.25"},
    ],
)
def test_unusable_usage_fails_closed(cfg_dir: Path, usage: dict[str, Any] | None) -> None:
    model = _fast_primary(cfg_dir)

    with pytest.raises(UsageUnavailableError):
        model.call_cost(usage)


def test_run_budget_is_checked_against_summed_local_costs(cfg_dir: Path) -> None:
    edit(cfg_dir, "pipeline", ["budgets", "run_budget_usd"], 0.01)
    model = _fast_primary(cfg_dir)
    budget = load_config(cfg_dir, allow_placeholders=True, env={}).pipeline.budgets.run_budget_usd
    mocked_usage = [{"prompt_tokens": 2_000, "completion_tokens": 100}] * 3  # 0.005 each

    spent = 0.0
    exceeded_after = None
    for i, usage in enumerate(mocked_usage, start=1):
        spent += model.call_cost(usage).usd
        if spent > budget and exceeded_after is None:
            exceeded_after = i

    assert spent == pytest.approx(0.015)
    assert exceeded_after == 3
