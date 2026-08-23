"""Run official-source adapters and normalize their output for the engine."""

from __future__ import annotations

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from market_radar.domain import (
    CollectedCalendarEvent,
    CollectedIndicator,
    CollectedRelease,
    CollectedSourceHealth,
    CollectionBundle,
    SourceDescriptor,
)
from market_radar.sources import (
    CalendarEvent,
    Freshness,
    HttpClient,
    IndicatorObservation,
    Release,
    ReleaseKind,
    SourceResult,
    SourceStatus,
    UrllibHttpClient,
)
from market_radar.sources.calendar import bea_calendar_adapter, bls_calendar_adapter
from market_radar.sources.cbrt import CbrtUsdTryAdapter
from market_radar.sources.feeds import cbrt_press_adapter, ecb_press_adapter, fed_press_adapter
from market_radar.sources.fred import FredBroadUsdAdapter
from market_radar.sources.gdelt import GdeltDocAdapter
from market_radar.sources.treasury import TreasuryYieldAdapter


@dataclass(frozen=True)
class CollectionJob:
    job_id: str
    kind: str
    adapter: Any
    source: SourceDescriptor
    market_tags: Tuple[str, ...] = ("global",)
    calendar_keywords: Tuple[str, ...] = ()


_INDICATOR_LABELS = {
    "us-treasury-2y": ("US Treasury 2Y", "percent"),
    "us-treasury-10y": ("US Treasury 10Y", "percent"),
    "us-curve-2s10s": ("US 2s10s curve", "basis-points"),
    "fed-broad-usd": ("Broad USD index (Fed)", "index"),
    "cbrt-usd-try": ("CBRT indicative USD/TRY buying rate", "try-per-usd"),
}


def _source(
    source_id: str, name: str, url: str, license_class: str
) -> SourceDescriptor:
    return SourceDescriptor(source_id, name, url, license_class)


def default_jobs(
    *,
    client: HttpClient,
    fred_api_key: Optional[str],
) -> Tuple[CollectionJob, ...]:
    treasury = TreasuryYieldAdapter(client)
    cbrt_rate = CbrtUsdTryAdapter(client)
    fred = FredBroadUsdAdapter(client, fred_api_key)
    fed_feed = fed_press_adapter(client)
    ecb_feed = ecb_press_adapter(client)
    cbrt_feed = cbrt_press_adapter(client)
    bls = bls_calendar_adapter(client)
    bea = bea_calendar_adapter(client)
    gdelt_global = GdeltDocAdapter(
        client,
        '(inflation OR "central bank" OR "interest rates" OR employment) sourcelang:english',
    )
    gdelt_turkey = GdeltDocAdapter(
        client,
        '(Turkey OR Turkish OR lira OR CBRT) sourcelang:english',
    )

    return (
        CollectionJob(
            "treasury-yields",
            "indicator",
            treasury,
            _source(
                "us-treasury-yields",
                "US Treasury",
                treasury.source_url,
                "us-government-public-domain",
            ),
        ),
        CollectionJob(
            "fred-broad-usd",
            "indicator",
            fred,
            _source(
                "fed-broad-usd",
                "Federal Reserve via FRED",
                "https://fred.stlouisfed.org/series/DTWEXBGS",
                "us-government-public-domain-with-fred-attribution",
            ),
        ),
        CollectionJob(
            "cbrt-usd-try",
            "indicator",
            cbrt_rate,
            _source(
                "cbrt-rates",
                "Central Bank of the Republic of Turkey",
                cbrt_rate.source_url,
                "official-source-attribution-required",
            ),
            ("global", "turkey"),
        ),
        CollectionJob(
            "fed-releases",
            "release",
            fed_feed,
            _source(
                "fed-releases",
                "Federal Reserve Board",
                fed_feed.source_url,
                "us-government-public-domain",
            ),
        ),
        CollectionJob(
            "ecb-releases",
            "release",
            ecb_feed,
            _source(
                "ecb-releases",
                "European Central Bank",
                ecb_feed.source_url,
                "official-reuse-with-attribution",
            ),
        ),
        CollectionJob(
            "cbrt-releases",
            "release",
            cbrt_feed,
            _source(
                "cbrt-releases",
                "Central Bank of the Republic of Turkey",
                cbrt_feed.source_url,
                "official-source-attribution-required",
            ),
            ("global", "turkey"),
        ),
        CollectionJob(
            "bls-calendar",
            "calendar",
            bls,
            _source(
                "bls-calendar",
                "U.S. Bureau of Labor Statistics",
                bls.source_url,
                "us-government-public-domain",
            ),
            calendar_keywords=("consumer price index", "employment situation"),
        ),
        CollectionJob(
            "bea-calendar",
            "calendar",
            bea,
            _source(
                "bea-calendar",
                "U.S. Bureau of Economic Analysis",
                bea.source_url,
                "us-government-public-domain",
            ),
            calendar_keywords=("gross domestic product", "personal income and outlays"),
        ),
        CollectionJob(
            "gdelt-global",
            "release",
            gdelt_global,
            _source(
                "gdelt-global-discovery",
                "GDELT discovery",
                "https://www.gdeltproject.org/",
                "open-dataset-discovery-only",
            ),
        ),
        CollectionJob(
            "gdelt-turkey",
            "release",
            gdelt_turkey,
            _source(
                "gdelt-turkey-discovery",
                "GDELT discovery",
                "https://www.gdeltproject.org/",
                "open-dataset-discovery-only",
            ),
            ("global", "turkey"),
        ),
    )


def collect_sources(
    *,
    at: datetime,
    fred_api_key: Optional[str],
    client: Optional[HttpClient] = None,
    jobs: Optional[Sequence[CollectionJob]] = None,
    max_workers: int = 6,
) -> CollectionBundle:
    if at.tzinfo is None:
        raise ValueError("collection timestamp must be timezone-aware")
    http_client = client or UrllibHttpClient()
    collection_jobs = tuple(jobs or default_jobs(client=http_client, fred_api_key=fred_api_key))
    results = {}
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(collection_jobs)))) as executor:
        futures = {executor.submit(job.adapter.fetch, at): job for job in collection_jobs}
        for future in as_completed(futures):
            job = futures[future]
            try:
                results[job.job_id] = future.result()
            except Exception:
                results[job.job_id] = SourceResult(
                    items=(),
                    retrieved_at=at,
                    source_url=job.source.url,
                    freshness=Freshness.UNKNOWN,
                    status=SourceStatus.ERROR,
                    error_code="ADAPTER_ERROR",
                )

    indicators: List[CollectedIndicator] = []
    releases: List[CollectedRelease] = []
    calendar: List[CollectedCalendarEvent] = []
    health: List[CollectedSourceHealth] = []
    histories = {}

    for job in collection_jobs:
        result = results[job.job_id]
        health.append(
            CollectedSourceHealth(
                source=job.source,
                status=result.status.value,
                retrieved_at=result.retrieved_at,
                item_count=len(result.items),
                error_code=result.error_code,
            )
        )
        if job.kind == "indicator":
            normalized = _normalize_indicators(job, result)
            indicators.extend(normalized)
            for item in normalized:
                histories.setdefault(item.indicator_id, []).append(item)
        elif job.kind == "release":
            releases.extend(_normalize_releases(job, result))
        elif job.kind == "calendar":
            calendar.extend(_normalize_calendar(job, result))
        else:
            raise ValueError("unsupported collection job kind: {}".format(job.kind))

    return CollectionBundle(
        indicators=tuple(indicators),
        releases=tuple(releases),
        calendar=tuple(calendar),
        source_health=tuple(health),
        histories={key: tuple(value) for key, value in histories.items()},
    )


def _normalize_indicators(
    job: CollectionJob, result: SourceResult[IndicatorObservation]
) -> List[CollectedIndicator]:
    output = []
    freshness = _freshness(result)
    for item in result.items:
        metadata = _INDICATOR_LABELS.get(item.indicator_id)
        if metadata is None:
            continue
        label, unit = metadata
        output.append(
            CollectedIndicator(
                indicator_id=item.indicator_id,
                label=label,
                value=item.value,
                unit=unit,
                display_value=_display_value(item.indicator_id, item.value),
                observed_at=item.observed_at,
                retrieved_at=result.retrieved_at,
                freshness=freshness,
                source=job.source,
                market_tags=job.market_tags,
            )
        )
    return output


def _normalize_releases(
    job: CollectionJob, result: SourceResult[Release]
) -> List[CollectedRelease]:
    output = []
    for item in result.items:
        timestamp = item.published_at or item.seen_at
        if (
            timestamp is None
            or urlparse(item.url).scheme != "https"
            or len(item.url) > 500
        ):
            continue
        kind = item.kind.value if isinstance(item.kind, ReleaseKind) else str(item.kind)
        output.append(
            CollectedRelease(
                release_id=_stable_id("release", item.url),
                title=_plain_text(item.title)[:300],
                url=item.url,
                published_at=timestamp,
                retrieved_at=result.retrieved_at,
                source=job.source,
                publisher=_plain_text(item.publisher)[:80],
                kind=kind,
                category=(item.category or "macro")[:80],
                market_tags=job.market_tags,
            )
        )
    return output


def _normalize_calendar(
    job: CollectionJob, result: SourceResult[CalendarEvent]
) -> List[CollectedCalendarEvent]:
    output = []
    for item in result.items:
        normalized_title = item.title.lower()
        if job.calendar_keywords and not any(
            keyword in normalized_title for keyword in job.calendar_keywords
        ):
            continue
        output.append(
            CollectedCalendarEvent(
                event_id=_stable_id("event", "{}|{}".format(job.job_id, item.event_id)),
                name=_plain_text(item.title)[:200],
                scheduled_at=item.scheduled_at,
                authority=item.authority,
                region=item.region,
                source_url=job.source.url,
                checked_at=result.retrieved_at,
                tentative=item.tentative,
                impact="high",
                market_tags=job.market_tags,
            )
        )
    return output


def _display_value(indicator_id: str, value: Decimal) -> str:
    if indicator_id in {"us-treasury-2y", "us-treasury-10y"}:
        return "{:.2f}%".format(value)
    if indicator_id == "us-curve-2s10s":
        return "{:+.0f} bp".format(value)
    if indicator_id == "cbrt-usd-try":
        return "{:.4f}".format(value)
    return "{:.2f}".format(value)


def _freshness(result: SourceResult[Any]) -> str:
    if result.freshness == Freshness.FRESH:
        return "fresh"
    if result.freshness == Freshness.STALE:
        return "stale"
    return "unavailable"


def _stable_id(prefix: str, value: str) -> str:
    return "{}-{}".format(prefix, hashlib.sha256(value.encode("utf-8")).hexdigest()[:16])


def _plain_text(value: str) -> str:
    without_markup = re.sub(r"<[^>]*>", " ", value)
    return " ".join(without_markup.replace("<", " ").replace(">", " ").split())
