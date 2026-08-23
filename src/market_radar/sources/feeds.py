"""Generic RSS/Atom metadata adapter for official central-bank feeds."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Dict, Iterable, Optional
from urllib.parse import urljoin, urlparse

from .base import (
    HttpClient,
    Release,
    ReleaseKind,
    SourceResult,
    UTC,
    error_result,
    normalize_retrieved_at,
    retrieve,
    success_result,
)


FED_PRESS_URL = "https://www.federalreserve.gov/feeds/press_all.xml"
ECB_PRESS_URL = "https://www.ecb.europa.eu/rss/press.html"
CBRT_PRESS_URL = (
    "https://www.tcmb.gov.tr/wps/wcm/connect/EN/TCMB%2BEN/Bottom%2BMenu/"
    "Other/RSS/Press%2BReleases"
)


class FeedAdapter:
    """Parse titles, canonical links, and publication times only."""

    def __init__(
        self,
        client: HttpClient,
        source_url: str,
        publisher: str,
        category: Optional[str] = None,
    ) -> None:
        self.client = client
        self.source_url = source_url
        self.publisher = publisher
        self.category = category

    def fetch(self, retrieved_at: Optional[datetime] = None) -> SourceResult[Release]:
        retrieved = normalize_retrieved_at(retrieved_at)
        body, request_error = retrieve(
            self.client,
            self.source_url,
            headers={"Accept": "application/atom+xml, application/rss+xml, text/xml"},
        )
        if request_error is not None or body is None:
            return error_result(self.source_url, retrieved, request_error or "NETWORK_ERROR")
        try:
            return self.parse(body, retrieved)
        except Exception:
            return error_result(self.source_url, retrieved, "PARSE_ERROR")

    def parse(self, body: bytes, retrieved_at: datetime) -> SourceResult[Release]:
        root = ET.fromstring(body)
        entries = [
            element
            for element in root.iter()
            if _local(element.tag) in {"item", "entry"}
        ]
        releases: Dict[str, Release] = {}
        partial = False

        for entry in entries:
            title = _first_text(entry, ("title",))
            url = _entry_url(entry, self.source_url)
            published_text = _first_text(
                entry, ("pubDate", "published", "updated", "date")
            )
            if not title or not url or not published_text:
                partial = True
                continue
            try:
                published_at = _parse_feed_datetime(published_text)
            except ValueError:
                partial = True
                continue

            feed_category = _entry_category(entry) or self.category
            release = Release(
                title=_clean_text(title),
                url=url,
                publisher=self.publisher,
                published_at=published_at,
                category=_clean_text(feed_category) if feed_category else None,
                kind=ReleaseKind.OFFICIAL,
                domain=urlparse(url).netloc.lower() or None,
            )
            existing = releases.get(url)
            existing_time = (
                existing.published_at
                if existing is not None and existing.published_at is not None
                else datetime.min.replace(tzinfo=UTC)
            )
            if existing is None or existing_time < published_at:
                releases[url] = release

        ordered = tuple(
            sorted(
                releases.values(),
                key=lambda release: release.published_at or datetime.min.replace(tzinfo=UTC),
                reverse=True,
            )
        )
        return success_result(
            ordered,
            self.source_url,
            retrieved_at,
            degraded_code="PARTIAL_DATA" if partial and ordered else None,
        )


def fed_press_adapter(client: HttpClient) -> FeedAdapter:
    return FeedAdapter(client, FED_PRESS_URL, "Federal Reserve Board")


def ecb_press_adapter(client: HttpClient) -> FeedAdapter:
    return FeedAdapter(client, ECB_PRESS_URL, "European Central Bank")


def cbrt_press_adapter(client: HttpClient) -> FeedAdapter:
    return FeedAdapter(client, CBRT_PRESS_URL, "Central Bank of the Republic of Turkey")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _first_text(parent: ET.Element, local_names: Iterable[str]) -> Optional[str]:
    wanted = set(local_names)
    for element in parent.iter():
        if element is parent:
            continue
        if _local(element.tag) in wanted and element.text:
            value = element.text.strip()
            if value:
                return value
    return None


def _entry_url(entry: ET.Element, base_url: str) -> Optional[str]:
    fallback = None
    for element in entry.iter():
        local = _local(element.tag)
        if local == "link":
            candidate = element.attrib.get("href") or (element.text or "").strip()
            if not candidate:
                continue
            if element.attrib.get("rel", "alternate") == "alternate":
                return _safe_absolute_url(candidate, base_url)
            fallback = fallback or candidate
        elif local == "guid" and element.attrib.get("isPermaLink", "true").lower() == "true":
            fallback = fallback or (element.text or "").strip()
    return _safe_absolute_url(fallback, base_url) if fallback else None


def _entry_category(entry: ET.Element) -> Optional[str]:
    for element in entry.iter():
        if _local(element.tag) != "category":
            continue
        candidate = (element.text or "").strip() or element.attrib.get("term", "").strip()
        if candidate:
            return candidate
    return None


def _safe_absolute_url(value: str, base_url: str) -> Optional[str]:
    absolute = urljoin(base_url, value.strip())
    parsed = urlparse(absolute)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return parsed._replace(fragment="").geturl()


def _parse_feed_datetime(value: str) -> datetime:
    text = value.strip()
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as error:
            raise ValueError("unsupported feed time") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _clean_text(value: str) -> str:
    return " ".join(value.split())
