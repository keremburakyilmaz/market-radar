"""GDELT DOC 2.0 discovery-metadata adapter."""

from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlparse

from .base import (
    UTC,
    HttpClient,
    Release,
    ReleaseKind,
    SourceResult,
    error_result,
    normalize_retrieved_at,
    retrieve,
    success_result,
)

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
_TRACKING_PARAMETERS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


class GdeltDocAdapter:
    """Discover article links without downloading or storing article content."""

    def __init__(
        self,
        client: HttpClient,
        query: str,
        *,
        timespan: str = "6h",
        max_records: int = 100,
    ) -> None:
        if not query.strip():
            raise ValueError("query is required")
        if not 1 <= max_records <= 250:
            raise ValueError("max_records must be between 1 and 250")
        self.client = client
        self.query = query.strip()
        self.timespan = timespan
        self.max_records = max_records

    @property
    def source_url(self) -> str:
        query = urlencode(
            {
                "query": self.query,
                "mode": "artlist",
                "format": "json",
                "sort": "datedesc",
                "maxrecords": str(self.max_records),
                "timespan": self.timespan,
            }
        )
        return f"{GDELT_DOC_URL}?{query}"

    def fetch(self, retrieved_at: datetime | None = None) -> SourceResult[Release]:
        retrieved = normalize_retrieved_at(retrieved_at)
        body, request_error = retrieve(
            self.client,
            self.source_url,
            headers={"Accept": "application/json"},
        )
        if request_error is not None or body is None:
            return error_result(self.source_url, retrieved, request_error or "NETWORK_ERROR")
        try:
            return self.parse(body, retrieved)
        except Exception:
            return error_result(self.source_url, retrieved, "PARSE_ERROR")

    def parse(self, body: bytes, retrieved_at: datetime) -> SourceResult[Release]:
        payload = json.loads(body.decode("utf-8"))
        articles = payload["articles"]
        if not isinstance(articles, list):
            raise TypeError("articles must be a list")

        releases: dict[str, Release] = {}
        partial = False
        for article in articles:
            if not isinstance(article, dict):
                partial = True
                continue
            title = article.get("title")
            raw_url = article.get("url")
            seen_date = article.get("seendate")
            if (
                not isinstance(title, str)
                or not title.strip()
                or not isinstance(raw_url, str)
                or not raw_url.strip()
                or not isinstance(seen_date, str)
                or not seen_date.strip()
            ):
                partial = True
                continue
            url = _canonicalize_url(raw_url)
            if url is None:
                partial = True
                continue
            try:
                seen_at = _parse_seen_date(seen_date)
            except ValueError:
                partial = True
                continue

            source_domain = _optional_string(article.get("domain"))
            domain = (source_domain or urlparse(url).netloc).lower() or None
            release = Release(
                title=" ".join(str(title).split()),
                url=url,
                publisher=domain or "Unknown publisher",
                seen_at=seen_at,
                category="news_discovery",
                kind=ReleaseKind.DISCOVERY,
                domain=domain,
                language=_optional_string(article.get("language")),
                source_country=_optional_string(article.get("sourcecountry")),
            )
            existing = releases.get(url)
            existing_time = (
                existing.seen_at
                if existing is not None and existing.seen_at is not None
                else datetime.min.replace(tzinfo=UTC)
            )
            if existing is None or existing_time < seen_at:
                releases[url] = release

        ordered = tuple(
            sorted(
                releases.values(),
                key=lambda release: release.seen_at or datetime.min.replace(tzinfo=UTC),
                reverse=True,
            )
        )
        return success_result(
            ordered,
            self.source_url,
            retrieved_at,
            degraded_code="PARTIAL_DATA" if partial and ordered else None,
        )


def _canonicalize_url(value: str) -> str | None:
    parsed = urlparse(value.strip())
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    kept = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in _TRACKING_PARAMETERS:
            continue
        kept.append((key, item))
    return parsed._replace(
        netloc=parsed.netloc.lower(),
        query=urlencode(sorted(kept)),
        fragment="",
    ).geturl()


def _parse_seen_date(value: str) -> datetime:
    text = value.strip()
    for pattern in ("%Y%m%dT%H%M%SZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=UTC)
        except ValueError:
            continue
    normalized = text.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).split())
    return normalized or None
