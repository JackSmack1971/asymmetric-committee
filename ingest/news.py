"""News (§4.1) behind ``NEWS_PROVIDER`` (``alpaca`` | ``alphavantage``, §18.1).

Each distinct text of an item is a new row: ``source_version`` (the revision) combines the
provider's update stamp with a hash of the text, so a silent back-edit still becomes a new version.

``available_at``:
- live polling: the receipt time (never earlier than the provider's own stamp);
- backfill: the provider's last-update stamp, because we only ever see the latest text. For Alpaca
  that is ``updated_at``; Alpha Vantage has no update stamp, so ``time_published`` is used and read
  as US/Eastern (the zone is undocumented; Eastern is the later reading, so no look-ahead).
``timestamp_audit`` flags items whose stamps look unreliable.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx

from contracts.data import NewsItem
from contracts.enums import NewsProviderName
from ingest.timeutil import ET, parse_utc

ALPACA_DATA_URL = "https://data.alpaca.markets"
ALPHAVANTAGE_URL = "https://www.alphavantage.co"


def body_hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _sids(symbols: Sequence[str], sids: Mapping[str, int]) -> tuple[int, ...]:
    return tuple(sorted({sids[s.upper()] for s in symbols if s.upper() in sids}))


class NewsProvider(Protocol):
    name: NewsProviderName

    def fetch(
        self,
        symbols: Sequence[str],
        start: datetime,
        end: datetime,
        sids: Mapping[str, int],
        *,
        received_at: datetime | None = None,
    ) -> list[NewsItem]:
        """Items for ``symbols`` published in ``[start, end]``. ``received_at`` = live polling."""
        ...


def parse_alpaca_news(
    payload: dict[str, Any], sids: Mapping[str, int], *, received_at: datetime | None
) -> list[NewsItem]:
    out = []
    for n in payload.get("news") or []:
        ids = _sids(n.get("symbols") or [], sids)
        if not ids or not n.get("headline"):
            continue
        created = parse_utc(n["created_at"])
        updated = parse_utc(n.get("updated_at") or n["created_at"])
        stamp = max(created, updated)
        digest = body_hash(n["headline"], n.get("summary") or "", n.get("content") or "")
        out.append(
            NewsItem(
                item_id=f"alpaca:{n['id']}",
                security_ids=ids,
                published_at=created,
                headline=n["headline"][:1000],
                summary=(n.get("summary") or "")[:20000],
                body_hash=digest,
                source=NewsProviderName.ALPACA,
                publisher=(n.get("source") or "")[:256],
                url=(n.get("url") or "")[:2048],
                event_time=created,
                available_at=max(stamp, received_at) if received_at else stamp,
                source_version=f"{stamp.strftime('%Y%m%dT%H%M%SZ')}:{digest[:12]}",
            )
        )
    return out


def parse_av_time(s: str) -> datetime:
    """``20240102T133000`` (no zone) read as US/Eastern."""
    return datetime.strptime(s, "%Y%m%dT%H%M%S").replace(tzinfo=ET).astimezone(UTC)


def parse_alphavantage_news(
    payload: dict[str, Any], sids: Mapping[str, int], *, received_at: datetime | None
) -> list[NewsItem]:
    if "feed" not in payload:
        raise ValueError(f"Alpha Vantage error: {payload}")
    out = []
    for n in payload["feed"]:
        symbols = [ts["ticker"] for ts in n.get("ticker_sentiment") or []]
        ids = _sids(symbols, sids)
        if not ids or not n.get("title"):
            continue
        published = parse_av_time(n["time_published"])
        digest = body_hash(n["title"], n.get("summary") or "")
        out.append(
            NewsItem(
                item_id=f"av:{hashlib.sha256(n['url'].encode()).hexdigest()[:24]}",
                security_ids=ids,
                published_at=published,
                headline=n["title"][:1000],
                summary=(n.get("summary") or "")[:20000],
                body_hash=digest,
                source=NewsProviderName.ALPHAVANTAGE,
                publisher=(n.get("source") or "")[:256],
                url=n["url"][:2048],
                event_time=published,
                available_at=max(published, received_at) if received_at else published,
                source_version=f"{published.strftime('%Y%m%dT%H%M%SZ')}:{digest[:12]}",
            )
        )
    return out


class AlpacaNews:
    name = NewsProviderName.ALPACA

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._http = httpx.Client(
            base_url=ALPACA_DATA_URL,
            transport=transport,
            timeout=30.0,
            headers={
                "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY_ID", ""),
                "APCA-API-SECRET-KEY": os.environ.get("ALPACA_API_SECRET", ""),
            },
        )

    def _pages(self, params: dict[str, str]) -> Iterator[dict[str, Any]]:
        token: str | None = None
        while True:
            r = self._http.get(
                "/v1beta1/news", params={**params, **({"page_token": token} if token else {})}
            )
            r.raise_for_status()
            page: dict[str, Any] = r.json()
            yield page
            token = page.get("next_page_token")
            if not token:
                return

    def fetch(
        self,
        symbols: Sequence[str],
        start: datetime,
        end: datetime,
        sids: Mapping[str, int],
        *,
        received_at: datetime | None = None,
    ) -> list[NewsItem]:
        params = {
            "symbols": ",".join(symbols),
            "start": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": "50",
            "sort": "asc",
        }
        return [
            item
            for page in self._pages(params)
            for item in parse_alpaca_news(page, sids, received_at=received_at)
        ]


class AlphaVantageNews:
    name = NewsProviderName.ALPHAVANTAGE
    WINDOW = timedelta(days=30)  # one call returns at most 1000 items

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._http = httpx.Client(base_url=ALPHAVANTAGE_URL, transport=transport, timeout=30.0)
        self._key = os.environ.get("ALPHAVANTAGE_API_KEY", "")

    def fetch(
        self,
        symbols: Sequence[str],
        start: datetime,
        end: datetime,
        sids: Mapping[str, int],
        *,
        received_at: datetime | None = None,
    ) -> list[NewsItem]:
        out: list[NewsItem] = []
        lo = start
        while lo < end:
            hi = min(lo + self.WINDOW, end)
            r = self._http.get(
                "/query",
                params={
                    "function": "NEWS_SENTIMENT",
                    "tickers": ",".join(symbols),
                    "time_from": lo.astimezone(ET).strftime("%Y%m%dT%H%M"),
                    "time_to": hi.astimezone(ET).strftime("%Y%m%dT%H%M"),
                    "sort": "EARLIEST",
                    "limit": "1000",
                    "apikey": self._key,
                },
            )
            r.raise_for_status()
            out.extend(parse_alphavantage_news(r.json(), sids, received_at=received_at))
            lo = hi
        return out


def provider_from_env(
    transport: httpx.BaseTransport | None = None, env: Mapping[str, str] | None = None
) -> NewsProvider:
    name = NewsProviderName((os.environ if env is None else env).get("NEWS_PROVIDER", "alpaca"))
    if name is NewsProviderName.ALPACA:
        return AlpacaNews(transport)
    return AlphaVantageNews(transport)


def timestamp_audit(items: Sequence[NewsItem], *, now: datetime) -> list[str]:
    """Reasons to distrust a provider's stamps (§4.1: reject sources that back-edit silently)."""
    issues = []
    for n in items:
        if n.published_at > now:
            issues.append(f"{n.item_id}: published in the future ({n.published_at})")
        if n.available_at < n.published_at:
            issues.append(f"{n.item_id}: available before publication")
        if n.available_at - n.published_at > timedelta(days=30):
            issues.append(f"{n.item_id}: revised {n.available_at - n.published_at} after publish")
    by_id: dict[str, set[str]] = {}
    for n in items:
        by_id.setdefault(n.item_id, set()).add(n.body_hash)
    issues.extend(f"{i}: {len(h)} texts in one fetch" for i, h in by_id.items() if len(h) > 1)
    return issues
