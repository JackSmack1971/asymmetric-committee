"""Brand and product aliases (``config/aliases.yaml``): the repository-config half of the alias
source. Identity aliases (name, ticker, CIK) live in the ``securities`` table instead (§12.1)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from config.loader import CONFIG_DIR, ConfigError, _read_yaml
from contracts.data import AliasText


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BrandEntry(_Cfg):
    cik: int = Field(ge=1)
    aliases: tuple[AliasText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        folded = [a.casefold() for a in self.aliases]
        if len(set(folded)) != len(folded):
            raise ValueError(f"duplicate alias for cik {self.cik}")
        return self


class BrandAliasConfig(_Cfg):
    brands: tuple[BrandEntry, ...]

    @model_validator(mode="after")
    def _unique(self) -> Self:
        ciks = [b.cik for b in self.brands]
        if len(set(ciks)) != len(ciks):
            raise ValueError("duplicate cik in brands")
        return self

    def by_cik(self) -> Mapping[int, tuple[str, ...]]:
        return {b.cik: b.aliases for b in self.brands}


def load_brand_aliases(path: Path = CONFIG_DIR / "aliases.yaml") -> BrandAliasConfig:
    try:
        return BrandAliasConfig.model_validate(_read_yaml(path))
    except ValidationError as e:
        raise ConfigError(f"invalid alias config {path}:\n{e}") from e
