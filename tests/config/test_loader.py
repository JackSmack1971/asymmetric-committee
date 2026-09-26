"""config/*.yaml validates at startup and fails loudly (§8.1, §10.1, §4.4)."""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest
import yaml

from config.loader import CONFIG_DIR, ConfigError, load_config
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
    edit(cfg_dir, "sectors", ["confirmed"], True)
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


def test_sectors_crosswalk_lookup() -> None:
    sectors = load_config(allow_placeholders=True, env={}).sectors
    assert {e.etf for e in sectors.sectors} >= {"XLK", "XLE", "XLF"}
    hit = sectors.sector_for_sic(6021)
    assert hit is not None and hit.etf == "XLF"
    assert sectors.sector_for_sic(9999) is None


def test_unconfirmed_sectors_rejected_in_production(cfg_dir: Path) -> None:
    with pytest.raises(ConfigError, match="not owner-confirmed"):
        load_config(cfg_dir, env={})


def test_overlapping_sic_ranges_rejected(cfg_dir: Path) -> None:
    path = cfg_dir / "sectors.yaml"
    data = yaml.safe_load(path.read_text())
    data["sectors"][0]["sic_ranges"].append([6000, 6010])
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="overlapping"):
        load_config(cfg_dir, allow_placeholders=True, env={})
