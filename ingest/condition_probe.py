"""Credentialed smoke probe for Alpaca trade conditions and historical SIP entitlement (§4.6).

    ALPACA_API_KEY_ID=... ALPACA_API_SECRET=... \\
        uv run python -m ingest.condition_probe --out proposed_trade_condition_map.yaml

It fetches ``/v2/stocks/meta/conditions/trade?tape=A|B|C`` and compares EVERY returned
tape/code/name with the checked-in provider map, then asks for one page of historical SIP trades.
It fails on any drift, unknown or missing code and on an entitlement refusal.

It never changes validation state. `checked-in trade_condition_map.yaml` ships ``validated: false``
and only the owner flips that after reviewing this report: the probe writes a PROPOSED file
elsewhere, and refuses to write over the checked-in one. Tests use recorded-shape fixtures and need
no keys; without keys this module reports "skipped" and exits 0.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from contracts.enums import Tape
from execution.trade_conditions import MAP_PATH, ProviderCodeMap, ProviderEntry, load_provider_map
from ingest.alpaca_trades import AlpacaTrades, SipEntitlementError


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


@dataclass(frozen=True)
class Finding:
    tape: Tape
    code: str
    kind: str  # unmapped_live_code | name_mismatch | missing_live_code
    detail: str


@dataclass
class ProbeReport:
    findings: list[Finding] = field(default_factory=list)
    entitlement_ok: bool | None = None  # None = not probed
    matched: list[ProviderEntry] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings and self.entitlement_ok is not False


def compare_conditions(
    pmap: ProviderCodeMap, live: Mapping[Tape, Mapping[str, str]]
) -> ProbeReport:
    """Every live tape/code/name against the map; drift in either direction is a finding."""
    report = ProbeReport()
    by_key = {(e.tape, e.provider_code): e for e in pmap.entries}
    for tape, codes in live.items():
        for code, name in sorted(codes.items()):
            entry = by_key.get((tape, code))
            if entry is None:
                report.findings.append(
                    Finding(
                        tape, code, "unmapped_live_code", f"live name {name!r} is not in the map"
                    )
                )
            elif _norm(entry.expected_provider_name) != _norm(name):
                detail = f"live {name!r} != expected {entry.expected_provider_name!r}"
                report.findings.append(Finding(tape, code, "name_mismatch", detail))
            else:
                report.matched.append(entry)
        for (etape, code), entry in by_key.items():
            if etape is tape and code not in codes:
                report.findings.append(
                    Finding(
                        tape,
                        code,
                        "missing_live_code",
                        f"expected {entry.expected_provider_name!r}",
                    )
                )
    return report


def propose_map(report: ProbeReport) -> str:
    """A PROPOSED map: only exactly-matching entries are marked validated. For owner review."""
    tapes: dict[str, list[dict[str, object]]] = {}
    for e in report.matched:
        tapes.setdefault(e.tape.value, []).append(
            {
                "provider_code": e.provider_code,
                "spec_code": e.spec_code,
                "expected_provider_name": e.expected_provider_name,
                "spec_name": e.spec_name,
                "validated": True,
            }
        )
    header = (
        "# PROPOSED by ingest.condition_probe. NOT the checked-in map: review it against the\n"
        "# CTS/UTP specifications, then commit it yourself. The probe never flips `validated`\n"
        "# in the repo.\n"
    )
    return header + yaml.safe_dump({"tapes": tapes}, sort_keys=True)


def probe_entitlement(trades: AlpacaTrades, *, now: datetime) -> bool:
    """One small historical SIP request well outside the real-time restriction."""
    end = now - timedelta(days=3)
    try:
        trades.historical_trades("SPY", end - timedelta(minutes=1), end)
    except SipEntitlementError:
        return False
    return True


def run_probe(trades: AlpacaTrades, pmap: ProviderCodeMap, *, now: datetime) -> ProbeReport:
    live = {tape: trades.conditions(tape) for tape in Tape}
    report = compare_conditions(pmap, live)
    report.entitlement_ok = probe_entitlement(trades, now=now)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out", type=Path, help="write the PROPOSED map here (never the checked-in map)"
    )
    args = parser.parse_args(argv)
    key, secret = os.environ.get("ALPACA_API_KEY_ID"), os.environ.get("ALPACA_API_SECRET")
    if not key or not secret:
        print("skipped: no ALPACA_API_KEY_ID / ALPACA_API_SECRET")
        return 0
    if args.out is not None and args.out.resolve() == MAP_PATH.resolve():
        print("refused: the probe never writes the checked-in map; choose another --out")
        return 2
    trades = AlpacaTrades(key_id=key, secret=secret)
    report = run_probe(trades, load_provider_map(), now=datetime.now(UTC))
    for f in report.findings:
        print(f"{f.kind}: tape {f.tape.value} code {f.code!r}: {f.detail}")
    print(f"historical SIP entitlement: {'ok' if report.entitlement_ok else 'REFUSED'}")
    if args.out is not None:
        args.out.write_text(propose_map(report), encoding="utf-8")
        print(f"proposed map written to {args.out} (validated flags are the owner's to commit)")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
