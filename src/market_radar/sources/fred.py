"""FRED adapter for the Federal Reserve broad U.S. dollar index."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from .base import (
    UTC,
    HttpClient,
    IndicatorObservation,
    SourceResult,
    error_result,
    normalize_retrieved_at,
    retrieve,
    success_result,
)

FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_BROAD_USD_SOURCE_URL = "https://fred.stlouisfed.org/series/DTWEXBGS"


class FredBroadUsdAdapter:
    """Read DTWEXBGS while keeping its source observation dates."""

    def __init__(
        self,
        client: HttpClient,
        api_key: str | None,
        max_age: timedelta = timedelta(days=10),
    ) -> None:
        self.client = client
        self.api_key = api_key.strip() if api_key else ""
        self.max_age = max_age

    def fetch(self, retrieved_at: datetime | None = None) -> SourceResult[IndicatorObservation]:
        retrieved = normalize_retrieved_at(retrieved_at)
        if not self.api_key:
            return error_result(FRED_BROAD_USD_SOURCE_URL, retrieved, "AUTH_MISSING")

        query: dict[str, str] = {
            "series_id": "DTWEXBGS",
            "api_key": self.api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": "90",
        }
        request_url = f"{FRED_API_URL}?{urlencode(query)}"
        body, request_error = retrieve(
            self.client,
            request_url,
            headers={"Accept": "application/json"},
        )
        if request_error is not None or body is None:
            return error_result(
                FRED_BROAD_USD_SOURCE_URL,
                retrieved,
                request_error or "NETWORK_ERROR",
            )
        try:
            return self.parse(body, retrieved)
        except Exception:
            return error_result(FRED_BROAD_USD_SOURCE_URL, retrieved, "PARSE_ERROR")

    def parse(self, body: bytes, retrieved_at: datetime) -> SourceResult[IndicatorObservation]:
        payload = json.loads(body.decode("utf-8"))
        raw_observations = payload["observations"]
        if not isinstance(raw_observations, list):
            raise TypeError("observations must be a list")

        by_date: dict[datetime, IndicatorObservation] = {}
        partial = False
        for raw in raw_observations:
            if not isinstance(raw, dict):
                partial = True
                continue
            date_text = raw.get("date")
            value_text = raw.get("value")
            if value_text == ".":
                continue
            if not isinstance(date_text, str) or not isinstance(value_text, str):
                partial = True
                continue
            try:
                observed_at = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=UTC)
                value = Decimal(value_text)
            except (InvalidOperation, ValueError):
                partial = True
                continue
            by_date[observed_at] = IndicatorObservation(
                indicator_id="fed-broad-usd",
                value=value,
                unit="index_2006_01_100",
                observed_at=observed_at,
            )

        ordered = tuple(by_date[key] for key in sorted(by_date))
        if not ordered:
            return error_result(FRED_BROAD_USD_SOURCE_URL, retrieved_at, "EMPTY_RESULT")
        return success_result(
            ordered,
            FRED_BROAD_USD_SOURCE_URL,
            retrieved_at,
            latest_observed_at=ordered[-1].observed_at,
            max_age=self.max_age,
            degraded_code="PARTIAL_DATA" if partial else None,
        )
