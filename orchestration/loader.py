"""The production ``StepLoader``: builds one step's inputs through the public ``store.as_of`` API.

Every fact read goes through ``store.as_of`` (invariant 2), which returns only rows with
``available_at <= as_of`` (invariant 3). This module holds no SQL and no table references. The
orchestrator re-checks the result (``assert_point_in_time``) as a second line of defence.

Known limits, deliberately not papered over:

- ``base_rates`` is a neutral prior (0.5) and no resolved observations are supplied, so the stacker
  stays in pass-through/rank mode. Outcomes are P6; until then rank sizing is the only honest mode.
- No market-wide ``regime`` features are supplied (no point-in-time source exists yet), so the
  macro agent renders without a regime table.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine

from agents.partitioner import AliasMasker
from agents.partitions import EntityData, Partitioner, assign_entity_tokens
from contracts.data import FeatureRow, InsiderTxn, NewsItem, PriceBar
from contracts.enums import Horizon
from features.builder import FEATURE_SET_VERSION
from orchestration.pipeline import StepInputs
from store import as_of as point_in_time
from store.aliases import add_people

log = logging.getLogger(__name__)

ADV_SESSIONS = 30
PRICE_LOOKBACK = timedelta(days=75)  # covers 30 sessions with room for holidays
INSIDER_LOOKBACK = timedelta(days=3 * 365 + 30)  # routine/opportunistic tagging needs 3 years
NEWS_LOOKBACK = timedelta(days=56)


class EmptyUniverseError(RuntimeError):
    """No usable universe at ``as_of``: a run must not commit an empty book from missing data."""


class StoreStepLoader:
    def __init__(
        self,
        engine: Engine,
        *,
        brands: Mapping[int, Sequence[str]],
        base_rates: Mapping[Horizon, float] | None = None,
        feature_set_version: str = FEATURE_SET_VERSION,
    ) -> None:
        self._engine = engine
        self._brands = brands
        self._base_rates = base_rates or dict.fromkeys(Horizon, 0.5)
        self._version = feature_set_version

    def load(self, as_of: datetime) -> StepInputs:
        # One snapshot for every read of the step, so the inputs are mutually consistent.
        with self._engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
            members = point_in_time.universe(conn, as_of)
            member_ids = [m.security_id for m in members]
            features = {
                f.security_id: f
                for f in point_in_time.feature_rows(conn, member_ids, as_of, self._version)
            }
            ids = [i for i in member_ids if i in features]
            if skipped := sorted(set(member_ids) - set(ids)):
                log.warning("no %s features at %s for securities %s", self._version, as_of, skipped)
            if not ids:
                raise EmptyUniverseError(f"no universe members with features at {as_of}")

            listed_on = as_of.astimezone(UTC).date()
            securities = point_in_time.securities(conn, ids, listed_on=listed_on)
            by_sid = {s.security_id: s for s in securities}
            ids = [i for i in ids if i in by_sid]
            if not ids:
                raise EmptyUniverseError(f"no listed universe members at {as_of}")

            bars: dict[int, list[PriceBar]] = defaultdict(list)
            for b in point_in_time.prices(conn, ids, as_of, PRICE_LOOKBACK):
                bars[b.security_id].append(b)
            insiders: dict[int, list[InsiderTxn]] = defaultdict(list)
            for t in point_in_time.insider_txns(conn, ids, as_of, INSIDER_LOOKBACK):
                insiders[t.security_id].append(t)
            news: dict[int, list[NewsItem]] = defaultdict(list)
            for n in point_in_time.news(conn, ids, as_of, NEWS_LOOKBACK):
                for sid in n.security_ids:
                    if sid in by_sid:
                        news[sid].append(n)
            facts = {sid: point_in_time.fundamentals(conn, sid, as_of) for sid in ids}
            aliases = add_people(
                point_in_time.alias_list(conn, as_of, self._brands, ids),
                {sid: sorted({t.filer for t in insiders[sid]}) for sid in ids},
            )

        tokens = assign_entity_tokens(ids)
        tier = {m.security_id: m.mcap_tier for m in members}
        entities = [
            EntityData(
                security_id=sid,
                entity_token=tokens[sid],
                cik=by_sid[sid].cik,
                sector=by_sid[sid].sector or "Unknown",
                mcap_tier=tier[sid],
                features=features[sid],
                facts=facts[sid],
                insiders=insiders[sid],
                news=news[sid],
                adv30_usd=_adv(bars[sid]),
            )
            for sid in ids
        ]
        universe: list[FeatureRow] = [features[sid] for sid in ids]
        return StepInputs(
            securities=[by_sid[sid] for sid in ids],
            brands=self._brands,
            entities=entities,
            partitioner=Partitioner(
                as_of=as_of,
                masker=AliasMasker(aliases, tokens),
                universe=universe,
                sectors={e.security_id: e.sector for e in entities},
                regime=None,
            ),
            base_rates=self._base_rates,
        )


def _adv(bars: Sequence[PriceBar]) -> float | None:
    """Mean daily dollar volume over the last 30 sessions; ``None`` if fewer are known."""
    recent = sorted(bars, key=lambda b: b.event_time)[-ADV_SESSIONS:]
    if len(recent) < ADV_SESSIONS:
        return None
    return sum(b.close * b.volume for b in recent) / len(recent)
