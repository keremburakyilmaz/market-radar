"""Small RFC 5545 subset for official BLS and BEA release calendars."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from datetime import datetime, time
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .base import (
    UTC,
    CalendarEvent,
    HttpClient,
    SourceResult,
    error_result,
    normalize_retrieved_at,
    retrieve,
    success_result,
)

BLS_CALENDAR_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
BEA_CALENDAR_URL = "https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics"

_WINDOWS_TIMEZONES = {
    "Eastern Standard Time": "America/New_York",
    "US Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
}


class IcsCalendarAdapter:
    """Parse VEVENT metadata without retaining descriptions or attachments."""

    def __init__(
        self,
        client: HttpClient,
        source_url: str,
        authority: str,
        region: str,
        source_timezone: str,
    ) -> None:
        self.client = client
        self.source_url = source_url
        self.authority = authority
        self.region = region
        self.source_timezone = source_timezone

    def fetch(self, retrieved_at: datetime | None = None) -> SourceResult[CalendarEvent]:
        retrieved = normalize_retrieved_at(retrieved_at)
        body, request_error = retrieve(
            self.client,
            self.source_url,
            headers={"Accept": "text/calendar"},
        )
        if request_error is not None or body is None:
            return error_result(self.source_url, retrieved, request_error or "NETWORK_ERROR")
        try:
            return self.parse(body, retrieved)
        except Exception:
            return error_result(self.source_url, retrieved, "PARSE_ERROR")

    def parse(self, body: bytes, retrieved_at: datetime) -> SourceResult[CalendarEvent]:
        lines = _unfold(body.decode("utf-8-sig"))
        raw_events = _collect_events(lines)
        events: dict[str, CalendarEvent] = {}
        partial = False

        for raw in raw_events:
            status = _property_value(raw, "STATUS")
            if status and status.upper() == "CANCELLED":
                continue
            summary = _property_value(raw, "SUMMARY")
            start_property = _first_property(raw, "DTSTART")
            if not summary or start_property is None:
                partial = True
                continue
            try:
                scheduled_at, all_day = _parse_ical_datetime(
                    start_property[1],
                    start_property[0],
                    self.source_timezone,
                )
            except (ValueError, ZoneInfoNotFoundError):
                partial = True
                continue

            end_property = _first_property(raw, "DTEND")
            ends_at = None
            if end_property is not None:
                try:
                    ends_at, _ = _parse_ical_datetime(
                        end_property[1], end_property[0], self.source_timezone
                    )
                except (ValueError, ZoneInfoNotFoundError):
                    partial = True

            title = _unescape(summary)
            uid = _property_value(raw, "UID")
            event_id = uid.strip() if uid else _stable_id(self.authority, title, scheduled_at)
            event_url = _safe_url(_property_value(raw, "URL"), self.source_url)
            tentative = (
                all_day
                or bool(status and status.upper() == "TENTATIVE")
                or "tentative" in title.lower()
            )
            events[event_id] = CalendarEvent(
                event_id=event_id,
                title=title,
                scheduled_at=scheduled_at,
                authority=self.authority,
                region=self.region,
                event_url=event_url,
                ends_at=ends_at,
                tentative=tentative,
            )

        ordered = tuple(
            sorted(events.values(), key=lambda event: (event.scheduled_at, event.event_id))
        )
        return success_result(
            ordered,
            self.source_url,
            retrieved_at,
            degraded_code="PARTIAL_DATA" if partial and ordered else None,
        )


def bls_calendar_adapter(client: HttpClient) -> IcsCalendarAdapter:
    return IcsCalendarAdapter(
        client,
        BLS_CALENDAR_URL,
        authority="U.S. Bureau of Labor Statistics",
        region="US",
        source_timezone="America/New_York",
    )


def bea_calendar_adapter(client: HttpClient) -> IcsCalendarAdapter:
    return IcsCalendarAdapter(
        client,
        BEA_CALENDAR_URL,
        authority="U.S. Bureau of Economic Analysis",
        region="US",
        source_timezone="America/New_York",
    )


Property = tuple[Mapping[str, str], str]


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _collect_events(lines: Iterable[str]) -> list[dict[str, list[Property]]]:
    events: list[dict[str, list[Property]]] = []
    current: dict[str, list[Property]] | None = None
    for line in lines:
        if line.upper() == "BEGIN:VEVENT":
            current = {}
            continue
        if line.upper() == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        head, value = line.split(":", 1)
        pieces = head.split(";")
        name = pieces[0].upper()
        params: dict[str, str] = {}
        for piece in pieces[1:]:
            if "=" in piece:
                key, parameter_value = piece.split("=", 1)
                params[key.upper()] = parameter_value.strip('"')
        current.setdefault(name, []).append((params, value))
    return events


def _first_property(event: Mapping[str, list[Property]], name: str) -> Property | None:
    properties = event.get(name)
    return properties[0] if properties else None


def _property_value(event: Mapping[str, list[Property]], name: str) -> str | None:
    prop = _first_property(event, name)
    return prop[1] if prop is not None else None


def _parse_ical_datetime(
    value: str,
    params: Mapping[str, str],
    default_timezone: str,
) -> tuple[datetime, bool]:
    text = value.strip()
    is_date = params.get("VALUE", "").upper() == "DATE" or (len(text) == 8 and "T" not in text)
    if is_date:
        parsed_date = datetime.strptime(text, "%Y%m%d").date()
        local = datetime.combine(parsed_date, time.min, ZoneInfo(default_timezone))
        return local.astimezone(UTC), True

    if text.endswith("Z"):
        for pattern in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%MZ"):
            try:
                return datetime.strptime(text, pattern).replace(tzinfo=UTC), False
            except ValueError:
                continue
        raise ValueError("unsupported UTC date-time")

    parsed = None
    for pattern in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
        try:
            parsed = datetime.strptime(text, pattern)
            break
        except ValueError:
            continue
    if parsed is None:
        raise ValueError("unsupported local date-time")

    timezone_name = params.get("TZID", default_timezone)
    timezone_name = _WINDOWS_TIMEZONES.get(timezone_name, timezone_name)
    return parsed.replace(tzinfo=ZoneInfo(timezone_name)).astimezone(UTC), False


def _unescape(value: str) -> str:
    return " ".join(
        value.replace("\\n", " ")
        .replace("\\N", " ")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .split()
    )


def _safe_url(value: str | None, base_url: str) -> str | None:
    if not value:
        return None
    absolute = urljoin(base_url, _unescape(value))
    parsed = urlparse(absolute)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return parsed._replace(fragment="").geturl()


def _stable_id(authority: str, title: str, scheduled_at: datetime) -> str:
    raw = "|".join((authority, title, scheduled_at.isoformat())).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]
