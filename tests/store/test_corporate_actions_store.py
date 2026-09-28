"""P6.4 storage and ingestion: versioned actions, per-security coverage, symbol continuity,
listing evidence and the delisting ledger, against a migrated Postgres."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.exc import DBAPIError

from contracts.corporate_actions import (
    AssetStatusObservation,
    CorporateAction,
    DelistingFiling,
    SecuritySymbol,
)
from contracts.data import PriceBar
from contracts.enums import (
    ActionInterpretation,
    AssetStatus,
    CorporateActionType,
    DelistingStatus,
    Form25Provision,
    KnowledgeBasis,
    PriceFeed,
    SymbolSource,
    TerminalReturnSource,
)
from contracts.errors import ImmutableConflictError
from ingest.corporate_actions import (
    AlpacaCorporateActions,
    CorporateActionsIngestor,
    ProviderAction,
    to_action,
)
from store import as_of, write
from store import as_of_sensitivity as sensitivity
from store.migrate import alembic_config
from tests.corporate_actions_support import EXAMPLES, coverage, page, pages_transport, record, utc
from tests.market_data_support import coverage_for, et, sessions_2024
from tests.services import scratch_database
from universe.delistings import covers

START, END = date(2024, 3, 1), date(2024, 3, 31)
T1, T2, T3 = utc(2024, 4, 1), utc(2024, 4, 2), utc(2024, 4, 3)
WIPE = (
    "TRUNCATE security_symbols, corporate_actions, corporate_action_coverage, "
    "asset_status_observations, delisting_filings, delistings, price_bars, trading_calendar, "
    "calendar_coverage, securities RESTART IDENTITY CASCADE"
)


@pytest.fixture
def engine(pg_engine: Engine) -> Iterator[Engine]:
    with pg_engine.begin() as c:
        c.execute(text(WIPE))
    yield pg_engine
    with pg_engine.begin() as c:
        c.execute(text(WIPE))


def security(engine: Engine, ticker: str = "AAA", cik: int = 1001) -> int:
    with engine.begin() as c:
        return write.ensure_security(
            c, ticker=ticker, cik=cik, name=f"{ticker} Inc", at=utc(2024, 1, 1)
        )


def ingestor(
    engine: Engine, pages: Sequence[dict[str, Any] | int], at: datetime
) -> CorporateActionsIngestor:
    client = AlpacaCorporateActions(
        pages_transport(list(pages)), key_id="k", secret="s", sleep=lambda _: None, clock=lambda: at
    )
    return CorporateActionsIngestor(engine, actions=client)


def run(
    engine: Engine,
    pages: Sequence[dict[str, Any] | int],
    at: datetime,
    basis: KnowledgeBasis = KnowledgeBasis.PROSPECTIVE,
) -> Any:
    ing = ingestor(engine, pages, at)
    return ing.sync(ing.scopes(at), START, END, basis=basis)


def dividend(**kw: Any) -> dict[str, Any]:
    return page(
        {CorporateActionType.CASH_DIVIDEND: [record(CorporateActionType.CASH_DIVIDEND, **kw)]}
    )


def actions_at(engine: Engine, sid: int, at: datetime) -> list[CorporateAction]:
    with engine.connect() as c:
        return as_of.corporate_actions(c, [sid], at)


def count(engine: Engine, table: str) -> int:
    with engine.connect() as c:
        return int(c.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


# --- point in time -------------------------------------------------------------------------------


def test_late_arriving_action_never_leaks_into_an_earlier_as_of(engine: Engine) -> None:
    sid = security(engine)
    assert run(engine, [page({})], T1).errors == []
    assert run(engine, [dividend()], T2).errors == []
    assert actions_at(engine, sid, T1) == []  # the ex/process date (March) is not availability
    (a,) = actions_at(engine, sid, T2)
    assert a.available_at == T2 and a.ex_date == date(2024, 3, 4)
    with engine.connect() as c:
        at_t1 = as_of.action_coverage(c, sid, T1)
    # At T1 the complete query truly showed nothing: that is what was knowable then.
    assert len(at_t1) == 1 and at_t1[0].action_count == 0
    assert covers(at_t1, START, END)


def test_backfill_rows_are_isolated_from_headline_readers(engine: Engine) -> None:
    sid = security(engine)
    run(engine, [dividend()], T1, KnowledgeBasis.BACKFILL)
    assert actions_at(engine, sid, T3) == []
    with engine.connect() as c:
        assert as_of.action_coverage(c, sid, T3) == []
        assert len(sensitivity.sensitivity_action_coverage(c, sid, T3)) == 1
        (b,) = sensitivity.sensitivity_corporate_actions(c, [sid], T3)
    assert b.knowledge_basis is KnowledgeBasis.BACKFILL
    # A later prospective poll of the same payload is its own, prospective observation.
    run(engine, [dividend()], T2)
    (p,) = actions_at(engine, sid, T3)
    assert p.knowledge_basis is KnowledgeBasis.PROSPECTIVE and p.available_at == T2


def test_revised_action_is_a_new_version_and_disappearance_a_withdrawal(engine: Engine) -> None:
    sid = security(engine)
    run(engine, [dividend()], T1)
    run(engine, [dividend(rate=0.15)], T2)
    assert actions_at(engine, sid, T1 + timedelta(hours=1))[0].cash_rate == 0.125
    assert actions_at(engine, sid, T2)[0].cash_rate == 0.15
    run(engine, [page({})], T3)
    assert actions_at(engine, sid, T3) == []
    assert actions_at(engine, sid, T2)[0].cash_rate == 0.15  # history unchanged
    with engine.connect() as c:
        rows: list[bool] = list(
            c.execute(text("SELECT withdrawn FROM corporate_actions ORDER BY available_at"))
            .scalars()
            .all()
        )
    assert rows == [False, False, True]  # nothing deleted
    run(engine, [page({})], T3)  # exact replay
    assert count(engine, "corporate_actions") == 3
    run(engine, [dividend(rate=0.15)], T3 + timedelta(days=1))  # reappears
    assert actions_at(engine, sid, T3 + timedelta(days=1))[0].cash_rate == 0.15


def test_every_supported_type_is_persisted_uninterpreted_ones_flagged(engine: Engine) -> None:
    sid = security(engine)
    security(engine, "BBB", 2002)
    run(engine, [page({k: [v] for k, v in EXAMPLES.items()})], T1)
    got = actions_at(engine, sid, T1)
    assert {a.action_type for a in got} == set(CorporateActionType)
    flagged = {a.action_type for a in got if a.interpretation is ActionInterpretation.UNINTERPRETED}
    assert flagged == {
        CorporateActionType.UNIT_SPLIT,
        CorporateActionType.SPIN_OFF,
        CorporateActionType.RIGHTS_DISTRIBUTION,
        CorporateActionType.PARTIAL_CALL,
        CorporateActionType.REORGANIZATION,
        CorporateActionType.CAPITAL_GAINS_DISTRIBUTION,
    }


# --- coverage ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pages",
    [
        [401],
        [page({}, "t1"), 500, 500, 500, 500],
        [page({}, "t1"), page({}, "t1")],
        [{**dividend(), "next_page_token": "t1"}, 403],
    ],
)
def test_failed_or_partial_query_advances_nothing(engine: Engine, pages: list[Any]) -> None:
    security(engine)
    result = run(engine, pages, T1)
    assert result.errors
    assert count(engine, "corporate_action_coverage") == 0
    assert count(engine, "corporate_actions") == 0


def test_complete_query_proves_absence_only_inside_its_range(engine: Engine) -> None:
    sid = security(engine)
    run(engine, [page({})], T1)
    with engine.connect() as c:
        cov = as_of.action_coverage(c, sid, T1)
        assert as_of.action_coverage(c, sid, T1 - timedelta(seconds=1)) == []
    assert covers(cov, START, END)
    assert not covers(cov, date(2024, 2, 28), END)
    (cv,) = cov
    assert cv.symbols == ("AAA",) and cv.date_filter.value == "process_date"


def test_coverage_must_describe_exactly_its_actions(engine: Engine) -> None:
    sid = security(engine)
    a = to_action(
        ProviderAction(
            CorporateActionType.CASH_DIVIDEND, record(CorporateActionType.CASH_DIVIDEND)
        ),
        security_id=sid,
        observed_at=T1,
        basis=KnowledgeBasis.PROSPECTIVE,
    )
    empty = coverage(sid, start=START, end=END, established_at=T1)
    with engine.begin() as c, pytest.raises(ValueError, match="does not describe"):
        write.record_action_query(c, coverages=[empty], observed=[a])


def test_contradicting_replay_fails_loudly_and_rows_are_insert_only(engine: Engine) -> None:
    sid = security(engine)
    run(engine, [dividend()], T1)
    changed = to_action(
        ProviderAction(
            CorporateActionType.CASH_DIVIDEND, record(CorporateActionType.CASH_DIVIDEND, rate=9.0)
        ),
        security_id=sid,
        observed_at=T1,
        basis=KnowledgeBasis.PROSPECTIVE,
    )
    cv = coverage(
        sid,
        start=START,
        end=END,
        established_at=T1,
        pairs=[(changed.provider_action_id, changed.source_version)],
    )
    with engine.begin() as c, pytest.raises(ImmutableConflictError):
        write.record_action_query(c, coverages=[cv], observed=[changed])
    for stmt in (
        "UPDATE corporate_actions SET cash_rate = 1",
        "DELETE FROM corporate_action_coverage",
    ):
        with engine.begin() as c, pytest.raises(DBAPIError):
            c.execute(text(stmt))


# --- identity ------------------------------------------------------------------------------------


def test_symbol_change_preserves_security_id(engine: Engine) -> None:
    sid = security(engine)
    name_change = page({CorporateActionType.NAME_CHANGE: [record(CorporateActionType.NAME_CHANGE)]})
    assert run(engine, [name_change], T1).errors == []
    with engine.begin() as c:
        assert write.security_ids_by_ticker(c) == {"ZZZ": sid}
        assert write.ensure_security(c, ticker="ZZZ", cik=1001, name="ZZZ Inc") == sid
        hist = as_of.security_symbols(c, sid, T1)
    assert [(h.symbol, h.source) for h in hist] == [
        ("AAA", SymbolSource.SEED),
        ("ZZZ", SymbolSource.NAME_CHANGE),
    ]
    assert ingestor(engine, [], T2).scopes(T2)[0].symbols == ("AAA", "ZZZ")
    assert count(engine, "securities") == 1
    # The rename is identity continuity, not a delisting.
    ing = ingestor(engine, [], T2)
    assert ing.run_delistings(T2).errors == []
    with engine.connect() as c:
        d = as_of.delisting(c, sid, T2)
    assert d is not None and d.status is DelistingStatus.IDENTITY_CHANGED


def test_share_classes_of_one_issuer_stay_distinct(engine: Engine) -> None:
    a = security(engine, "BRK.A", 1067983)
    b = security(engine, "BRK.B", 1067983)
    assert a != b
    with engine.begin() as c:
        assert write.ensure_security(c, ticker="BRK.B", cik=1067983, name="B") == b
        conflict = write.apply_name_change(
            c,
            SecuritySymbol(
                security_id=a,
                symbol="BRK.B",
                valid_from=date(2024, 3, 1),
                source=SymbolSource.NAME_CHANGE,
                source_ref="x",
                available_at=T1,
            ),
        )
        assert conflict is not None and conflict.holder_security_id == b
        assert write.security_ids_by_ticker(c) == {"BRK.A": a, "BRK.B": b}
        assert as_of.security_cik(c, a) == (1067983, 2)


def test_migration_seeded_symbols_keep_existing_ids_resolvable(engine: Engine) -> None:
    sid = security(engine)
    with engine.connect() as c:
        (h,) = as_of.security_symbols(c, sid, utc(2030, 1, 1))
    assert (h.symbol, h.valid_from, h.source) == ("AAA", date(1, 1, 1), SymbolSource.SEED)


# --- delistings end to end -----------------------------------------------------------------------


def test_later_consideration_supersedes_the_default_and_both_are_kept(engine: Engine) -> None:
    sid = security(engine)
    unknown = page(
        {CorporateActionType.CASH_MERGER: [record(CorporateActionType.CASH_MERGER, rate=0)]}
    )
    run(engine, [unknown], T1)
    ing = ingestor(engine, [], T1)
    assert ing.run_delistings(T1).errors == []
    assert ing.run_delistings(T1 + timedelta(hours=1)).written == 0  # unchanged conclusion
    run(
        engine,
        [page({CorporateActionType.CASH_MERGER: [record(CorporateActionType.CASH_MERGER)]})],
        T2,
    )
    assert ingestor(engine, [], T2).run_delistings(T2).errors == []
    with engine.connect() as c:
        hist = as_of.delisting_history(c, sid, T3)
        early = as_of.delisting(c, sid, T1 + timedelta(hours=2))
    assert [h.terminal_return_source for h in hist] == [
        TerminalReturnSource.DEFAULT,
        TerminalReturnSource.CONSIDERATION,
    ]
    assert early is not None and early.terminal_return == -0.3
    assert hist[-1].cash_per_share == pytest.approx(5.37)


def test_status_derived_default_through_the_ingestor(engine: Engine) -> None:
    sid = security(engine)
    sessions = sessions_2024()
    with engine.begin() as c:
        write.insert_calendar_range(
            c, sessions, coverage_for(sessions, date(2024, 1, 1), date(2024, 12, 31))
        )
        write.insert_asset_status(
            c,
            [
                AssetStatusObservation(
                    security_id=sid,
                    symbol="AAA",
                    observed_at=utc(2024, 3, 20, 12),
                    status=AssetStatus.INACTIVE,
                    tradable=False,
                    source_version="a",
                ),
                AssetStatusObservation(
                    security_id=sid,
                    symbol="AAA",
                    observed_at=utc(2024, 3, 21, 23),
                    status=AssetStatus.INACTIVE,
                    tradable=False,
                    source_version="b",
                ),
            ],
        )
        write.insert_delisting_filings(
            c,
            [
                DelistingFiling(
                    cik=1001,
                    accession="0001234567-24-000001",
                    form="25",
                    filing_date=date(2024, 3, 8),
                    earliest_effective_date=date(2024, 3, 18),
                    rule_provision=Form25Provision.C,
                    available_at=utc(2024, 3, 8, 21),
                )
            ],
        )
        write.insert_price_bars(
            c,
            [
                PriceBar(
                    security_id=sid,
                    event_time=et(2024, 3, d),
                    available_at=et(2024, 3, d, 16, 15),
                    source_version="sip",
                    open=10,
                    high=10,
                    low=10,
                    close=10,
                    volume=1,
                    feed=PriceFeed.SIP,
                )
                for d in (4, 5, 6, 7, 8)
            ],
        )
    now = utc(2024, 4, 1, 22)
    ing = CorporateActionsIngestor(
        engine,
        actions=AlpacaCorporateActions(
            pages_transport([page({})]), key_id="k", secret="s", clock=lambda: now
        ),
    )
    ing.sync(ing.scopes(now), date(2024, 3, 1), date(2024, 3, 31), basis=KnowledgeBasis.PROSPECTIVE)
    assert ing.run_delistings(now).errors == []
    with engine.connect() as c:
        d = as_of.delisting(c, sid, now)
    assert d is not None and d.terminal_return_source is TerminalReturnSource.DEFAULT
    assert d.reason.value == "listing_terminated"


def test_evidence_and_delisting_writers_are_idempotent_and_conflict_checked(engine: Engine) -> None:
    sid = security(engine)
    obs = AssetStatusObservation(
        security_id=sid,
        symbol="AAA",
        observed_at=T1,
        status=AssetStatus.ACTIVE,
        tradable=True,
        source_version="a",
    )
    with engine.begin() as c:
        assert write.insert_asset_status(c, [obs]) == 1
        assert write.insert_asset_status(c, [obs]) == 0
    with engine.begin() as c, pytest.raises(ImmutableConflictError):
        write.insert_asset_status(c, [obs.model_copy(update={"status": AssetStatus.INACTIVE})])


# --- migration -----------------------------------------------------------------------------------


def test_migration_0008_up_down_up(pg_engine: Engine) -> None:
    server = pg_engine.url.set(database="postgres").render_as_string(hide_password=False)
    with scratch_database(server) as url:
        cfg = alembic_config(url)
        command.upgrade(cfg, "0007")
        eng = create_engine(url)
        with eng.begin() as c:
            c.execute(
                text("INSERT INTO securities (ticker, cik, name) VALUES ('OLD', 7, 'Old Inc')")
            )
        command.upgrade(cfg, "head")
        with eng.connect() as c:
            seeded = c.execute(text("SELECT symbol, source FROM security_symbols")).all()
        assert seeded == [("OLD", "seed")]
        command.downgrade(cfg, "0007")
        with eng.connect() as c:
            assert c.execute(text("SELECT to_regclass('corporate_actions')")).scalar() is None
        command.upgrade(cfg, "head")
        with eng.connect() as c:
            assert c.execute(select(text("count(*)")).select_from(text("delistings"))).scalar() == 0
        eng.dispose()


def test_a_renamed_securitys_former_ticker_stays_masked(engine: Engine) -> None:
    sid = security(engine)
    name_change = page({CorporateActionType.NAME_CHANGE: [record(CorporateActionType.NAME_CHANGE)]})
    run(engine, [name_change], T1)
    with engine.connect() as c:
        (sec,) = as_of.alias_list(c, T2).securities
    tickers = {a.text for a in sec.aliases if a.kind.value == "ticker"}
    assert sec.security_id == sid and tickers == {"AAA", "ZZZ"}


# --- P6.4a: dated attribution, rename conflicts, tombstone scope, backfill isolation -------------

RENAME = page({CorporateActionType.NAME_CHANGE: [record(CorporateActionType.NAME_CHANGE)]})  # 3/15


def div_on(symbol: str, day: str, aid: str) -> dict[str, Any]:
    return record(
        CorporateActionType.CASH_DIVIDEND, id=aid, symbol=symbol, process_date=day, ex_date=day
    )


def rename_then_reuse(engine: Engine) -> tuple[int, int]:
    """A renames AAA -> ZZZ on 3/15; unrelated issuer B later lists as AAA."""
    a = security(engine)
    assert run(engine, [RENAME], T1).errors == []
    with engine.begin() as c:
        b = write.ensure_security(c, ticker="AAA", cik=5005, name="New AAA", at=utc(2024, 3, 20))
    return a, b


def test_reused_ticker_is_attributed_by_date_in_one_batch(engine: Engine) -> None:
    a, b = rename_then_reuse(engine)
    both = page(
        {
            CorporateActionType.CASH_DIVIDEND: [
                div_on("AAA", "2024-03-05", "old-div"),  # A traded as AAA then
                div_on("AAA", "2024-03-25", "new-div"),  # B trades as AAA now
            ],
            CorporateActionType.NAME_CHANGE: [record(CorporateActionType.NAME_CHANGE)],
        }
    )
    result = run(engine, [both], T2)
    assert result.errors == []
    assert [x.provider_action_id for x in actions_at(engine, a, T2)] == ["old-div", "nc-1"]
    assert [x.provider_action_id for x in actions_at(engine, b, T2)] == ["new-div"]


def test_reused_ticker_is_attributed_by_date_in_separate_batches(engine: Engine) -> None:
    a, b = rename_then_reuse(engine)
    both = page(
        {
            CorporateActionType.CASH_DIVIDEND: [
                div_on("AAA", "2024-03-05", "old-div"),
                div_on("AAA", "2024-03-25", "new-div"),
            ],
            CorporateActionType.NAME_CHANGE: [record(CorporateActionType.NAME_CHANGE)],
        }
    )
    client = AlpacaCorporateActions(
        pages_transport([both, both]), key_id="k", secret="s", clock=lambda: T2
    )
    ing = CorporateActionsIngestor(engine, actions=client, batch_size=1)
    assert ing.sync(ing.scopes(T2), START, END, basis=KnowledgeBasis.PROSPECTIVE).errors == []
    assert [x.provider_action_id for x in actions_at(engine, a, T2)] == ["old-div", "nc-1"]
    assert [x.provider_action_id for x in actions_at(engine, b, T2)] == ["new-div"]
    # Nothing was withdrawn or double-attributed: rename + one dividend per security.
    assert count(engine, "corporate_actions") == 3


def test_unattributable_action_withholds_only_the_affected_coverage(engine: Engine) -> None:
    a, b = rename_then_reuse(engine)
    security(engine, "CCC", 3003)
    gap = page({CorporateActionType.CASH_DIVIDEND: [div_on("AAA", "2024-03-17", "gap-div")]})
    result = run(engine, [gap], T2)  # 3/17: after A's rename, before B existed
    assert len(result.errors) == 2 and all("coverage withheld" in e for e in result.errors)
    with engine.connect() as c:
        assert as_of.action_coverage(c, a, T2)[-1].established_at == T1  # no new coverage
        assert as_of.action_coverage(c, b, T2) == []
        assert len(as_of.action_coverage(c, 3, T2)) == 1  # CCC still covered


def test_rename_conflict_never_rolls_back_actions_or_coverage(engine: Engine) -> None:
    """Normal order: the SEC seed creates ZZZ before Alpaca's name change arrives."""
    a = security(engine)
    z = security(engine, "ZZZ", 1001)  # same issuer, seeded from the updated ticker list
    both = page(
        {
            CorporateActionType.NAME_CHANGE: [record(CorporateActionType.NAME_CHANGE)],
            CorporateActionType.CASH_DIVIDEND: [div_on("AAA", "2024-03-05", "div")],
        }
    )
    result = run(engine, [both], T1)
    assert len(result.errors) == 1 and "identity conflict" in result.errors[0]
    assert {x.provider_action_id for x in actions_at(engine, a, T1)} == {"nc-1", "div"}
    with engine.connect() as c:
        assert len(as_of.action_coverage(c, a, T1)) == 1
        (conflict,) = as_of.identity_conflicts(c, T1)
        assert (conflict.security_id, conflict.symbol, conflict.holder_security_id) == (a, "ZZZ", z)
        assert [h.symbol for h in as_of.security_symbols(c, a, T1)] == ["AAA"]  # not mutated
        assert write.security_ids_by_ticker(c) == {"AAA": a, "ZZZ": z}
    assert run(engine, [both], T1).errors == result.errors  # replay: same conflict, no duplicate
    assert count(engine, "identity_conflicts") == 1


def test_new_share_class_reusing_a_retired_ticker_stays_distinct(engine: Engine) -> None:
    a = security(engine)
    run(engine, [RENAME], T1)
    with engine.begin() as c:
        cls = write.ensure_security(c, ticker="AAA", cik=1001, name="AAA Class B", at=T2)
        seeds = as_of.security_symbols(c, cls, T2)
    assert cls != a
    assert [(h.symbol, h.valid_from) for h in seeds] == [("AAA", T2.date())]


def test_withdrawal_requires_the_prior_symbol_in_the_new_scope(engine: Engine) -> None:
    sid = security(engine)
    run(engine, [dividend()], T1)
    (prior,) = actions_at(engine, sid, T1)
    narrower = coverage(sid, start=START, end=END, established_at=T2, symbols=("QQQ",))
    with engine.begin() as c:
        assert write.record_action_query(c, coverages=[narrower], observed=[]) == 0
    assert actions_at(engine, sid, T2) == [prior]  # not withdrawn
    same = coverage(sid, start=START, end=END, established_at=T3, symbols=("AAA",))
    with engine.begin() as c:
        assert write.record_action_query(c, coverages=[same], observed=[]) == 1
    assert actions_at(engine, sid, T3) == []


def test_backfilled_rename_is_evidence_only(engine: Engine) -> None:
    sid = security(engine)
    assert run(engine, [RENAME], T1, KnowledgeBasis.BACKFILL).errors == []
    with engine.connect() as c:
        assert write.security_ids_by_ticker(c) == {"AAA": sid}
        assert [h.symbol for h in as_of.security_symbols(c, sid, T2)] == ["AAA"]
        assert as_of.corporate_actions(c, [sid], T2) == []
        (nc,) = sensitivity.sensitivity_corporate_actions(c, [sid], T2)
    assert nc.action_type is CorporateActionType.NAME_CHANGE


def test_primary_readers_take_no_backfill_switch() -> None:
    import inspect

    for fn in (as_of.corporate_actions, as_of.action_coverage, as_of.delisting_history):
        assert "include_backfill" not in inspect.signature(fn).parameters


def test_import_linter_forbids_the_sensitivity_readers_in_production_paths() -> None:
    import tomllib
    from pathlib import Path

    cfg = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text())
    contracts = cfg["tool"]["importlinter"]["contracts"]
    (rule,) = [x for x in contracts if "store.as_of_sensitivity" in x.get("forbidden_modules", [])]
    assert {"evaluation", "risk", "committee", "orchestration", "agents"} <= set(
        rule["source_modules"]
    )
