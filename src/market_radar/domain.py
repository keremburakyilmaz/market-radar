"""Normalized domain objects between source adapters and public snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Mapping, Optional, Sequence, Tuple


@dataclass(frozen=True)
class SourceDescriptor:
    source_id: str
    name: str
    url: str
    license_class: str

    def public_dict(self) -> dict:
        return {
            "id": self.source_id,
            "name": self.name,
            "url": self.url,
            "licenseClass": self.license_class,
        }


@dataclass(frozen=True)
class CollectedIndicator:
    indicator_id: str
    label: str
    value: Decimal
    unit: str
    display_value: str
    observed_at: datetime
    retrieved_at: datetime
    freshness: str
    source: SourceDescriptor
    market_tags: Tuple[str, ...] = ("global",)


@dataclass(frozen=True)
class CollectedRelease:
    release_id: str
    title: str
    url: str
    published_at: datetime
    retrieved_at: datetime
    source: SourceDescriptor
    kind: str = "official"
    category: str = "macro"
    market_tags: Tuple[str, ...] = ("global",)


@dataclass(frozen=True)
class CollectedCalendarEvent:
    event_id: str
    name: str
    scheduled_at: datetime
    authority: str
    region: str
    source_url: str
    checked_at: datetime
    tentative: bool = False
    impact: str = "high"
    market_tags: Tuple[str, ...] = ("global",)


@dataclass(frozen=True)
class CollectedSourceHealth:
    source: SourceDescriptor
    status: str
    retrieved_at: datetime
    item_count: int
    error_code: Optional[str] = None


@dataclass(frozen=True)
class CollectionBundle:
    indicators: Tuple[CollectedIndicator, ...]
    releases: Tuple[CollectedRelease, ...]
    calendar: Tuple[CollectedCalendarEvent, ...]
    source_health: Tuple[CollectedSourceHealth, ...]
    histories: Mapping[str, Sequence[CollectedIndicator]] = field(default_factory=dict)

