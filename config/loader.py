"""Load and validate config/*.yaml at startup. Any problem raises ConfigError (fail loudly)."""

from __future__ import annotations

import os
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from contracts.enums import BearSeverity, FeedName, Horizon, ModelTier
from contracts.models import MAX_POSITION

CONFIG_DIR = Path(__file__).parent
PLACEHOLDER_PREFIX = "TODO"

Fraction = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
PosFloat = Annotated[float, Field(gt=0.0, allow_inf_nan=False)]


class ConfigError(RuntimeError):
    pass


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- models.yaml -----------------------------------------------------------------------------


class ServedModel(_Cfg):
    slug: str = Field(min_length=1)
    stated_training_cutoff: date
    supports_structured_outputs: bool


class ModelEntry(_Cfg):
    primary: ServedModel
    fallbacks: tuple[ServedModel, ...]

    @property
    def models(self) -> tuple[ServedModel, ...]:
        return (self.primary, *self.fallbacks)

    @property
    def all_slugs(self) -> tuple[str, ...]:
        return tuple(model.slug for model in self.models)

    @model_validator(mode="after")
    def _unique_slugs(self) -> Self:
        if len(set(self.all_slugs)) != len(self.all_slugs):
            raise ValueError("duplicate model slug within tier")
        return self


class ModelsConfig(_Cfg):
    tiers: dict[ModelTier, ModelEntry]

    @model_validator(mode="after")
    def _check(self) -> Self:
        missing = set(ModelTier) - set(self.tiers)
        if missing:
            raise ValueError(f"missing tiers: {sorted(missing)}")
        by_slug: dict[str, ServedModel] = {}
        for tier, entry in self.tiers.items():
            for model in entry.models:
                if not model.supports_structured_outputs:
                    raise ValueError(
                        f"model {model.slug} in tier {tier.value} must support "
                        "structured outputs (§10.1)"
                    )
                if model.slug in by_slug and by_slug[model.slug] != model:
                    raise ValueError(
                        f"duplicate model slug {model.slug!r} has conflicting metadata"
                    )
                by_slug[model.slug] = model
        production = {
            s for t in (ModelTier.FAST, ModelTier.STRONG) for s in self.tiers[t].all_slugs
        }
        if not set(self.tiers[ModelTier.PROBE].all_slugs) <= production:
            raise ValueError("probe tier must use production models (§10.1)")
        return self

    def latest_stated_cutoff(self) -> date:
        return max(
            model.stated_training_cutoff for entry in self.tiers.values() for model in entry.models
        )

    def model_for_response(self, response_model: str) -> ServedModel:
        """Return metadata for an exact OpenRouter ``response.model`` value."""
        for entry in self.tiers.values():
            for model in entry.models:
                if model.slug == response_model:
                    return model
        raise ValueError(f"unknown served model: {response_model!r}")


# --- risk.yaml -------------------------------------------------------------------------------


class RiskConfig(_Cfg):
    long_only: bool
    max_position: Fraction
    max_sector: Fraction
    vol_target_annual: PosFloat
    min_position: Fraction
    entry_threshold: Fraction
    k: PosFloat
    dispersion_lambda: Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
    bear_multiplier: dict[BearSeverity, Fraction]
    cio_veto_budget: Fraction
    kill_switch_daily_loss: Fraction
    modeled_cost_bps: Annotated[float, Field(ge=0.0, allow_inf_nan=False)]

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.long_only:
            raise ValueError("long_only must be true in phase 1 (§18.4)")
        if not 0 < self.min_position < self.max_position <= MAX_POSITION:
            raise ValueError(f"need 0 < min_position < max_position <= {MAX_POSITION}")
        if self.max_sector < self.max_position:
            raise ValueError("max_sector must be >= max_position")
        if not 0.5 < self.entry_threshold < 1:
            raise ValueError("entry_threshold must be in (0.5, 1)")
        if set(self.bear_multiplier) != set(BearSeverity):
            raise ValueError("bear_multiplier needs every BearSeverity")
        return self


# --- universe.yaml ---------------------------------------------------------------------------


class UniverseConfig(_Cfg):
    sectors: tuple[str, ...]
    market_cap_min_usd: PosFloat
    market_cap_max_usd: PosFloat
    adv20_min_usd: PosFloat
    price_min_usd: PosFloat
    top_n: int = Field(ge=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.market_cap_min_usd >= self.market_cap_max_usd:
            raise ValueError("market_cap_min_usd must be < market_cap_max_usd")
        if len(set(self.sectors)) != len(self.sectors):
            raise ValueError("duplicate sector")
        return self


# --- pipeline.yaml ---------------------------------------------------------------------------


class Weekday(StrEnum):
    MONDAY = "monday"
    TUESDAY = "tuesday"
    WEDNESDAY = "wednesday"
    THURSDAY = "thursday"
    FRIDAY = "friday"


class GateConfig(_Cfg):
    k: int = Field(ge=1)


class RebalanceConfig(_Cfg):
    weekday: Weekday
    minutes_after_open: int = Field(ge=0, le=390)
    timezone: str = Field(min_length=1)


class OrdersConfig(_Cfg):
    limit_offset_bps: Annotated[float, Field(ge=0.0)]
    time_in_force_minutes: int = Field(ge=1, le=390)


class CommitteeConfig(_Cfg):
    logit_clip: tuple[Fraction, Fraction]
    cold_start_min_verdicts: int = Field(ge=0)
    weight_floor: Annotated[float, Field(ge=0.0, allow_inf_nan=False)]

    @model_validator(mode="after")
    def _check(self) -> Self:
        lo, hi = self.logit_clip
        if not 0 < lo < 0.5 < hi < 1:
            raise ValueError("logit_clip must satisfy 0 < lo < 0.5 < hi < 1")
        return self


class LlmConfig(_Cfg):
    temperature: Annotated[float, Field(ge=0.0, le=2.0)]
    max_retries: int = Field(ge=0, le=3)  # §10.2: capped at 3
    repair_attempts: int = Field(ge=0, le=1)  # §10.2: one repair attempt


class BudgetsConfig(_Cfg):
    run_budget_usd: PosFloat


class PipelineConfig(_Cfg):
    gate: GateConfig
    rebalance: RebalanceConfig
    orders: OrdersConfig
    committee: CommitteeConfig
    horizons: tuple[Horizon, ...] = Field(min_length=1)
    llm: LlmConfig
    budgets: BudgetsConfig
    freshness_sla_hours: dict[FeedName, PosFloat] = Field(min_length=1)


# --- top level -------------------------------------------------------------------------------


class AppConfig(_Cfg):
    models: ModelsConfig
    risk: RiskConfig
    universe: UniverseConfig
    pipeline: PipelineConfig


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"{path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at top level")
    return data


def _placeholders(cfg: AppConfig) -> list[str]:
    found = [
        f"models.{tier.value}: {slug}"
        for tier, entry in cfg.models.tiers.items()
        for slug in entry.all_slugs
        if slug.startswith(PLACEHOLDER_PREFIX)
    ]
    if not cfg.universe.sectors:
        found.append("universe.sectors is empty")
    return found


def load_config(
    config_dir: Path = CONFIG_DIR,
    *,
    allow_placeholders: bool = False,
    env: dict[str, str] | None = None,
) -> AppConfig:
    """Validate every config file. Placeholders (TODO slugs, empty sectors) are errors unless
    ``allow_placeholders`` is set, which only tests and the P0 gate should do."""
    env = dict(os.environ) if env is None else env
    raw = {name: _read_yaml(config_dir / f"{name}.yaml") for name in AppConfig.model_fields}
    if budget := env.get("RUN_BUDGET_USD"):
        raw["pipeline"].setdefault("budgets", {})["run_budget_usd"] = budget
    try:
        cfg = AppConfig.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"invalid config in {config_dir}:\n{e}") from e
    if not allow_placeholders and (found := _placeholders(cfg)):
        raise ConfigError("config still has placeholders:\n  " + "\n  ".join(found))
    return cfg


def load_universe(path: Path) -> UniverseConfig:
    """Validate one universe file (the backfill CLI takes ``--universe PATH``)."""
    try:
        return UniverseConfig.model_validate(_read_yaml(path))
    except ValidationError as e:
        raise ConfigError(f"invalid universe config {path}:\n{e}") from e
