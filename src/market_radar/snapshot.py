"""Assemble a validated, public-safe Market Radar snapshot."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from market_radar.domain import (
    CollectedCalendarEvent,
    CollectedIndicator,
    CollectedRelease,
    CollectionBundle,
)
from market_radar.scoring import latest_indicators, score_macro_conditions
from market_radar.state import RadarState
from market_radar.timeutil import format_utc
from market_radar.validation import validate_snapshot


CORE_INDICATOR_IDS = ("us-treasury-10y", "us-curve-2s10s", "fed-broad-usd")
MAX_DEVELOPMENTS = 8
MAX_STORIES = 12
MAX_CALENDAR_EVENTS = 20


@dataclass(frozen=True)
class SnapshotBuildResult:
    snapshot: dict
    next_state: RadarState


def _snapshot_id(generated_at: datetime) -> str:
    return "mr-{}".format(generated_at.astimezone(timezone.utc).strftime("%Y%m%dt%H%M%Sz").lower())


def _stable_id(prefix: str, value: str) -> str:
    return "{}-{}".format(prefix, hashlib.sha256(value.encode("utf-8")).hexdigest()[:16])


def _derive_curve(
    current: Mapping[str, CollectedIndicator],
) -> Optional[CollectedIndicator]:
    if "us-curve-2s10s" in current:
        return None
    two_year = current.get("us-treasury-2y")
    ten_year = current.get("us-treasury-10y")
    if two_year is None or ten_year is None:
        return None
    observed_at = min(two_year.observed_at, ten_year.observed_at)
    retrieved_at = max(two_year.retrieved_at, ten_year.retrieved_at)
    value = (ten_year.value - two_year.value) * Decimal("100")
    freshness = "fresh" if two_year.freshness == ten_year.freshness == "fresh" else "stale"
    return CollectedIndicator(
        indicator_id="us-curve-2s10s",
        label="US 2s10s curve",
        value=value,
        unit="basis-points",
        display_value="{:+.0f} bp".format(value),
        observed_at=observed_at,
        retrieved_at=retrieved_at,
        freshness=freshness,
        source=ten_year.source,
        market_tags=("global",),
    )


def _indicator_change(
    indicator: CollectedIndicator, previous_values: Mapping[str, float]
) -> Optional[dict]:
    previous = previous_values.get(indicator.indicator_id)
    if previous is None:
        return None
    value = float(indicator.value)
    delta = value - previous
    delta_percent = None if previous == 0 else delta / abs(previous) * 100.0
    if abs(delta) < 1e-12:
        direction = "unchanged"
    elif delta > 0:
        direction = "up"
    else:
        direction = "down"
    return {
        "previousValue": previous,
        "delta": round(delta, 6),
        "deltaPercent": None if delta_percent is None else round(delta_percent, 4),
        "direction": direction,
    }


def _indicator_dict(
    indicator: CollectedIndicator, previous_values: Mapping[str, float]
) -> dict:
    return {
        "id": indicator.indicator_id,
        "label": indicator.label,
        "value": float(indicator.value),
        "unit": indicator.unit,
        "displayValue": indicator.display_value,
        "observedAt": format_utc(indicator.observed_at),
        "retrievedAt": format_utc(indicator.retrieved_at),
        "freshness": indicator.freshness,
        "change": _indicator_change(indicator, previous_values),
        "source": indicator.source.public_dict(),
        "marketTags": list(indicator.market_tags),
    }


def _category_for(release: CollectedRelease) -> str:
    if release.category and release.category != "macro":
        return release.category
    title = release.title.lower()
    keyword_categories = (
        (("inflation", "consumer price", "cpi", "pce"), "inflation"),
        (("employment", "jobs", "labor", "payroll"), "labor"),
        (("rate", "monetary policy", "fomc", "governing council"), "central-bank"),
        (("energy", "oil", "gas"), "energy"),
        (("gdp", "growth", "activity"), "growth"),
    )
    for keywords, category in keyword_categories:
        if any(keyword in title for keyword in keywords):
            return category
    return "macro"


def _why_it_matters(category: str, market_tags: Sequence[str]) -> str:
    if category == "inflation":
        return "Inflation composition can change the expected path of policy rates and real yields."
    if category == "labor":
        return "Labor-market momentum affects the balance between inflation risk and growth support."
    if category == "central-bank":
        if "turkey" in market_tags:
            return "CBRT guidance affects domestic rate expectations and the lira risk premium."
        return "Official guidance can reprice the expected timing and pace of policy changes."
    if category == "energy":
        return "Energy supply changes can move headline inflation and growth expectations together."
    if category == "growth":
        return "Growth momentum changes the policy trade-off and demand outlook."
    return "This development may alter the macro inputs that currently drive the radar."


def _impact(category: str, kind: str) -> str:
    if category in {"inflation", "central-bank", "labor"} and kind == "official":
        return "high"
    return "medium"


def _release_dict(release: CollectedRelease) -> dict:
    category = _category_for(release)
    return {
        "id": release.release_id or _stable_id("release", release.url),
        "headline": release.title,
        "publishedAt": format_utc(release.published_at),
        "retrievedAt": format_utc(release.retrieved_at),
        "kind": release.kind,
        "category": category,
        "impact": _impact(category, release.kind),
        "whyItMatters": _why_it_matters(category, release.market_tags),
        "url": release.url,
        "source": release.source.public_dict(),
        "marketTags": list(release.market_tags),
    }


def _calendar_dict(event: CollectedCalendarEvent) -> dict:
    return {
        "id": event.event_id,
        "name": event.name,
        "scheduledAt": format_utc(event.scheduled_at),
        "authority": event.authority,
        "region": event.region,
        "impact": event.impact,
        "tentative": event.tentative,
        "sourceUrl": event.source_url,
        "checkedAt": format_utc(event.checked_at),
        "marketTags": list(event.market_tags),
    }


def _dedupe_releases(releases: Iterable[CollectedRelease]) -> List[CollectedRelease]:
    by_url: Dict[str, CollectedRelease] = {}
    for release in releases:
        existing = by_url.get(release.url)
        if existing is None or release.published_at > existing.published_at:
            by_url[release.url] = release
    return sorted(by_url.values(), key=lambda item: item.published_at, reverse=True)


def _source_status(status: str) -> str:
    normalized = status.lower()
    if normalized in {"ok", "healthy", "fresh"}:
        return "fresh"
    if normalized in {"degraded", "stale"}:
        return "degraded"
    return "unavailable"


def build_snapshot(
    bundle: CollectionBundle,
    previous_state: RadarState,
    *,
    generated_at: datetime,
    valid_for: timedelta = timedelta(hours=8, minutes=30),
    successful_slot: Optional[str] = None,
) -> SnapshotBuildResult:
    if generated_at.tzinfo is None:
        raise ValueError("generated_at must be timezone-aware")
    generated_at = generated_at.astimezone(timezone.utc).replace(microsecond=0)

    current = latest_indicators(bundle.indicators)
    derived_curve = _derive_curve(current)
    all_indicators = list(bundle.indicators)
    if derived_curve is not None:
        all_indicators.append(derived_curve)
        current[derived_curve.indicator_id] = derived_curve

    histories = dict(bundle.histories)
    for indicator_id, indicator in current.items():
        existing = list(histories.get(indicator_id, ()))
        if not any(item.observed_at == indicator.observed_at for item in existing):
            existing.append(indicator)
        histories[indicator_id] = tuple(existing)

    available_core = sum(
        1
        for indicator_id in CORE_INDICATOR_IDS
        if indicator_id in current and current[indicator_id].freshness != "unavailable"
    )
    if available_core < 2:
        raise ValueError("publication requires at least two core macro indicators")

    conditions = score_macro_conditions(current, histories)
    releases = _dedupe_releases(bundle.releases)
    new_releases = [
        release for release in releases if release.url not in previous_state.seen_release_urls
    ]
    priority = [release for release in new_releases if release.kind == "official"][
        :MAX_DEVELOPMENTS
    ]
    stories = new_releases[:MAX_STORIES]

    calendar_end = generated_at + timedelta(days=21)
    upcoming_events = sorted(
        (
            event
            for event in bundle.calendar
            if generated_at - timedelta(hours=1) <= event.scheduled_at <= calendar_end
        ),
        key=lambda item: item.scheduled_at,
    )[:MAX_CALENDAR_EVENTS]

    degraded_sources = sum(
        1 for health in bundle.source_health if _source_status(health.status) != "fresh"
    )
    pipeline_status = (
        "healthy" if available_core == len(CORE_INDICATOR_IDS) and degraded_sources == 0 else "degraded"
    )
    snapshot_id = _snapshot_id(generated_at)

    indicator_payload = [
        _indicator_dict(indicator, previous_state.indicator_values)
        for indicator in sorted(current.values(), key=lambda item: item.indicator_id)
    ]
    source_payload = [
        {
            **health.source.public_dict(),
            "status": _source_status(health.status),
            "retrievedAt": format_utc(health.retrieved_at),
            "itemCount": health.item_count,
            "errorCode": health.error_code,
        }
        for health in sorted(bundle.source_health, key=lambda item: item.source.source_id)
    ]

    watch = [driver.label for driver in sorted(conditions.drivers, key=lambda item: item.score, reverse=True)[:2]]
    if upcoming_events:
        watch.append(upcoming_events[0].name)

    snapshot = {
        "schemaVersion": 1,
        "id": snapshot_id,
        "generatedAt": format_utc(generated_at),
        "validUntil": format_utc(generated_at + valid_for),
        "pipeline": {
            "status": pipeline_status,
            "summary": (
                "All required sources are current."
                if pipeline_status == "healthy"
                else "The last valid data is retained where a source is delayed or unavailable."
            ),
            "coverage": {
                "coreAvailable": available_core,
                "coreRequired": 2,
                "coreTotal": len(CORE_INDICATOR_IDS),
                "sourcesAvailable": len(bundle.source_health) - degraded_sources,
                "sourcesTotal": len(bundle.source_health),
            },
        },
        "macroConditions": conditions.public_dict(),
        "indicators": indicator_payload,
        "priorityDevelopments": [_release_dict(release) for release in priority],
        "stories": [_release_dict(release) for release in stories],
        "calendar": [_calendar_dict(event) for event in upcoming_events],
        "digest": {
            "summary": conditions.summary,
            "watch": watch,
        },
        "sources": source_payload,
    }

    validate_snapshot(snapshot)
    next_urls = tuple(
        dict.fromkeys((*previous_state.seen_release_urls, *(release.url for release in releases)))
    )[-2000:]
    next_slots = previous_state.successful_slots
    if successful_slot and successful_slot not in next_slots:
        next_slots = (*next_slots, successful_slot)[-500:]
    next_state = RadarState(
        previous_snapshot_id=snapshot_id,
        indicator_values={
            indicator_id: float(indicator.value) for indicator_id, indicator in current.items()
        },
        seen_release_urls=next_urls,
        successful_slots=next_slots,
    )
    return SnapshotBuildResult(snapshot=snapshot, next_state=next_state)
