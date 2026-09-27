"""A synthetic company with every identity string and raw number the partitions must never leak."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from agents.partitioner import AliasMasker
from agents.partitions import EntityData, Partitioner, assign_entity_tokens
from contracts.data import (
    FeatureRow,
    FundamentalFact,
    InsiderTxn,
    NewsItem,
    Security,
)
from contracts.enums import InsiderRole, InsiderTxnCode, McapTier, NewsProviderName
from features.renderer import FUNDAMENTAL_FEATURES, PRICE_FEATURES
from store.aliases import add_people, build_alias_list

AS_OF = datetime(2025, 3, 7, 21, 0, tzinfo=UTC)
FUTURE = AS_OF + timedelta(days=3)
CIK = 1234567

# What must never appear in any prompt (case-insensitive, whole word).
IDENTITY = [
    "Zephyr Dynamics",
    "Zephyr",
    "ZPHR",
    "1234567",
    "0001234567",
    "Zephyr Cloud",
    "Nimbus Edge",
    "DOE JANE Q",
    "Jane Doe",
    "Doe",
    "Okafor Chidi",
    "Chidi Okafor",
    "Okafor",
]
# Raw numbers that appear in the underlying rows (>= 4 significant digits).
RAW_FACT = {"revenue": 61_234_567_000.0, "shares": 812_345_678.0}
RAW_TXN_SHARES = 48_213.0
RAW_TXN_PRICE = 187.4321
RAW_POST_HOLDINGS = 351_777.0


def _securities() -> list[Security]:
    def sec(sid: int, name: str, ticker: str, cik: int) -> Security:
        return Security(
            security_id=sid,
            ticker=ticker,
            cik=cik,
            name=name,
            sector="Semiconductor equipment",
            industry=None,
            listed_from=date(2015, 1, 1),
            listed_to=None,
        )

    return [
        sec(1, "Zephyr Dynamics Corp.", "ZPHR", CIK),
        sec(2, "Borealis Systems Inc", "BRLS", 2345678),
        sec(3, "Calder Optics Inc", "CLDR", 3456789),
        sec(4, "Dunmore Analog Ltd", "DNMR", 4567890),
    ]


def _quarter_ends(n: int) -> list[date]:
    ends: list[date] = []
    y, m = 2024, 12
    for _ in range(n):
        last = {3: 31, 6: 30, 9: 30, 12: 31}[m]
        ends.append(date(y, m, last))
        m -= 3
        if m < 1:
            y, m = y - 1, 12
    return ends


def _facts(sid: int) -> list[FundamentalFact]:
    out: list[FundamentalFact] = []
    for i, end in enumerate(_quarter_ends(22)):
        filed = datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=40)
        growth = 1 - 0.015 * i
        rows = {
            "us-gaap:Revenues": RAW_FACT["revenue"] * growth,
            "us-gaap:GrossProfit": 27_654_321_000.0 * growth,
            "us-gaap:OperatingIncomeLoss": 9_876_543_000.0 * growth,
            "us-gaap:NetIncomeLoss": 7_654_321_000.0 * growth,
            "us-gaap:NetCashProvidedByUsedInOperatingActivities": 8_111_111_000.0 * growth,
            "us-gaap:Assets": 141_234_567_000.0 * growth,
            "dei:EntityCommonStockSharesOutstanding": RAW_FACT["shares"] * (1 + 0.002 * i),
        }
        for concept, value in rows.items():
            out.append(
                FundamentalFact(
                    security_id=sid,
                    concept=concept,
                    unit="USD",
                    period_start=None,
                    period_end=end,
                    fiscal_period=None,
                    form="10-Q",
                    value=value,
                    event_time=datetime(end.year, end.month, end.day, tzinfo=UTC),
                    available_at=filed,
                    source_version=f"0000{sid}0-{i:02d}-000001",
                )
            )
    return out


def txn(
    filer: str,
    txn_date: date,
    *,
    code: InsiderTxnCode = InsiderTxnCode.P,
    acquired: bool = True,
    shares: float = RAW_TXN_SHARES,
    price: float | None = RAW_TXN_PRICE,
    post: float | None = RAW_POST_HOLDINGS,
    plan: bool = False,
    role: InsiderRole = InsiderRole.OFFICER,
    title: str | None = "Chief Executive Officer",
    filed_days_after: int = 2,
    seq: int = 0,
    serial: int = 1,
) -> InsiderTxn:
    filed = datetime(txn_date.year, txn_date.month, txn_date.day, 16, tzinfo=UTC) + timedelta(
        days=filed_days_after
    )
    accession = f"00000{serial:05d}-{txn_date.year % 100:02d}-{serial:06d}"
    return InsiderTxn(
        security_id=1,
        accession=accession,
        seq=seq,
        filer=filer,
        role=role,
        officer_title=title,
        txn_date=txn_date,
        code=code,
        acquired=acquired,
        shares=shares,
        price=price,
        post_holdings=post,
        is_10b5_1=plan,
        event_time=datetime(txn_date.year, txn_date.month, txn_date.day, tzinfo=UTC),
        available_at=filed,
        source_version=accession,
    )


def _insiders() -> list[InsiderTxn]:
    routine = [
        txn("DOE JANE Q", date(y, 11, 15), serial=10 + i)
        for i, y in enumerate((2021, 2022, 2023, 2024))
    ]
    return [
        *routine,  # Jane Doe buys every November: ROUTINE in Nov 2024
        txn(
            "Okafor Chidi",
            date(2025, 1, 10),
            role=InsiderRole.DIRECTOR,
            title=None,
            serial=30,
        ),  # a director's one-off buy: OPPORTUNISTIC with 3+ years of visible history
        txn(
            "DOE JANE Q",
            date(2025, 2, 20),
            code=InsiderTxnCode.S,
            acquired=False,
            plan=True,
            post=None,
            shares=9_999.0,
            price=201.5678,
            serial=31,
        ),  # a planned sale with no post-holdings: only the ADV intensity can be shown
        txn(
            "DOE JANE Q",
            date(2025, 3, 6),
            filed_days_after=4,  # accepted after as_of, so invisible
            shares=77_777.0,
            price=333.3333,
            serial=32,
        ),
    ]


def _news() -> list[NewsItem]:
    def item(item_id: str, headline: str, summary: str, at: datetime, avail: datetime) -> NewsItem:
        return NewsItem(
            item_id=item_id,
            security_ids=(1,),
            published_at=at,
            headline=headline,
            summary=summary,
            body_hash="a" * 64,
            source=NewsProviderName.ALPACA,
            publisher="Wire Service",
            url="https://news.example.com/zephyr-dynamics/story",
            event_time=at,
            available_at=avail,
            source_version="v1",
        )

    d = AS_OF
    return [
        item(
            "n1",
            "Zephyr Dynamics (ZPHR) lands $2.5 billion Nimbus Edge contract",
            "CEO Jane Doe said on March 3, 2025 that Zephyr Cloud demand lifted revenue to "
            "$61.2 billion; director Chidi Okafor bought 48,213 shares at $187.4321.",
            d - timedelta(days=9),
            d - timedelta(days=9),
        ),
        item(
            "n2",
            "Analyst upgrades Zephyr Dynamics to Buy, raises price target",
            "Wall Street analysts see upside.",
            d - timedelta(days=5),
            d - timedelta(days=5),
        ),
        item(
            "n3",
            "Zephyr wins customer in Asia",
            "Original wording.",
            d - timedelta(days=4),
            d - timedelta(days=4),
        ),
        item(  # a later rewrite of n3, visible only after as_of
            "n3",
            "FUTURE_MARKER rewritten headline",
            "Rewritten after the fact.",
            d - timedelta(days=4),
            d + timedelta(days=1),
        ),
        item(  # published after as_of
            "n4",
            "FUTURE_MARKER tomorrow's story",
            "Not yet published.",
            d + timedelta(days=1),
            d + timedelta(days=1),
        ),
    ]


def _feature_row(sid: int) -> FeatureRow:
    values: dict[str, object] = {
        n: float(sid) * 0.37 + i * 0.011 for i, n in enumerate(PRICE_FEATURES)
    }
    values.update({n: float(sid) * 0.91 + i * 0.13 for i, n in enumerate(FUNDAMENTAL_FEATURES)})
    values.update({"insider_distinct_buyers_90d": 1, "insider_ceo_cfo_buy_flag_90d": 0})
    return FeatureRow(
        security_id=sid,
        event_time=AS_OF,
        available_at=AS_OF,
        source_version="fs_v1",
        feature_set_version="fs_v1",
        values=values,
    )


REGIME = {
    "spy_return_1m": 0.0312,
    "spy_return_3m": -0.0127,
    "spy_sma_200d_distance": 0.0654,
    "vix_proxy_zscore": 0.4433,
    "yield_10y_pct": 4.2871,
}


@dataclass
class World:
    partitioner: Partitioner
    entity: EntityData
    masker: AliasMasker
    tokens: dict[int, str]
    universe: list[FeatureRow] = field(default_factory=list)


def build_world() -> World:
    securities = _securities()
    aliases = build_alias_list(securities, AS_OF, {CIK: ["Zephyr", "Zephyr Cloud", "Nimbus Edge"]})
    aliases = add_people(aliases, {1: ["DOE JANE Q", "Okafor Chidi"]})
    tokens = assign_entity_tokens([s.security_id for s in securities])
    masker = AliasMasker(aliases, tokens)
    universe = [_feature_row(s.security_id) for s in securities]
    sectors = dict.fromkeys(tokens, "Semiconductor equipment")
    partitioner = Partitioner(
        as_of=AS_OF, masker=masker, universe=universe, sectors=sectors, regime=REGIME
    )
    entity = EntityData(
        security_id=1,
        entity_token=tokens[1],
        cik=CIK,
        sector="Semiconductor equipment",
        mcap_tier=McapTier.MID,
        features=universe[0],
        facts=_facts(1),
        insiders=_insiders(),
        news=_news(),
        adv30_usd=12_345_678.0,
    )
    return World(partitioner, entity, masker, tokens, universe)
