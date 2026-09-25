"""Synthetic market for the backfill smoke fixtures (see README.md: none of this is real data).

``uv run python -m tests.fixtures.synth`` runs the real backfill in ``--record`` mode with this
module standing in for the network, so ``tests/fixtures/http/`` has exactly the files a replay of
the same command needs. Requires ``DATABASE_URL`` and ``REDIS_URL`` (a scratch database is fine).
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from ingest.timeutil import ET

HERE = Path(__file__).parent
HTTP_DIR = HERE / "http"
END = date(2024, 6, 28)
DAYS = 90
TICKERS = ("ALFA", "BRVO", "CHRL", "DLTA", "ECHO")
SMOKE_ARGS = [
    "--days", str(DAYS), "--end", END.isoformat(), "--tickers", ",".join(TICKERS),
    "--universe", str(HERE / "universe_smoke.yaml"),
]  # fmt: skip


@dataclass(frozen=True)
class Co:
    ticker: str
    cik: int
    name: str
    sic: int
    price: float
    volume: float
    shares: float


WORLD = (
    Co("ALFA", 900001, "Alfa Systems Inc", 3571, 41.0, 1.8e6, 120e6),  # IT, mcap ~5B
    Co("BRVO", 900002, "Bravo Software Corp", 7372, 88.0, 0.9e6, 60e6),  # IT, mcap ~5B
    Co("CHRL", 900003, "Charlie Semiconductor", 3674, 23.0, 2.5e6, 300e6),  # IT, mcap ~7B
    Co("DLTA", 900004, "Delta Therapeutics", 2834, 12.0, 0.6e6, 80e6),  # HC, adv ~7M < 10M
    Co("ECHO", 900005, "Echo Bancorp", 6022, 150.0, 1.2e6, 500e6),  # Financials: out of sector
)


def _weekdays(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _accn(cik: int, year: int, n: int) -> str:
    return f"{cik:010d}-{year % 100:02d}-{n:06d}"


def _edgar_ts(dt: datetime) -> str:
    """EDGAR prints Eastern wall time with a 'Z' suffix."""
    return dt.astimezone(ET).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class World:
    def __init__(self, seed: int = 7) -> None:
        self.rng = random.Random(seed)
        start = END - timedelta(days=DAYS + 40)
        self.days = _weekdays(start, END)
        self.bars = {c.ticker: self._bars(c) for c in WORLD}
        self.filings = {c.ticker: self._filings(c) for c in WORLD}

    def _bars(self, c: Co) -> list[dict[str, Any]]:
        out, px = [], c.price
        for d in self.days:
            o = px * (1 + self.rng.gauss(0, 0.004))
            px = max(1.0, px * (1 + self.rng.gauss(0.0003, 0.018)))
            hi, lo = max(o, px) * 1.006, min(o, px) * 0.994
            t = datetime.combine(d, datetime.min.time(), ET).astimezone(UTC)
            out.append({
                "t": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": round(o, 2), "h": round(hi, 2),
                "l": round(lo, 2), "c": round(px, 2), "n": 5000,
                "v": int(c.volume * self.rng.uniform(0.7, 1.3)), "vw": round((o + px) / 2, 2),
            })  # fmt: skip
        return out

    def _filings(self, c: Co) -> list[dict[str, Any]]:
        """10-Q (May), 10-K (Feb) and Form 4s; each Form 4 is accepted 2 business days later."""
        tenk = datetime(2024, 2, 14, 16, 5, tzinfo=ET)
        tenq = datetime(2024, 5, 2, 16, 20, tzinfo=ET)
        out = [
            {"accn": _accn(c.cik, 2024, 11), "form": "10-K", "accepted": tenk, "doc": "k.htm"},
            {"accn": _accn(c.cik, 2024, 41), "form": "10-Q", "accepted": tenq, "doc": "q.htm"},
        ]
        for n, d in enumerate(self.rng.sample(self.days[45:-5], 3)):
            accepted = datetime.combine(
                _weekdays(d, d + timedelta(days=6))[2], datetime.min.time(), ET
            ).replace(hour=17, minute=2)
            out.append(
                {
                    "accn": _accn(c.cik, 2024, 100 + n),
                    "form": "4",
                    "accepted": accepted,
                    "doc": f"xslF345X05/f4_{n}.xml",
                    "txn_date": d,
                    "code": self.rng.choice("PSSA"),
                    "plan": n == 0,
                }
            )
        return sorted(out, key=lambda f: f["accepted"], reverse=True)

    # --- endpoints ---------------------------------------------------------------------------

    def tickers(self) -> dict[str, Any]:
        return {
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [[c.cik, c.name, c.ticker, "Nasdaq"] for c in WORLD],
        }

    def submissions(self, c: Co) -> dict[str, Any]:
        fs = self.filings[c.ticker]
        return {
            "cik": str(c.cik), "name": c.name, "sic": str(c.sic),
            "sicDescription": f"SIC {c.sic}", "tickers": [c.ticker], "exchanges": ["Nasdaq"],
            "filings": {"recent": {
                "accessionNumber": [f["accn"] for f in fs],
                "filingDate": [f["accepted"].date().isoformat() for f in fs],
                "acceptanceDateTime": [_edgar_ts(f["accepted"]) for f in fs],
                "form": [f["form"] for f in fs],
                "primaryDocument": [f["doc"] for f in fs],
            }, "files": []},
        }  # fmt: skip

    def companyfacts(self, c: Co) -> dict[str, Any]:
        k, q = _accn(c.cik, 2024, 11), _accn(c.cik, 2024, 41)
        rev = c.shares * c.price * 0.08

        def e(
            start: str | None, end: str, val: float, accn: str, fp: str, form: str, filed: str
        ) -> dict[str, Any]:
            return {
                **({"start": start} if start else {}),
                "end": end,
                "val": val,
                "accn": accn,
                "fy": 2024,
                "fp": fp,
                "form": form,
                "filed": filed,
            }

        return {"cik": c.cik, "entityName": c.name, "facts": {
            "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
                e(None, "2024-02-01", c.shares, k, "FY", "10-K", "2024-02-14"),
                e(None, "2024-04-26", c.shares * 1.01, q, "Q1", "10-Q", "2024-05-02"),
            ]}}},
            "us-gaap": {"Revenues": {"units": {"USD": [
                e("2023-01-01", "2023-12-31", rev * 4, k, "FY", "10-K", "2024-02-14"),
                e("2024-01-01", "2024-03-31", rev, q, "Q1", "10-Q", "2024-05-02"),
                # 10-Q comparative restates FY2023 revenue: a new accession, a new row.
                e("2023-01-01", "2023-12-31", rev * 3.9, q, "Q1", "10-Q", "2024-05-02"),
            ]}}},
        }}  # fmt: skip

    def form4(self, c: Co, f: dict[str, Any]) -> str:
        acquired = "A" if f["code"] in "PA" else "D"
        price = "" if f["code"] == "A" else f"<value>{c.price:.2f}</value>"
        return f"""<?xml version="1.0"?>
<ownershipDocument><schemaVersion>X0508</schemaVersion><documentType>4</documentType>
<aff10b5One>{1 if f["plan"] else 0}</aff10b5One>
<issuer><issuerCik>{c.cik:010d}</issuerCik><issuerTradingSymbol>{c.ticker}</issuerTradingSymbol>
</issuer><reportingOwner><reportingOwnerId><rptOwnerName>Insider {f["accn"][-3:]}</rptOwnerName>
</reportingOwnerId><reportingOwnerRelationship><isOfficer>1</isOfficer>
<officerTitle>Chief Executive Officer</officerTitle></reportingOwnerRelationship></reportingOwner>
<nonDerivativeTable><nonDerivativeTransaction><securityTitle><value>Common</value></securityTitle>
<transactionDate><value>{f["txn_date"].isoformat()}</value></transactionDate>
<transactionCoding><transactionFormType>4</transactionFormType>
<transactionCode>{f["code"]}</transactionCode></transactionCoding><transactionAmounts>
<transactionShares><value>{1000 + int(f["accn"][-3:])}</value></transactionShares>
<transactionPricePerShare>{price}</transactionPricePerShare>
<transactionAcquiredDisposedCode><value>{acquired}</value></transactionAcquiredDisposedCode>
</transactionAmounts><postTransactionAmounts><sharesOwnedFollowingTransaction><value>50000</value>
</sharesOwnedFollowingTransaction></postTransactionAmounts></nonDerivativeTransaction>
</nonDerivativeTable></ownershipDocument>
"""

    def news(self, symbols: list[str], start: datetime, end: datetime) -> dict[str, Any]:
        items = []
        for c in WORLD:
            if c.ticker not in symbols:
                continue
            for i, d in enumerate(self.days[::9]):
                created = datetime.combine(d, datetime.min.time(), ET).replace(hour=10 + i % 6)
                if not start <= created <= end:
                    continue
                revised = i % 4 == 0  # a quarter of items are edited after publication
                updated = created + (timedelta(hours=2) if revised else timedelta(seconds=1))
                peer = WORLD[(WORLD.index(c) + 1) % len(WORLD)].ticker
                items.append({
                    "id": int(hashlib.sha256(f"{c.ticker}{d}".encode()).hexdigest()[:7], 16),
                    "headline": f"{c.name} update {d.isoformat()}", "author": "Wire",
                    "created_at": created.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "updated_at": updated.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "summary": f"Synthetic item {i}{' (updated)' if revised else ''}.",
                    "content": "", "url": f"https://news.example.test/{c.ticker}/{d}",
                    "images": [], "symbols": [c.ticker, peer], "source": "synthetic",
                })  # fmt: skip
        return {"news": items, "next_page_token": None}


class SyntheticTransport(httpx.BaseTransport):
    def __init__(self, world: World | None = None) -> None:
        self.w = world or World()
        self.by_cik = {c.cik: c for c in WORLD}
        self.by_ticker = {c.ticker: c for c in WORLD}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url, p = request.url, request.url.params
        path = url.path
        body: Any
        if path == "/files/company_tickers_exchange.json":
            body = self.w.tickers()
        elif path.startswith("/submissions/CIK"):
            body = self.w.submissions(self.by_cik[int(path[len("/submissions/CIK") : -5])])
        elif path.startswith("/api/xbrl/companyfacts/CIK"):
            body = self.w.companyfacts(self.by_cik[int(path.rsplit("CIK", 1)[1][:-5])])
        elif path.startswith("/Archives/edgar/data/"):
            _, cik, folder, doc = path.rsplit("/", 3)
            c = self.by_cik[int(cik)]
            f = next(
                f
                for f in self.w.filings[c.ticker]
                if f["accn"].replace("-", "") == folder and f["doc"].endswith(doc)
            )
            return httpx.Response(200, text=self.w.form4(c, f))
        elif path.startswith("/v2/stocks/") and path.endswith("/bars"):
            c = self.by_ticker[path.split("/")[3]]
            lo = date.fromisoformat(p["start"][:10])
            hi = _utc(p["end"])
            bars = [
                b
                for b in self.w.bars[c.ticker]
                if lo <= _utc(b["t"]).astimezone(ET).date() and _utc(b["t"]) <= hi
            ]
            body = {"bars": bars, "symbol": c.ticker, "next_page_token": None}
        elif path == "/v1beta1/news":
            body = self.w.news(p["symbols"].split(","), _utc(p["start"]), _utc(p["end"]))
        else:
            return httpx.Response(404, text=f"synthetic world has no {url}")
        return httpx.Response(200, content=json.dumps(body, sort_keys=True).encode())


def main() -> None:
    from ingest.backfill import main as backfill

    shutil.rmtree(HTTP_DIR, ignore_errors=True)
    rc = backfill([*SMOKE_ARGS, "--record", str(HTTP_DIR)], inner=SyntheticTransport())
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
