"""Startup gates: owner-supplied configuration is reported, never silently bypassed."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, text

from config.aliases import BrandAliasConfig, BrandEntry
from config.loader import ConfigError
from orchestration import startup
from orchestration.startup import (
    StartupConfigError,
    assert_startup_ready,
    check_alias_coverage,
    check_environment,
)
from tests.agents.world import AS_OF, CIK
from tests.orchestration.store_support import seed
from tests.orchestration.test_pipeline import BASE_CFG

GOOD: dict[str, str] = {
    "DATABASE_URL": "postgresql+psycopg://u:p@db/x",
    "REDIS_URL": "redis://redis/0",
    "OPENROUTER_API_KEY": "k",
    "NEWS_PROVIDER": "alpaca",
    "ALPACA_API_KEY_ID": "id",
    "ALPACA_API_SECRET": "secret",
    "ANCHOR_GIT_REMOTE": "https://git.example/me/anchors.git",
    "ANCHOR_GIT_BRANCH": "anchors",
    "ANCHOR_GIT_DIR": "/var/lib/committee/anchor",
    "ANCHOR_OTS_CALENDARS": "https://a.example,https://b.example",
    "ANCHOR_OTS_MIN_CALENDARS": "2",
}


def env(**changes: str | None) -> dict[str, str]:
    out = {**GOOD}
    for k, v in changes.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = v
    return out


def test_a_complete_environment_has_no_problems() -> None:
    assert check_environment(GOOD, live=True, check_network=False) == []


def test_every_missing_item_is_reported_at_once() -> None:
    problems = check_environment({}, live=True, check_network=False)
    joined = "\n".join(problems)
    for name in (
        "DATABASE_URL",
        "REDIS_URL",
        "OPENROUTER_API_KEY",
        "NEWS_PROVIDER",
        "ALPACA_API_KEY_ID",
        "ALPACA_API_SECRET",
        "ANCHOR_GIT_REMOTE",
        "ANCHOR_GIT_BRANCH",
        "ANCHOR_GIT_DIR",
        "ANCHOR_OTS_CALENDARS",
    ):
        assert name in joined, name


def test_alpaca_keys_are_only_required_for_live() -> None:
    e = env(ALPACA_API_KEY_ID=None, ALPACA_API_SECRET=None)
    assert check_environment(e, live=False, check_network=False) == []
    assert len(check_environment(e, live=True, check_network=False)) == 2


@pytest.mark.parametrize("var", ["ALPACA_PAPER_BASE_URL", "APCA_API_BASE_URL", "ALPACA_BASE_URL"])
def test_a_live_trading_url_anywhere_in_the_environment_refuses_to_start(var: str) -> None:
    problems = check_environment(
        env(**{var: "https://api.alpaca.markets"}), live=True, check_network=False
    )
    assert any("live trading endpoint refused" in p for p in problems)


def test_the_news_provider_must_be_a_known_choice() -> None:
    problems = check_environment(env(NEWS_PROVIDER="rumours"), live=False, check_network=False)
    assert any("NEWS_PROVIDER must be one of" in p for p in problems)


def test_anchor_remote_must_be_public_and_not_a_local_path(monkeypatch: pytest.MonkeyPatch) -> None:
    local = env(ANCHOR_GIT_REMOTE="/srv/git/anchors.git")
    assert any(
        "not a local path" in p for p in check_environment(local, live=False, check_network=False)
    )
    ok_local = env(ANCHOR_GIT_REMOTE="/srv/git/anchors.git", ANCHOR_ALLOW_LOCAL_REMOTE="1")
    assert check_environment(ok_local, live=False, check_network=False) == []

    monkeypatch.setattr(startup, "remote_is_publicly_readable", lambda remote: False)
    private = check_environment(GOOD, live=False, check_network=True)
    assert any("not readable anonymously" in p for p in private)
    monkeypatch.setattr(startup, "remote_is_publicly_readable", lambda remote: True)
    assert check_environment(GOOD, live=False, check_network=True) == []


def test_calendars_must_be_https_and_the_minimum_must_fit() -> None:
    http = env(ANCHOR_OTS_CALENDARS="http://a.example")
    assert any(
        "must be https" in p for p in check_environment(http, live=False, check_network=False)
    )
    for bad in ("0", "3", "x"):
        problems = check_environment(
            env(ANCHOR_OTS_MIN_CALENDARS=bad), live=False, check_network=False
        )
        assert any("ANCHOR_OTS_MIN_CALENDARS" in p for p in problems), bad


def test_the_repos_own_config_still_blocks_production_until_the_owner_fills_it_in() -> None:
    """Placeholder slugs, an empty universe, an unconfirmed sector map: real and unbypassed."""
    with pytest.raises(StartupConfigError) as caught:
        assert_startup_ready(GOOD, live=True, check_network=False)
    text_ = str(caught.value)
    assert "config still has placeholders" in text_ and "TODO/" in text_


def test_environment_and_config_problems_are_reported_together() -> None:
    with pytest.raises(StartupConfigError) as caught:
        assert_startup_ready({}, live=True, check_network=False)
    assert any("DATABASE_URL" in p for p in caught.value.problems)
    assert any("placeholders" in p for p in caught.value.problems)


def test_a_ready_environment_returns_the_validated_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(startup, "load_config", lambda *a, **k: BASE_CFG)
    assert assert_startup_ready(GOOD, live=True, check_network=False) is BASE_CFG


def test_a_config_error_alone_is_still_a_startup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise ConfigError("sectors.yaml crosswalk is not owner-confirmed")

    monkeypatch.setattr(startup, "load_config", boom)
    with pytest.raises(StartupConfigError, match="owner-confirmed"):
        assert_startup_ready(GOOD, live=True, check_network=False)


# --- alias coverage against the database ---------------------------------------------------


@pytest.fixture
def engine(pg_engine: Engine) -> Iterator[Engine]:
    def wipe() -> None:
        with pg_engine.begin() as c:
            c.execute(text("TRUNCATE runs, securities RESTART IDENTITY CASCADE"))
            c.execute(
                text(
                    "TRUNCATE universe_snapshots, features, fundamentals_asfiled, insider_txns, "
                    "news_items, price_bars RESTART IDENTITY CASCADE"
                )
            )

    wipe()
    yield pg_engine
    wipe()


def _brands(entries: dict[int, list[str]]) -> BrandAliasConfig:
    return BrandAliasConfig(
        brands=tuple(BrandEntry(cik=c, aliases=tuple(a)) for c, a in entries.items())
    )


def test_a_universe_name_without_its_short_name_alias_stops_startup(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(engine)
    monkeypatch.setattr(startup, "load_brand_aliases", lambda: _brands({}))
    problems = check_alias_coverage(engine, AS_OF)
    assert len(problems) == 1 and "Zephyr" in problems[0] and "Borealis" in problems[0]


def test_covered_names_pass(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(engine)
    covered = {CIK: ["Zephyr"], 2345678: ["Borealis"], 3456789: ["Calder"], 4567890: ["Dunmore"]}
    monkeypatch.setattr(startup, "load_brand_aliases", lambda: _brands(covered))
    assert check_alias_coverage(engine, AS_OF) == []


def test_no_universe_snapshot_means_not_ready(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(startup, "load_brand_aliases", lambda: _brands({}))
    assert check_alias_coverage(engine, datetime(2025, 3, 7, tzinfo=UTC)) == [
        "no universe snapshot is knowable yet: run the backfill and snapshot first"
    ]
