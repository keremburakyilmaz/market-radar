"""Official-source adapters used by Market Radar."""

from .base import (
    CalendarEvent,
    Freshness,
    HttpClient,
    HttpResponse,
    IndicatorObservation,
    Release,
    ReleaseKind,
    SourceResult,
    SourceStatus,
    UrllibHttpClient,
)
from .calendar import bea_calendar_adapter, bls_calendar_adapter, IcsCalendarAdapter
from .cbrt import CbrtUsdTryAdapter
from .feeds import (
    FeedAdapter,
    cbrt_press_adapter,
    ecb_press_adapter,
    fed_press_adapter,
)
from .fred import FredBroadUsdAdapter
from .gdelt import GdeltDocAdapter
from .treasury import TreasuryYieldAdapter

__all__ = [
    "CalendarEvent",
    "Freshness",
    "HttpClient",
    "HttpResponse",
    "IndicatorObservation",
    "Release",
    "ReleaseKind",
    "SourceResult",
    "SourceStatus",
    "UrllibHttpClient",
    "CbrtUsdTryAdapter",
    "FeedAdapter",
    "FredBroadUsdAdapter",
    "GdeltDocAdapter",
    "IcsCalendarAdapter",
    "TreasuryYieldAdapter",
    "bea_calendar_adapter",
    "bls_calendar_adapter",
    "cbrt_press_adapter",
    "ecb_press_adapter",
    "fed_press_adapter",
]
