from __future__ import annotations

from pathlib import Path

import pytest

from config.aliases import load_brand_aliases
from config.loader import ConfigError


def _write(tmp: Path, body: str) -> Path:
    path = tmp / "aliases.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_shipped_file_is_valid() -> None:
    assert load_brand_aliases().by_cik() == {}


def test_by_cik(tmp_path: Path) -> None:
    body = "brands:\n  - {cik: 5, aliases: [Widget, 'Acme Cloud']}\n"
    assert load_brand_aliases(_write(tmp_path, body)).by_cik() == {5: ("Widget", "Acme Cloud")}


@pytest.mark.parametrize(
    "body",
    [
        "brands:\n  - {cik: 5, aliases: []}\n",
        "brands:\n  - {cik: 0, aliases: [X]}\n",
        "brands:\n  - {cik: 5, aliases: [X, x]}\n",
        "brands:\n  - {cik: 5, aliases: [X]}\n  - {cik: 5, aliases: [Y]}\n",
        "brands:\n  - {cik: 5, aliases: [X], extra: 1}\n",
        "brands: []\nextra: 1\n",
        "aliases: []\n",
    ],
)
def test_invalid_rejected(tmp_path: Path, body: str) -> None:
    with pytest.raises(ConfigError):
        load_brand_aliases(_write(tmp_path, body))
