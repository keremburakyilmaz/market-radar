"""Shared source models and small, dependency-free HTTP primitives."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Generic, Mapping, Optional, Protocol, Tuple, TypeVar
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import time


UTC = timezone.utc
T = TypeVar("T")
MAX_RESPONSE_BYTES = 5_000_000


class SourceStatus(str, Enum):
    """Operational status of one source request and parse."""

    OK = "ok"
    DEGRADED = "degraded"
    ERROR = "error"


class Freshness(str, Enum):
    """Whether returned data is current enough for its source cadence."""

    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class ReleaseKind(str, Enum):
    OFFICIAL = "official"
    DISCOVERY = "discovery"


@dataclass(frozen=True)
class IndicatorObservation:
    indicator_id: str
    value: Decimal
    unit: str
    observed_at: datetime
    granularity: str = "day"

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")


@dataclass(frozen=True)
class Release:
    """Release or discovery metadata; article bodies are intentionally absent."""

    title: str
    url: str
    publisher: str
    published_at: Optional[datetime] = None
    seen_at: Optional[datetime] = None
    category: Optional[str] = None
    kind: ReleaseKind = ReleaseKind.OFFICIAL
    domain: Optional[str] = None
    language: Optional[str] = None
    source_country: Optional[str] = None

    def __post_init__(self) -> None:
        if self.published_at is not None:
            _require_aware(self.published_at, "published_at")
        if self.seen_at is not None:
            _require_aware(self.seen_at, "seen_at")


@dataclass(frozen=True)
class CalendarEvent:
    event_id: str
    title: str
    scheduled_at: datetime
    authority: str
    region: str
    event_url: Optional[str] = None
    ends_at: Optional[datetime] = None
    tentative: bool = False

    def __post_init__(self) -> None:
        _require_aware(self.scheduled_at, "scheduled_at")
        if self.ends_at is not None:
            _require_aware(self.ends_at, "ends_at")


@dataclass(frozen=True)
class SourceResult(Generic[T]):
    """Typed source output with provenance and a safe machine error code."""

    items: Tuple[T, ...]
    retrieved_at: datetime
    source_url: str
    freshness: Freshness
    status: SourceStatus
    error_code: Optional[str] = None

    def __post_init__(self) -> None:
        _require_aware(self.retrieved_at, "retrieved_at")
        if self.error_code is not None and self.error_code != sanitize_error_code(
            self.error_code
        ):
            raise ValueError("error_code must already be sanitized")


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


class HttpClient(Protocol):
    def get(
        self,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        timeout: float = 20.0,
    ) -> HttpResponse:
        ...


class UrllibHttpClient:
    """Production client. Tests inject a fake implementing ``HttpClient``."""

    def get(
        self,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        timeout: float = 20.0,
    ) -> HttpResponse:
        request = Request(url, headers=dict(headers or {}), method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError("response exceeds the configured byte limit")
                return HttpResponse(
                    status=int(response.status),
                    body=body,
                    headers=dict(response.headers.items()),
                )
        except HTTPError as error:
            return HttpResponse(
                status=int(error.code),
                body=error.read(),
                headers=dict(error.headers.items()) if error.headers else {},
            )


_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_ALLOWED_ERROR_CODES = {
    "ADAPTER_ERROR",
    "AUTH_ERROR",
    "AUTH_MISSING",
    "EMPTY_RESULT",
    "FUTURE_DATA",
    "HTTP_ERROR",
    "NETWORK_ERROR",
    "NOT_FOUND",
    "PARSE_ERROR",
    "PARTIAL_DATA",
    "RATE_LIMITED",
    "SOURCE_ERROR",
    "STALE_DATA",
    "UNSUPPORTED_UNIT",
    "UPSTREAM_ERROR",
    "USD_RATE_MISSING",
}


def sanitize_error_code(code: str) -> str:
    """Convert an internal label to a bounded code that cannot leak exceptions."""

    sanitized = re.sub(r"[^A-Z0-9]+", "_", code.upper()).strip("_")[:64]
    if not sanitized or not sanitized[0].isalpha():
        sanitized = "SOURCE_ERROR"
    if not _ERROR_CODE.fullmatch(sanitized) or sanitized not in _ALLOWED_ERROR_CODES:
        return "SOURCE_ERROR"
    return sanitized


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def normalize_retrieved_at(value: Optional[datetime]) -> datetime:
    if value is None:
        return utc_now()
    _require_aware(value, "retrieved_at")
    return value.astimezone(UTC)


def error_result(
    source_url: str,
    retrieved_at: datetime,
    error_code: str,
) -> SourceResult[T]:
    return SourceResult(
        items=(),
        retrieved_at=retrieved_at,
        source_url=source_url,
        freshness=Freshness.UNKNOWN,
        status=SourceStatus.ERROR,
        error_code=sanitize_error_code(error_code),
    )


def success_result(
    items: Tuple[T, ...],
    source_url: str,
    retrieved_at: datetime,
    *,
    latest_observed_at: Optional[datetime] = None,
    max_age: Optional[timedelta] = None,
    degraded_code: Optional[str] = None,
) -> SourceResult[T]:
    if not items:
        return error_result(source_url, retrieved_at, "EMPTY_RESULT")

    freshness = Freshness.FRESH
    status = SourceStatus.OK
    error_code = None

    if latest_observed_at is not None and max_age is not None:
        _require_aware(latest_observed_at, "latest_observed_at")
        latest_utc = latest_observed_at.astimezone(UTC)
        if latest_utc - retrieved_at > timedelta(minutes=5):
            freshness = Freshness.UNKNOWN
            status = SourceStatus.DEGRADED
            error_code = "FUTURE_DATA"
        elif retrieved_at - latest_utc > max_age:
            freshness = Freshness.STALE
            status = SourceStatus.DEGRADED
            error_code = "STALE_DATA"

    if degraded_code is not None and status is SourceStatus.OK:
        status = SourceStatus.DEGRADED
        error_code = sanitize_error_code(degraded_code)

    return SourceResult(
        items=items,
        retrieved_at=retrieved_at,
        source_url=source_url,
        freshness=freshness,
        status=status,
        error_code=error_code,
    )


def retrieve(
    client: HttpClient,
    request_url: str,
    *,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = 20.0,
    attempts: int = 3,
) -> Tuple[Optional[bytes], Optional[str]]:
    """Retrieve bytes and collapse all failures to non-sensitive codes."""

    request_headers = {
        "User-Agent": "MarketRadar/1.0 (+https://github.com/keremburakyilmaz)",
        **dict(headers or {}),
    }
    if attempts < 1 or attempts > 5:
        raise ValueError("attempts must be between one and five")

    last_code = "NETWORK_ERROR"
    for attempt in range(attempts):
        try:
            response = client.get(request_url, headers=request_headers, timeout=timeout)
        except Exception:
            last_code = "NETWORK_ERROR"
        else:
            if len(response.body) > MAX_RESPONSE_BYTES:
                return None, "RESPONSE_TOO_LARGE"
            if 200 <= response.status < 300:
                return response.body, None
            if response.status in (401, 403):
                return None, "AUTH_ERROR"
            if response.status == 404:
                return None, "NOT_FOUND"
            if response.status == 429:
                last_code = "RATE_LIMITED"
            elif response.status >= 500:
                last_code = "UPSTREAM_ERROR"
            else:
                return None, "HTTP_ERROR"

        if attempt < attempts - 1 and isinstance(client, UrllibHttpClient):
            time.sleep(0.25 * (attempt + 1))
    return None, last_code


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("{} must be timezone-aware".format(name))
