"""CBRT indicative exchange-rate adapter."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Mapping, Optional

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


CBRT_TODAY_URL = "https://www.tcmb.gov.tr/kurlar/today.xml"


class CbrtUsdTryAdapter:
    """Read the once-daily official USD ForexBuying value."""

    def __init__(
        self,
        client: HttpClient,
        source_url: str = CBRT_TODAY_URL,
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
        observed_at = _parse_rate_date(root.attrib)

        usd = None
        for currency in root.iter():
            code = currency.attrib.get("CurrencyCode") or currency.attrib.get("Kod")
            if code == "USD":
                usd = currency
                break
        if usd is None:
            return error_result(self.source_url, retrieved_at, "USD_RATE_MISSING")

        unit_text = _child_text(usd, "Unit")
        buying_text = _child_text(usd, "ForexBuying")
        if unit_text is None or Decimal(unit_text) != Decimal("1"):
            return error_result(self.source_url, retrieved_at, "UNSUPPORTED_UNIT")
        if buying_text is None:
            return error_result(self.source_url, retrieved_at, "USD_RATE_MISSING")

        observation = IndicatorObservation(
            indicator_id="cbrt-usd-try",
            value=Decimal(buying_text),
            unit="TRY_per_USD",
            observed_at=observed_at,
        )
        return success_result(
            (observation,),
            self.source_url,
            retrieved_at,
            latest_observed_at=observed_at,
            max_age=self.max_age,
        )


def _parse_rate_date(attributes: Mapping[str, str]) -> datetime:
    value = attributes.get("Date") or attributes.get("Tarih")
    if not value:
        raise ValueError("missing rate date")
    for pattern in ("%m/%d/%Y", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value, pattern)
            return parsed.replace(tzinfo=UTC)
        except ValueError:
            continue
    raise ValueError("unsupported rate date")


def _child_text(parent: ET.Element, name: str) -> Optional[str]:
    for child in parent:
        if child.tag.rsplit("}", 1)[-1] == name and child.text:
            value = child.text.strip()
            return value or None
    return None
