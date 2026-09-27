"""The canonical symbol set a halt (or a run's references) needs marks for (§9). Pure.

One definition, used by the halt sweeper and by reference capture: the durable committed book, the
universe snapshot in effect at the run's ``as_of`` and the configured reference instruments (SPY and
the sector ETFs), upper-cased, de-duplicated and sorted, so the same inputs always give the same set
and hash. An empty result is not a discovery and produces nothing.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from contracts.enums import KillTrigger
from contracts.market_data import HaltSymbolSet, symbols_sha256

SOURCE = "committed_book+universe_snapshot+reference_instruments"
VERSION = "halt_symbols_v1"


def canonical_symbols(*groups: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({s.strip().upper() for g in groups for s in g if s.strip()}))


def symbol_set_version(*, universe_snapshot: str | None, config_hash: str) -> str:
    """Names the snapshot and configuration that, with the hash, reproduce the exact set."""
    return f"{VERSION}:snapshot={universe_snapshot or 'none'}:config={config_hash[:16]}"


def reconstruct_symbol_set(
    run_id: UUID,
    trigger: KillTrigger,
    *,
    book: Iterable[str],
    universe: Iterable[str],
    reference_instruments: Iterable[str],
    version: str,
    resolved_at: datetime,
) -> HaltSymbolSet | None:
    symbols = canonical_symbols(book, universe, reference_instruments)
    if not symbols:
        return None
    return HaltSymbolSet(
        run_id=run_id,
        trigger=trigger,
        symbols=symbols,
        symbol_count=len(symbols),
        symbols_sha256=symbols_sha256(symbols),
        source=SOURCE,
        source_version=version,
        resolved_at=resolved_at,
    )
