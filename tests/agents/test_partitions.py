"""Information boundaries, evidence IDs, point-in-time filtering and the insider/news/fundamentals
transformations (§3, §5, §12.1)."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

import pytest

from agents.partitioner import person_token
from agents.partitions import (
    REGIME_FEATURES,
    Partition,
    Partitioner,
    assign_entity_tokens,
    is_analyst_item,
    quarterly_history,
    role_token,
    trade_tag,
)
from contracts.data import FundamentalFact
from contracts.enums import AgentName, FeedName, InsiderRole, InsiderTxnCode
from features.renderer import RenderError
from tests.agents.world import AS_OF, CIK, World, build_world, txn


@pytest.fixture(scope="module")
def world() -> World:
    return build_world()


@pytest.fixture(scope="module")
def parts(world: World) -> dict[AgentName, Partition]:
    built = world.partitioner.build_all(world.entity)
    built[AgentName.RED_TEAM] = world.partitioner.red_team(world.entity)
    return built


def prefixes(part: Partition) -> set[str]:
    return {
        m.group(1) for m in re.finditer(r"^(FIN|INS|NEWS|TECH|MACRO)_", part.text, re.MULTILINE)
    }


# --- §3 "Sees" / "Blinded to" ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        (AgentName.VALUE, {"FIN"}),
        (AgentName.QUALITY_CATALYST, {"FIN", "NEWS"}),
        (AgentName.INSIDER, {"FIN", "INS"}),
        (AgentName.TECHNICAL, {"TECH"}),
        (AgentName.MACRO_NARRATIVE, {"MACRO", "NEWS"}),
        (AgentName.RED_TEAM, {"FIN", "INS", "NEWS", "TECH", "MACRO"}),
    ],
)
def test_each_agent_sees_exactly_its_feeds(
    parts: dict[AgentName, Partition], agent: AgentName, expected: set[str]
) -> None:
    assert prefixes(parts[agent]) == expected


def test_blinded_agents_never_see_forbidden_markers(parts: dict[AgentName, Partition]) -> None:
    assert "insider_trades" not in parts[AgentName.VALUE].text
    assert "news" not in parts[AgentName.INSIDER].text.lower()
    assert "price_features" not in parts[AgentName.QUALITY_CATALYST].text
    assert "fundamentals" not in parts[AgentName.TECHNICAL].text
    assert "fundamentals" not in parts[AgentName.MACRO_NARRATIVE].text
    assert "insider" not in parts[AgentName.MACRO_NARRATIVE].text


def test_header_carries_only_token_sector_and_size_tier(parts: dict[AgentName, Partition]) -> None:
    head = parts[AgentName.VALUE].text.split("\n\n")[0]
    assert head == "## entity\nentity\tTICKER_01\nsector\tSemiconductor equipment\nsize\tMid-Cap"


# --- evidence IDs ----------------------------------------------------------------------------


def test_every_row_id_is_in_the_evidence_set_and_unique(parts: dict[AgentName, Partition]) -> None:
    for agent, part in parts.items():
        ids = re.findall(r"^((?:FIN|INS|NEWS|TECH|MACRO)_[A-Za-z0-9_]+)\t", part.text, re.MULTILINE)
        assert ids and len(ids) == len(set(ids)), agent.value
        assert {row for _, row in part.evidence} == set(ids), agent.value


def test_evidence_sources_map_to_feeds(parts: dict[AgentName, Partition]) -> None:
    ev = parts[AgentName.RED_TEAM].evidence
    assert (FeedName.FUNDAMENTALS, "FIN_Q1") in ev
    assert (FeedName.INSIDER_TRADES, "INS_1") in ev
    assert (FeedName.NEWS, "NEWS_1") in ev
    assert (FeedName.FEATURES, "TECH_1") in ev
    assert any(src is FeedName.REGIME for src, _ in ev)
    assert (FeedName.NEWS, "NEWS_99") not in ev  # an unprovided citation is not evidence


def test_input_hash_is_stable_and_content_sensitive(
    world: World, parts: dict[AgentName, Partition]
) -> None:
    again = world.partitioner.value(world.entity)
    assert again.input_hash == parts[AgentName.VALUE].input_hash
    assert parts[AgentName.VALUE].input_hash != parts[AgentName.TECHNICAL].input_hash


# --- point-in-time (invariant 3) -------------------------------------------------------------


def test_nothing_available_after_as_of_reaches_a_partition(
    parts: dict[AgentName, Partition],
) -> None:
    for part in parts.values():
        assert "FUTURE_MARKER" not in part.text
    insider = parts[AgentName.INSIDER].text
    assert insider.count("\nINS_") == 3  # the Mar 6 buy was accepted after as_of: invisible


def test_news_uses_the_latest_revision_visible_at_as_of(
    parts: dict[AgentName, Partition],
) -> None:
    text = parts[AgentName.QUALITY_CATALYST].text
    assert "Original wording" in text and "Rewritten" not in text


def test_future_facts_are_ignored(world: World) -> None:
    late = world.entity.facts[0].model_copy(
        update={"available_at": AS_OF.replace(year=2026), "value": 1.0e9}
    )
    with_late = quarterly_history([*world.entity.facts, late], AS_OF)
    assert with_late == quarterly_history(world.entity.facts, AS_OF)


def test_restated_fact_visible_at_as_of_replaces_the_original(world: World) -> None:
    latest = max(
        (f for f in world.entity.facts if f.concept == "us-gaap:Revenues"),
        key=lambda f: f.period_end,
    )
    restated = latest.model_copy(
        update={
            "value": latest.value * 2,
            "available_at": latest.available_at + timedelta(days=10),
            "source_version": "0000099-99-000099",
        }
    )
    assert restated.available_at <= AS_OF
    before = quarterly_history(world.entity.facts, AS_OF)[0][2]["gross_margin"]
    after = quarterly_history([*world.entity.facts, restated], AS_OF)[0][2]["gross_margin"]
    assert before is not None and after == pytest.approx(before / 2)
    # the same restatement filed after as_of is invisible
    hidden = restated.model_copy(update={"available_at": AS_OF + timedelta(days=1)})
    assert quarterly_history([*world.entity.facts, hidden], AS_OF)[0][2]["gross_margin"] == before


# --- fundamentals history --------------------------------------------------------------------


def test_history_is_capped_at_twenty_quarters_newest_first(world: World) -> None:
    hist = quarterly_history(world.entity.facts, AS_OF)
    assert [h[0] for h in hist] == [f"Q-{i}" for i in range(1, 21)]
    assert hist[0][1] > hist[-1][1]


def test_history_ratios_are_dimensionless_and_correct(world: World) -> None:
    label, _, ratios = quarterly_history(world.entity.facts, AS_OF)[0]
    assert label == "Q-1"
    assert ratios["gross_margin"] == pytest.approx(27_654_321_000 / 61_234_567_000)
    assert ratios["revenue_growth_yoy"] is not None and abs(ratios["revenue_growth_yoy"]) < 1


def test_year_to_date_rows_are_ignored() -> None:
    def fact(concept: str, start: date | None, value: float) -> FundamentalFact:
        return FundamentalFact(
            security_id=1,
            concept=concept,
            unit="USD",
            period_start=start,
            period_end=date(2024, 12, 31),
            fiscal_period=None,
            form="10-K",
            value=value,
            event_time=datetime(2024, 12, 31, tzinfo=UTC),
            available_at=datetime(2025, 2, 10, tzinfo=UTC),
            source_version="0000001-25-000001",
        )

    facts = [
        fact("us-gaap:Revenues", date(2024, 10, 1), 100.0),  # a quarter
        fact("us-gaap:GrossProfit", date(2024, 1, 1), 400.0),  # a full year: not comparable
    ]
    ((_, _, ratios),) = quarterly_history(facts, AS_OF)
    assert ratios["gross_margin"] is None


# --- insider transformations -----------------------------------------------------------------


def test_routine_vs_opportunistic_vs_unknown(world: World) -> None:
    visible = [t for t in world.entity.insiders if t.available_at <= AS_OF]
    nov_2024 = next(t for t in visible if t.txn_date == date(2024, 11, 15))
    okafor = next(t for t in visible if t.filer == "Okafor Chidi")
    assert trade_tag(nov_2024, visible) == "ROUTINE"
    assert trade_tag(okafor, visible) == "OPPORTUNISTIC"
    short = [okafor]  # no history before the trade: cannot tell, so never OPPORTUNISTIC
    assert trade_tag(okafor, short) == "UNKNOWN"


def test_signal_only_for_opportunistic_non_plan_open_market_buys(
    parts: dict[AgentName, Partition],
) -> None:
    rows = [
        ln.split("\t") for ln in parts[AgentName.INSIDER].text.splitlines() if ln.startswith("INS_")
    ]
    header = next(
        ln.split("\t")
        for ln in parts[AgentName.INSIDER].text.splitlines()
        if ln.startswith("evidence_id\texec")
    )
    col = {name: i for i, name in enumerate(header)}
    by_tag = {(r[col["role"]], r[col["direction"]], r[col["tag"]]): r[col["signal"]] for r in rows}
    assert by_tag[("ROLE_DIRECTOR", "buy", "OPPORTUNISTIC")] == "1"
    assert by_tag[("ROLE_CEO", "buy", "ROUTINE")] == "0"
    planned = next(r for r in rows if r[col["plan_10b5_1"]] == "1")
    assert planned[col["signal"]] == "0"
    assert planned[col["pct_of_holdings"]] == "NA"  # no post_holdings: omitted, agent stays active
    assert planned[col["adv_intensity"]] != "NA"  # the substitute measure is shown


def test_position_change_and_adv_intensity_math() -> None:
    from agents.partitions import adv_intensity, position_change

    buy = txn("X Y", date(2025, 1, 2), shares=100.0, post=400.0, price=10.0)
    assert position_change(buy) == pytest.approx(100 / 300)  # bought 100 on top of 300
    sell = txn(
        "X Y", date(2025, 1, 2), acquired=False, code=InsiderTxnCode.S, shares=100.0, post=400.0
    )
    assert position_change(sell) == pytest.approx(100 / 500)
    assert position_change(txn("X Y", date(2025, 1, 2), post=None)) is None
    assert position_change(txn("X Y", date(2025, 1, 2), shares=100.0, post=100.0)) is None
    assert adv_intensity(buy, 1_000_000.0) == pytest.approx(1000 / 1_000_000)
    assert adv_intensity(buy, None) is None


@pytest.mark.parametrize(
    ("role", "title", "token"),
    [
        (InsiderRole.OFFICER, "Chief Executive Officer", "ROLE_CEO"),
        (InsiderRole.OFFICER, "CFO and Treasurer", "ROLE_CFO"),
        (InsiderRole.OFFICER, "VP Sales", "ROLE_OFFICER"),
        (InsiderRole.DIRECTOR, None, "ROLE_DIRECTOR"),
        (InsiderRole.TEN_PERCENT_OWNER, None, "ROLE_10PCT"),
        (InsiderRole.OTHER, None, "ROLE_OTHER"),
    ],
)
def test_role_tokens(role: InsiderRole, title: str | None, token: str) -> None:
    assert role_token(txn("A B", date(2025, 1, 2), role=role, title=title)) == token


def test_same_insider_gets_the_same_token_in_every_row(
    parts: dict[AgentName, Partition],
) -> None:
    text = parts[AgentName.INSIDER].text
    assert text.count(person_token("DOE JANE Q", CIK)) == 2  # her buy and her sale


# --- news filter -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headline",
    [
        "Analyst upgrades Acme to Buy",
        "Firm downgrades stock after results",
        "Broker raises price target to 50",
        "Acme initiated coverage at Overweight",
        "Bank reiterates Outperform",
    ],
)
def test_analyst_items_are_detected(headline: str) -> None:
    assert is_analyst_item(headline)


def test_ordinary_news_is_kept() -> None:
    assert not is_analyst_item("Acme opens new plant in Ohio")


def test_analyst_item_never_reaches_a_news_partition(parts: dict[AgentName, Partition]) -> None:
    for agent in (AgentName.QUALITY_CATALYST, AgentName.MACRO_NARRATIVE, AgentName.RED_TEAM):
        assert "upgrade" not in parts[agent].text.lower()
        assert "price target" not in parts[agent].text.lower()


# --- construction ----------------------------------------------------------------------------


def test_entity_tokens_depend_on_rank_not_ticker() -> None:
    assert assign_entity_tokens([40, 7, 12]) == {7: "TICKER_01", 12: "TICKER_02", 40: "TICKER_03"}


def test_unknown_regime_feature_is_refused(world: World) -> None:
    with pytest.raises(RenderError):
        Partitioner(
            as_of=AS_OF,
            masker=world.masker,
            universe=world.universe,
            sectors={1: "A"},
            regime={"spy_close_price": 512.34},
        )
    assert "spy_close_price" not in REGIME_FEATURES


def test_naive_as_of_is_refused(world: World) -> None:
    with pytest.raises(ValueError, match="timezone"):
        Partitioner(
            as_of=datetime(2025, 3, 7),
            masker=world.masker,
            universe=world.universe,
            sectors={1: "A"},
        )
