"""Startup gates: production refuses to start until the owner-supplied configuration is real.

Nothing here fills in a default for something the owner must decide. Every problem is collected and
reported at once (``StartupConfigError``) so an operator fixes the list, not one item per attempt:

- ``config/`` has no placeholders: real model slugs, prices and rate limits, a non-empty universe
  and an owner-confirmed sector crosswalk (``load_config``);
- required secrets and endpoints are set: database, Redis, OpenRouter, the news provider choice
  and, for live, the Alpaca paper keys (paper-only is enforced by ``paper_base_url`` and fails
  closed on any other URL);
- alias coverage: every name in the latest universe snapshot has the brand aliases invariant 4
  needs (``config/aliases.yaml``), checked against the database;
- anchoring: a git remote, branch and clone directory, OpenTimestamps calendars over https, and (for
  a real deployment) a remote that anonymous ``git ls-remote`` can read, i.e. actually public.
  ``ANCHOR_ALLOW_LOCAL_REMOTE=1`` (tests only) accepts a local repository instead.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine

from config.aliases import load_brand_aliases
from config.loader import CONFIG_DIR, AppConfig, ConfigError, load_config
from execution.alpaca import LiveTradingError, paper_base_url
from orchestration.anchor_adapters import remote_is_publicly_readable
from store import as_of as point_in_time
from universe.snapshot import alias_coverage_gaps

NEWS_PROVIDERS = ("alpaca", "alphavantage")
_PUBLIC_SCHEMES = ("https://", "ssh://", "git@")


class StartupConfigError(RuntimeError):
    """Owner-supplied configuration is missing or unsafe; ``problems`` lists every item."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("startup configuration is not ready:\n  - " + "\n  - ".join(problems))


def calendars(env: Mapping[str, str]) -> list[str]:
    return [u.strip() for u in env.get("ANCHOR_OTS_CALENDARS", "").split(",") if u.strip()]


def check_environment(
    env: Mapping[str, str], *, live: bool, check_network: bool = True
) -> list[str]:
    problems: list[str] = []

    def need(name: str, why: str) -> str:
        value = env.get(name, "").strip()
        if not value:
            problems.append(f"{name} is not set ({why})")
        return value

    need("DATABASE_URL", "Postgres, the business truth")
    need("REDIS_URL", "Celery transport, LLM cache and rate limiter")
    need("OPENROUTER_API_KEY", "LLM calls")
    provider = need("NEWS_PROVIDER", "owner decision, blueprint §18.1")
    if provider and provider not in NEWS_PROVIDERS:
        problems.append(f"NEWS_PROVIDER must be one of {NEWS_PROVIDERS}, not {provider!r}")

    if live:
        need("ALPACA_API_KEY_ID", "paper trading")
        need("ALPACA_API_SECRET", "paper trading")
    try:
        paper_base_url(dict(env))  # any non-paper Alpaca URL in the environment fails closed
    except LiveTradingError as exc:
        problems.append(f"live trading endpoint refused: {exc}")

    allow_local = env.get("ANCHOR_ALLOW_LOCAL_REMOTE") == "1"
    remote = need("ANCHOR_GIT_REMOTE", "public git remote for commitment anchors")
    need("ANCHOR_GIT_BRANCH", "branch that receives anchor manifests")
    need("ANCHOR_GIT_DIR", "dedicated local clone directory for anchoring")
    urls = calendars(env)
    if not urls:
        problems.append("ANCHOR_OTS_CALENDARS is not set (comma-separated https calendar URLs)")
    problems += [
        f"OpenTimestamps calendar must be https: {u}" for u in urls if not u.startswith("https://")
    ]
    minimum = env.get("ANCHOR_OTS_MIN_CALENDARS", "1")
    if not minimum.isdigit() or not 1 <= int(minimum) <= max(len(urls), 1):
        problems.append("ANCHOR_OTS_MIN_CALENDARS must be between 1 and the number of calendars")
    if remote and not allow_local:
        if not remote.startswith(_PUBLIC_SCHEMES):
            problems.append("ANCHOR_GIT_REMOTE must be an https/ssh remote, not a local path")
        elif check_network and not remote_is_publicly_readable(remote):
            problems.append("ANCHOR_GIT_REMOTE is not readable anonymously, so it is not public")
    return problems


def check_alias_coverage(engine: Engine, now: datetime) -> list[str]:
    """Latest snapshot's names, checked against ``config/aliases.yaml`` (invariant 4)."""
    brands = load_brand_aliases().by_cik()
    with engine.connect() as conn:
        members = point_in_time.universe(conn, now)
        if not members:
            return ["no universe snapshot is knowable yet: run the backfill and snapshot first"]
        listed = point_in_time.securities(
            conn, [m.security_id for m in members], listed_on=now.astimezone(UTC).date()
        )
    gaps = alias_coverage_gaps(listed, brands)
    if not gaps:
        return []
    names = ", ".join(f"security {sid}: {short}" for sid, short in sorted(gaps.items()))
    return [f"config/aliases.yaml lacks short names for {names}"]


def assert_startup_ready(
    env: Mapping[str, str] | None = None,
    *,
    live: bool,
    engine: Engine | None = None,
    check_network: bool = True,
    config_dir: Path = CONFIG_DIR,
    now: datetime | None = None,
) -> AppConfig:
    """Return the validated config or raise ``StartupConfigError`` listing everything wrong."""
    env = dict(os.environ) if env is None else env
    problems = check_environment(env, live=live, check_network=check_network)
    cfg: AppConfig | None = None
    try:
        cfg = load_config(config_dir, env=dict(env))
    except ConfigError as exc:
        problems.append(str(exc))
    if engine is not None:
        problems += check_alias_coverage(engine, now or datetime.now(UTC))
    if problems or cfg is None:
        raise StartupConfigError(problems)
    return cfg
