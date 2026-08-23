"""U.S. Treasury daily par-yield adapter."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, Optional, Tuple

from .base import (
    HttpClient,
    IndicatorObservation,
    SourceResult,
    UTC,
    error_result,
    normalize_retrieved_at,
    retrieve,
    success_result,
)


TREASURY_YIELD_URL = (
    "https://home.treasury.gov/sites/default/files/interest-rates/yield.xml"
)


class TreasuryYieldAdapter:
    """Read 2-year and 10-year CMT values plus the 10y-2y curve."""

    def __init__(
        self,
        client: HttpClient,
        source_url: str = TREASURY_YIELD_URL,
        max_age: timedelta = timedelta(days=4),
    ) -> None:
        self.client = client
        self.source_url = source_url
        self.max_age = max_age

    def fetch(
        self, retrieved_at: Optional[datetime] = None
    ) -> SourceResult[IndicatorObservation]:
        retrieved = normalize_retrieved_at(retrieved_at)
        body, request_error = retrieve(
            self.client,
            self.source_url,
            headers={"Accept": "application/xml, text/xml;q=0.9"},
        )
        if request_error is not None or body is None:
            return error_result(self.source_url, retrieved, request_error or "NETWORK_ERROR")
        try:
            return self.parse(body, retrieved)
        except Exception:
            return error_result(self.source_url, retrieved, "PARSE_ERROR")

    def parse(
        self, body: bytes, retrieved_at: datetime
    ) -> SourceResult[IndicatorObservation]:
        root = ET.fromstring(body)
        observations: Dict[Tuple[datetime, str], IndicatorObservation] = {}
        partial = False

        entries = [element for element in root.iter() if _local(element.tag) == "entry"]
        if not entries and _local(root.tag) == "entry":
            entries = [root]

        for entry in entries:
            fields: Dict[str, str] = {}
            for element in entry.iter():
                local = _local(element.tag)
                if local in {"NEW_DATE", "BC_2YEAR", "BC_10YEAR"} and element.text:
                    fields[local] = element.text.strip()

            if not fields:
                continue
            if "NEW_DATE" not in fields:
                partial = True
                continue

            observed_at = _parse_treasury_date(fields["NEW_DATE"])
            two_year = _optional_decimal(fields.get("BC_2YEAR"))
            ten_year = _optional_decimal(fields.get("BC_10YEAR"))
            if two_year is None or ten_year is None:
                partial = True

            if two_year is not None:
                item = IndicatorObservation(
                    indicator_id="us-treasury-2y",
                    value=two_year,
                    unit="percent",
                    observed_at=observed_at,
                )
                observations[(observed_at, item.indicator_id)] = item
            if ten_year is not None:
                item = IndicatorObservation(
                    indicator_id="us-treasury-10y",
                    value=ten_year,
                    unit="percent",
                    observed_at=observed_at,
                )
                observations[(observed_at, item.indicator_id)] = item
            if two_year is not None and ten_year is not None:
                item = IndicatorObservation(
                    indicator_id="us-curve-2s10s",
                    value=(ten_year - two_year) * Decimal("100"),
                    unit="basis_points",
                    observed_at=observed_at,
                )
                observations[(observed_at, item.indicator_id)] = item

        ordered = tuple(
            observations[key]
            for key in sorted(observations, key=lambda pair: (pair[0], pair[1]))
        )
        if not ordered:
            return error_result(self.source_url, retrieved_at, "EMPTY_RESULT")
        latest = max(item.observed_at for item in ordered)
        return success_result(
            ordered,
            self.source_url,
            retrieved_at,
            latest_observed_at=latest,
            max_age=self.max_age,
            degraded_code="PARTIAL_DATA" if partial else None,
        )


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _parse_treasury_date(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = datetime.strptime(value.strip()[:10], "%Y-%m-%d")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_decimal(value: Optional[str]) -> Optional[Decimal]:
    if value is None or not value.strip() or value.strip() == ".":
        return None
    return Decimal(value.strip())
