"""Assemble the strict v1 public snapshot and its next private checkpoint."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from market_radar.domain import (
    CollectedCalendarEvent,
    CollectedIndicator,
    CollectedRelease,
    CollectionBundle,
    SourceDescriptor,
)
from market_radar.scoring import MacroConditions, latest_indicators, score_macro_conditions
from market_radar.state import RadarState
from market_radar.timeutil import format_utc, parse_utc
from market_radar.validation import validate_snapshot

CORE_INDICATOR_IDS = ("us-treasury-10y", "us-curve-2s10s", "fed-broad-usd")
MAX_DEVELOPMENTS = 8
MAX_STORIES = 12
MAX_CALENDAR_EVENTS = 20
_FRESHNESS_SECONDS = {
    "us-treasury-2y": 604_800,
    "us-treasury-10y": 604_800,
    "us-curve-2s10s": 604_800,
    "fed-broad-usd": 1_209_600,
    "cbrt-usd-try": 604_800,
}
_FALLBACK_RETENTION_SECONDS = 2_592_000
_NEWS_RELEVANCE_TERMS = (
    "central bank",
    "federal reserve",
    "interest rate",
    "monetary policy",
    "policy rate",
    "fomc",
    "inflation",
    "consumer price",
    "consumer expectations",
    "nonfarm payroll",
    "unemployment",
    "treasury yield",
    "bond yield",
    "economic growth",
    "economic outlook",
    "gross domestic product",
    "euro area economy",
    "liquidity management",
    "exchange rate",
    "turkish lira",
    "turkish central bank",
    "cbrt",
    "ecb",
    "gdp",
)
_NEWS_EXCLUSION_TERMS = (
    "announces approval of",
    "enforcement action",
    "former employee",
    "invites the public",
    "concert",
)


@dataclass(frozen=True)
class SnapshotBuildResult:
    snapshot: dict[str, Any]
    next_state: RadarState


def _snapshot_id(generated_at: datetime) -> str:
    return "mr-{}".format(generated_at.astimezone(timezone.utc).strftime("%Y%m%dt%H%M%Sz").lower())


def _slug(value: str, fallback: str = "macro") -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:48]
    if len(normalized) < 2:
        return fallback
    return normalized


def _source_type(source: SourceDescriptor) -> str:
    if source.source_id.startswith("gdelt-"):
        return "public-data"
    return "official"


def _provenance(source: SourceDescriptor, retrieved_at: datetime) -> dict[str, Any]:
    return {
        "sourceId": source.source_id,
        "sourceName": source.name[:80],
        "sourceType": _source_type(source),
        "sourceUrl": source.url,
        "retrievedAt": format_utc(retrieved_at),
    }


def _derive_curve(current: Mapping[str, CollectedIndicator]) -> CollectedIndicator | None:
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
        display_value=f"{value:+.0f} bp",
        observed_at=observed_at,
        retrieved_at=retrieved_at,
        freshness=freshness,
        source=ten_year.source,
        market_tags=("global", "united-states", "rates"),
    )


def _indicator_from_record(
    indicator_id: str,
    record: Mapping[str, object],
    generated_at: datetime,
) -> CollectedIndicator | None:
    try:
        observed_at = parse_utc(str(record["observedAt"]))
        retrieved_at = parse_utc(str(record["retrievedAt"]))
        if (generated_at - observed_at).total_seconds() > _FALLBACK_RETENTION_SECONDS:
            return None
        raw_source = record["source"]
        if not isinstance(raw_source, Mapping):
            return None
        source = SourceDescriptor(
            source_id=str(raw_source["id"]),
            name=str(raw_source["name"]),
            url=str(raw_source["url"]),
            license_class=str(raw_source["licenseClass"]),
        )
        raw_tags = record.get("marketTags", ["global"])
        if not isinstance(raw_tags, list):
            return None
        return CollectedIndicator(
            indicator_id=indicator_id,
            label=str(record["label"]),
            value=Decimal(str(record["value"])),
            unit=str(record["unit"]),
            display_value=str(record["displayValue"]),
            observed_at=observed_at,
            retrieved_at=retrieved_at,
            freshness="stale",
            source=source,
            market_tags=tuple(str(tag) for tag in raw_tags),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _indicator_record(indicator: CollectedIndicator) -> dict[str, Any]:
    return {
        "label": indicator.label,
        "value": float(indicator.value),
        "unit": indicator.unit,
        "displayValue": indicator.display_value,
        "observedAt": format_utc(indicator.observed_at),
        "retrievedAt": format_utc(indicator.retrieved_at),
        "source": indicator.source.public_dict(),
        "marketTags": list(indicator.market_tags),
    }


def _restore_last_good_indicators(
    current: dict[str, CollectedIndicator],
    previous_state: RadarState,
    generated_at: datetime,
) -> None:
    for indicator_id, record in previous_state.indicator_records.items():
        if indicator_id in current:
            continue
        restored = _indicator_from_record(indicator_id, record, generated_at)
        if restored is not None:
            current[indicator_id] = restored


def _indicator_change(
    indicator: CollectedIndicator, previous_values: Mapping[str, float]
) -> dict[str, Any] | None:
    previous = previous_values.get(indicator.indicator_id)
    if previous is None:
        return None
    value = float(indicator.value)
    delta = value - previous
    if abs(delta) < 1e-12:
        direction = "flat"
    elif delta > 0:
        direction = "up"
    else:
        direction = "down"

    if indicator.indicator_id in {"us-treasury-2y", "us-treasury-10y"}:
        display = f"{delta * 100:+.0f} bp"
    elif indicator.indicator_id == "us-curve-2s10s":
        display = f"{delta:+.0f} bp"
    elif indicator.indicator_id == "cbrt-usd-try":
        display = f"{delta:+.4f}"
    else:
        display = f"{delta:+.2f}"
    return {
        "rawValue": round(delta, 6),
        "displayValue": display,
        "period": "previous-observation",
        "direction": direction,
    }


def _public_unit(indicator: CollectedIndicator) -> str:
    if indicator.indicator_id == "cbrt-usd-try":
        return "currency-rate"
    if indicator.unit in {"basis_points", "basis-points"}:
        return "basis-points"
    if indicator.unit.startswith("index"):
        return "index"
    return indicator.unit


def _macro_signal(indicator: CollectedIndicator, conditions: Mapping[str, object]) -> str:
    drivers = conditions.get("drivers", [])
    if isinstance(drivers, list):
        for driver in drivers:
            if isinstance(driver, dict) and driver.get("indicatorId") == indicator.indicator_id:
                direction = driver.get("direction")
                if direction == "restrictive":
                    return "tightening"
                if direction == "supportive":
                    return "easing"
                return "neutral"
    if indicator.indicator_id == "us-treasury-2y":
        if indicator.value >= Decimal("4"):
            return "tightening"
        if indicator.value <= Decimal("2.5"):
            return "easing"
        return "neutral"
    if indicator.indicator_id == "cbrt-usd-try":
        return "mixed"
    return "neutral"


def _indicator_dict(
    indicator: CollectedIndicator,
    previous_values: Mapping[str, float],
    generated_at: datetime,
    conditions: Mapping[str, object],
) -> dict[str, Any]:
    age_seconds = max(0, int((generated_at - indicator.observed_at).total_seconds()))
    age_seconds = min(age_seconds, _FALLBACK_RETENTION_SECONDS)
    max_age = _FRESHNESS_SECONDS.get(indicator.indicator_id, 604_800)
    freshness = "fresh" if indicator.freshness == "fresh" and age_seconds <= max_age else "stale"
    return {
        "id": indicator.indicator_id,
        "label": indicator.label[:80],
        "rawValue": float(indicator.value),
        "displayValue": indicator.display_value[:32],
        "unit": _public_unit(indicator),
        "change": _indicator_change(indicator, previous_values),
        "observedAt": format_utc(indicator.observed_at),
        "retrievedAt": format_utc(indicator.retrieved_at),
        "freshness": {
            "status": freshness,
            "ageSeconds": age_seconds,
            "maxAgeSeconds": max_age,
        },
        "macroSignal": _macro_signal(indicator, conditions),
        "source": _provenance(indicator.source, indicator.retrieved_at),
        "marketTags": list(dict.fromkeys(indicator.market_tags)),
    }


def _category_for(release: CollectedRelease) -> str:
    if release.category and release.category != "macro":
        return _slug(release.category)
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
        return (
            "Labor-market momentum affects the balance between inflation risk and growth support."
        )
    if category == "central-bank":
        if "turkey" in market_tags:
            return "CBRT guidance affects domestic rate expectations and the lira risk premium."
        return "Official guidance can reprice the expected timing and pace of policy changes."
    if category == "energy":
        return "Energy supply changes can move headline inflation and growth expectations together."
    if category == "growth":
        return "Growth momentum changes the policy trade-off and demand outlook."
    return "This development may alter the official macro inputs that currently drive the radar."


def _impact(category: str, kind: str) -> str:
    if category in {"inflation", "central-bank", "labor"} and kind == "official":
        return "high"
    return "medium"


def _development_dict(release: CollectedRelease) -> dict[str, Any]:
    category = _category_for(release)
    return {
        "id": release.release_id,
        "headline": release.title[:240],
        "summary": _why_it_matters(category, release.market_tags),
        "impact": _impact(category, release.kind),
        "firstSeenAt": format_utc(release.published_at),
        "updatedAt": format_utc(release.retrieved_at),
        "provenance": [_provenance(release.source, release.retrieved_at)],
        "marketTags": list(dict.fromkeys(release.market_tags)),
    }


def _story_dict(release: CollectedRelease) -> dict[str, Any]:
    category = _category_for(release)
    if release.kind == "discovery":
        summary = (
            "GDELT surfaced this title as discovery metadata; Market Radar has not independently "
            "confirmed the underlying report."
        )
    else:
        summary = (
            "Official release metadata is retained with its source link; "
            "Market Radar does not copy the full release text."
        )
    return {
        "id": release.release_id,
        "headline": release.title[:240],
        "summary": summary,
        "whyItMatters": _why_it_matters(category, release.market_tags),
        "url": release.url,
        "publisher": (release.publisher or release.source.name)[:80],
        "publishedAt": format_utc(release.published_at),
        "retrievedAt": format_utc(release.retrieved_at),
        "impact": _impact(category, release.kind),
        "category": category,
        "provenance": [_provenance(release.source, release.retrieved_at)],
        "marketTags": list(dict.fromkeys(release.market_tags)),
    }


def _calendar_category(name: str) -> str:
    lowered = name.lower()
    if "consumer price" in lowered or "inflation" in lowered:
        return "inflation"
    if "employment" in lowered or "jobs" in lowered:
        return "labor"
    if "gross domestic" in lowered or "personal income" in lowered:
        return "growth"
    return "macro"


def _calendar_timezone(region: str) -> str:
    if region == "US":
        return "America/New_York"
    if region in {"TR", "Turkey", "Türkiye"}:
        return "Europe/Istanbul"
    return "Europe/Brussels"


def _country_code(region: str) -> str:
    mapping = {"US": "US", "TR": "TR", "Turkey": "TR", "Türkiye": "TR", "EU": "EU"}
    return mapping.get(region, "EU")


def _calendar_dict(event: CollectedCalendarEvent) -> dict[str, Any]:
    category = _calendar_category(event.name)
    tags = list(dict.fromkeys((*event.market_tags, category)))
    source = SourceDescriptor(
        source_id=_slug(event.authority, "official-calendar"),
        name=event.authority,
        url=event.source_url,
        license_class="official-calendar",
    )
    provenance = _provenance(source, event.checked_at)
    provenance["sourceType"] = "official"
    return {
        "id": event.event_id,
        "title": event.name[:240],
        "countryCode": _country_code(event.region),
        "scheduledAt": format_utc(event.scheduled_at),
        "timezone": _calendar_timezone(event.region),
        "status": "scheduled",
        "impact": event.impact,
        "previous": None,
        "forecast": None,
        "actual": None,
        "unit": None,
        "marketTags": tags,
        "source": provenance,
    }


def _dedupe_releases(releases: Iterable[CollectedRelease]) -> list[CollectedRelease]:
    by_url: dict[str, CollectedRelease] = {}
    for release in releases:
        existing = by_url.get(release.url)
        if existing is None or release.published_at > existing.published_at:
            by_url[release.url] = release
    return sorted(by_url.values(), key=lambda item: item.published_at, reverse=True)


def _release_relevance(release: CollectedRelease) -> int:
    title = release.title.lower()
    if any(term in title for term in _NEWS_EXCLUSION_TERMS):
        return 0
    matches = sum(
        1
        for term in _NEWS_RELEVANCE_TERMS
        if (re.search(rf"\b{re.escape(term)}\b", title) is not None)
    )
    if not matches:
        return 0
    return matches + (100 if release.kind == "official" else 0)


def _release_rank(release: CollectedRelease) -> tuple[int, int, datetime]:
    return (
        1 if release.kind == "official" else 0,
        _release_relevance(release),
        release.published_at,
    )


def _source_health_status(status: str) -> str:
    normalized = status.lower()
    if normalized in {"ok", "healthy", "fresh"}:
        return "healthy"
    if normalized in {"degraded", "stale"}:
        return "stale"
    return "unavailable"


def _source_market_tags(source_id: str) -> list[str]:
    if "cbrt" in source_id or "turkey" in source_id:
        return ["turkey"]
    if "treasury" in source_id or "fred" in source_id or "fed-" in source_id:
        return ["global", "united-states"]
    if "ecb" in source_id:
        return ["global", "euro-area"]
    return ["global"]


def _source_payload(bundle: CollectionBundle) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for health in sorted(bundle.source_health, key=lambda item: item.source.source_id):
        status = _source_health_status(health.status)
        if status == "healthy":
            message = "Latest scheduled retrieval succeeded."
        elif status == "stale":
            message = "Source returned partial or stale data; observation times remain explicit."
        else:
            message = "Source was unavailable in this run; internal error details are not public."
        output.append(
            {
                "id": health.source.source_id,
                "name": health.source.name[:80],
                "type": _source_type(health.source),
                "url": health.source.url,
                "status": status,
                "lastAttemptAt": format_utc(health.retrieved_at),
                "lastSuccessAt": format_utc(health.retrieved_at) if health.item_count else None,
                "itemsUsed": health.item_count,
                "marketTags": _source_market_tags(health.source.source_id),
                "publicMessage": message,
            }
        )
    return output


def _all_market_tags(
    current: Mapping[str, CollectedIndicator],
    releases: Sequence[CollectedRelease],
    calendar: Sequence[CollectedCalendarEvent],
) -> list[str]:
    tags = ["global"]
    for indicator in current.values():
        tags.extend(indicator.market_tags)
    for release in releases:
        tags.extend(release.market_tags)
    for event in calendar:
        tags.extend(event.market_tags)
    return list(dict.fromkeys(_slug(tag) for tag in tags))[:12]


def _digest_dict(
    conditions: MacroConditions,
    conditions_payload: Mapping[str, object],
    stories: Sequence[Mapping[str, Any]],
    calendar: Sequence[Mapping[str, Any]],
    generated_at: datetime,
    market_tags: Sequence[str],
) -> dict[str, Any]:
    drivers = conditions_payload["drivers"]
    assert isinstance(drivers, list)
    highlights: list[dict[str, Any]] = []
    for index, driver in enumerate(
        sorted(drivers, key=lambda item: abs(float(item["contributionPoints"])), reverse=True)[:3]
    ):
        contribution = float(driver["contributionPoints"])
        highlights.append(
            {
                "id": "highlight-{}-{}".format(index + 1, _slug(str(driver["indicatorId"]))),
                "text": str(driver["explanation"]),
                "impact": "high" if abs(contribution) >= 10 else "medium",
                "relatedStoryIds": [],
            }
        )
    public_label = str(conditions_payload["label"])
    return {
        "id": "digest-{}".format(generated_at.strftime("%Y%m%dt%H%M%Sz").lower()),
        "periodStart": format_utc(generated_at - timedelta(hours=24)),
        "periodEnd": format_utc(generated_at),
        "generatedAt": format_utc(generated_at),
        "title": f"Macro conditions are {public_label}",
        "summary": conditions.summary,
        "highlights": highlights,
        "storyIds": [story["id"] for story in stories],
        "itemCount": len(stories),
        "marketTags": list(market_tags),
        "commentary": _daily_commentary(conditions_payload, stories, calendar),
    }


def _commentary_section(
    headline: str,
    body: str,
    evidence_ids: Sequence[str],
) -> dict[str, Any]:
    return {
        "headline": headline[:240],
        "body": body[:800],
        "evidenceIds": list(dict.fromkeys(evidence_ids))[:12],
    }


def _daily_commentary(
    conditions: Mapping[str, Any],
    stories: Sequence[Mapping[str, Any]],
    calendar: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Turn already-published facts into a bounded daily reading note.

    This is deliberately deterministic. It comments only on the score, driver,
    story, and calendar records that will ship in the same public snapshot.
    """

    raw_drivers = conditions.get("drivers", [])
    drivers = [item for item in raw_drivers if isinstance(item, Mapping)]
    pressure = sorted(
        (item for item in drivers if float(item.get("contributionPoints", 0)) > 0),
        key=lambda item: float(item["contributionPoints"]),
        reverse=True,
    )
    offsets = sorted(
        (item for item in drivers if float(item.get("contributionPoints", 0)) < 0),
        key=lambda item: float(item["contributionPoints"]),
    )
    score = float(conditions["score"])
    label = str(conditions["label"])
    data_evidence: list[str] = []
    data_sentences = [f"The macro-conditions score is {score:.1f}/100, a {label} reading."]
    if pressure:
        lead = pressure[0]
        data_evidence.append(str(lead["indicatorId"]))
        data_sentences.append(
            "{} is the largest tightening input at {:+.1f} points.".format(
                lead["label"], float(lead["contributionPoints"])
            )
        )
    if offsets:
        lead_offset = offsets[0]
        data_evidence.append(str(lead_offset["indicatorId"]))
        data_sentences.append(
            "{} offsets part of that pressure at {:+.1f} points.".format(
                lead_offset["label"], float(lead_offset["contributionPoints"])
            )
        )
    if not pressure and not offsets and drivers:
        data_evidence.append(str(drivers[0]["indicatorId"]))
        data_sentences.append("The scored inputs are clustered close to their neutral bands.")
    data_sentences.append(
        "Read the result as a description of current conditions, not a directional market call."
    )
    data_lead = pressure[0]["label"] if pressure else drivers[0]["label"]
    data_headline = f"{data_lead} sets the tone in a {label} backdrop"

    official_stories = [
        story
        for story in stories
        if any(
            provenance.get("sourceType") == "official"
            for provenance in story.get("provenance", [])
            if isinstance(provenance, Mapping)
        )
    ]
    discovery_stories = [story for story in stories if story not in official_stories]
    if official_stories:
        lead_story = official_stories[0]
        news_headline = str(lead_story["headline"])
        news_body = (
            f"{len(official_stories)} new official release"
            f"{'s' if len(official_stories) != 1 else ''} entered the 24-hour brief. "
            f"The lead item is '{lead_story['headline']}'. "
            f"{lead_story['whyItMatters']}"
        )
        if discovery_stories:
            news_body += (
                f" {len(discovery_stories)} additional discovery headline"
                f"{'s are' if len(discovery_stories) != 1 else ' is'} retained as "
                "unconfirmed context."
            )
    elif discovery_stories:
        lead_story = discovery_stories[0]
        news_headline = "Discovery headlines add context, not confirmation"
        news_body = (
            f"{len(discovery_stories)} new discovery headline"
            f"{'s were' if len(discovery_stories) != 1 else ' was'} found. "
            f"The lead title is '{lead_story['headline']}', but Market Radar has not "
            "independently confirmed the underlying report."
        )
    else:
        news_headline = "No new attributable release changed the brief"
        news_body = (
            "The current run added no new official release or discovery headline. "
            "That is an absence of new sourced material, not evidence that the news "
            "environment is quiet."
        )

    news_evidence = [str(story["id"]) for story in (*official_stories, *discovery_stories)[:4]]

    if calendar:
        next_event = calendar[0]
        watch_headline = str(next_event["title"])
        watch_body = (
            f"The next published high-impact event is {next_event['title']}. "
            "Use the official calendar time shown below; the key question is whether the release "
            "reinforces or offsets the radar's largest scored driver."
        )
        watch_evidence = [str(next_event["id"]), *data_evidence[:1]]
    else:
        watch_headline = "No official event is currently inside the watch window"
        watch_body = (
            "The verified calendar contains no upcoming event in the current window. "
            "Market Radar will add one only after an official calendar source publishes it."
        )
        watch_evidence = data_evidence[:1]

    return {
        "generation": {
            "mode": "deterministic",
            "method": "daily-commentary-v1",
        },
        "dataRead": _commentary_section(
            data_headline,
            " ".join(data_sentences),
            data_evidence,
        ),
        "newsRead": _commentary_section(news_headline, news_body, news_evidence),
        "watchNext": _commentary_section(watch_headline, watch_body, watch_evidence),
    }


def build_snapshot(
    bundle: CollectionBundle,
    previous_state: RadarState,
    *,
    generated_at: datetime,
    started_at: datetime | None = None,
    run_id: str | None = None,
    valid_for: timedelta = timedelta(hours=8, minutes=30),
    successful_slot: str | None = None,
) -> SnapshotBuildResult:
    if generated_at.tzinfo is None:
        raise ValueError("generated_at must be timezone-aware")
    generated_at = generated_at.astimezone(timezone.utc).replace(microsecond=0)
    started_at = (started_at or generated_at).astimezone(timezone.utc).replace(microsecond=0)
    if started_at > generated_at:
        raise ValueError("started_at must not be after generated_at")
    run_id = run_id or "run-{}".format(generated_at.strftime("%Y%m%dt%H%M%Sz").lower())

    current = latest_indicators(bundle.indicators)
    _restore_last_good_indicators(current, previous_state, generated_at)
    derived_curve = _derive_curve(current)
    if derived_curve is not None:
        current[derived_curve.indicator_id] = derived_curve

    histories = dict(bundle.histories)
    for indicator_id, indicator in current.items():
        existing = list(histories.get(indicator_id, ()))
        if not any(item.observed_at == indicator.observed_at for item in existing):
            existing.append(indicator)
        histories[indicator_id] = tuple(existing)

    available_core = sum(1 for indicator_id in CORE_INDICATOR_IDS if indicator_id in current)
    if available_core < 2:
        raise ValueError("publication requires at least two core macro indicators")

    conditions = score_macro_conditions(current, histories)
    conditions_payload = conditions.public_dict()
    releases = _dedupe_releases(bundle.releases)
    new_releases = sorted(
        (
            release
            for release in releases
            if release.url not in previous_state.seen_release_urls
            and generated_at - timedelta(hours=24)
            <= release.published_at
            <= generated_at + timedelta(minutes=5)
            and _release_relevance(release) > 0
        ),
        key=_release_rank,
        reverse=True,
    )
    priority_releases = [release for release in new_releases if release.kind == "official"][
        :MAX_DEVELOPMENTS
    ]
    story_releases = new_releases[:MAX_STORIES]

    calendar_end = generated_at + timedelta(days=21)
    upcoming_events = sorted(
        (
            event
            for event in bundle.calendar
            if generated_at - timedelta(hours=1) <= event.scheduled_at <= calendar_end
        ),
        key=lambda item: item.scheduled_at,
    )[:MAX_CALENDAR_EVENTS]

    source_payload = _source_payload(bundle)
    successful_sources = sum(1 for source in source_payload if source["status"] == "healthy")
    stale_sources = sum(1 for source in source_payload if source["status"] == "stale")
    failed_sources = sum(1 for source in source_payload if source["status"] == "unavailable")
    pipeline_status = (
        "healthy"
        if available_core == len(CORE_INDICATOR_IDS) and stale_sources == 0 and failed_sources == 0
        else "degraded"
    )
    market_tags = _all_market_tags(current, releases, upcoming_events)
    snapshot_id = _snapshot_id(generated_at)
    story_payload = [_story_dict(release) for release in story_releases]
    calendar_payload = [_calendar_dict(event) for event in upcoming_events]

    snapshot = {
        "schemaVersion": 1,
        "id": snapshot_id,
        "generatedAt": format_utc(generated_at),
        "validUntil": format_utc(generated_at + valid_for),
        "pipeline": {
            "runId": run_id,
            "status": pipeline_status,
            "startedAt": format_utc(started_at),
            "completedAt": format_utc(generated_at),
            "coverage": {
                "expectedSources": len(source_payload),
                "successfulSources": successful_sources,
                "staleSources": stale_sources,
                "failedSources": failed_sources,
                "marketTags": market_tags,
            },
            **(
                {
                    "publicNote": (
                        "One or more sources are delayed or unavailable; observation and retrieval "
                        "timestamps remain explicit."
                    )
                }
                if pipeline_status == "degraded"
                else {}
            ),
        },
        "macroConditions": conditions_payload,
        "indicators": [
            _indicator_dict(
                indicator, previous_state.indicator_values, generated_at, conditions_payload
            )
            for indicator in sorted(current.values(), key=lambda item: item.indicator_id)
        ],
        "priorityDevelopments": [_development_dict(release) for release in priority_releases],
        "stories": story_payload,
        "calendar": calendar_payload,
        "digest": _digest_dict(
            conditions,
            conditions_payload,
            story_payload,
            calendar_payload,
            generated_at,
            market_tags,
        ),
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
        indicator_records={
            indicator_id: _indicator_record(indicator)
            for indicator_id, indicator in current.items()
        },
        seen_release_urls=next_urls,
        successful_slots=next_slots,
    )
    return SnapshotBuildResult(snapshot=snapshot, next_state=next_state)
