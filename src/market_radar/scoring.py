"""Transparent, deterministic macro-pressure scoring."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Mapping, Optional, Sequence, Tuple

from market_radar.domain import CollectedIndicator


METHODOLOGY_VERSION = "macro-pressure-v1"


@dataclass(frozen=True)
class MacroDriver:
    driver_id: str
    label: str
    score: float
    weight: float
    explanation: str

    def public_dict(self) -> dict:
        return {
            "id": self.driver_id,
            "label": self.label,
            "score": round(self.score, 1),
            "weight": self.weight,
            "explanation": self.explanation,
        }


@dataclass(frozen=True)
class MacroConditions:
    score: float
    label: str
    summary: str
    drivers: Tuple[MacroDriver, ...]

    def public_dict(self) -> dict:
        return {
            "score": round(self.score, 1),
            "label": self.label,
            "summary": self.summary,
            "methodologyVersion": METHODOLOGY_VERSION,
            "drivers": [driver.public_dict() for driver in self.drivers],
        }


def _clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    return max(minimum, min(maximum, value))


def _latest(values: Sequence[CollectedIndicator]) -> Optional[CollectedIndicator]:
    if not values:
        return None
    return max(values, key=lambda item: item.observed_at)


def _long_rate_driver(
    indicators: Mapping[str, CollectedIndicator],
) -> Optional[MacroDriver]:
    observation = indicators.get("us-treasury-10y")
    if observation is None:
        return None
    value = float(observation.value)
    score = _clamp((value - 2.5) / 3.0 * 100.0)
    return MacroDriver(
        driver_id="us-10y-level",
        label="US 10Y level",
        score=score,
        weight=0.45,
        explanation=(
            "Higher long-term Treasury yields increase the discount-rate and financing-pressure input."
        ),
    )


def _curve_driver(
    indicators: Mapping[str, CollectedIndicator],
) -> Optional[MacroDriver]:
    curve = indicators.get("us-curve-2s10s")
    if curve is None:
        two_year = indicators.get("us-treasury-2y")
        ten_year = indicators.get("us-treasury-10y")
        if two_year is None or ten_year is None:
            return None
        spread_basis_points = float((ten_year.value - two_year.value) * Decimal("100"))
    else:
        spread_basis_points = float(curve.value)
    score = _clamp(50.0 - spread_basis_points * 0.5)
    return MacroDriver(
        driver_id="us-2s10s-curve",
        label="US 2s10s curve",
        score=score,
        weight=0.30,
        explanation=(
            "A more inverted curve raises the slowdown-pressure input; a steeper positive curve lowers it."
        ),
    )


def _broad_usd_driver(
    histories: Mapping[str, Sequence[CollectedIndicator]],
) -> Optional[MacroDriver]:
    values = sorted(histories.get("fed-broad-usd", ()), key=lambda item: item.observed_at)
    if len(values) < 2:
        return None
    latest = values[-1]
    comparison = values[-21] if len(values) >= 21 else values[0]
    base = float(comparison.value)
    if base == 0:
        return None
    change_percent = (float(latest.value) - base) / base * 100.0
    score = _clamp(50.0 + change_percent * 12.5)
    direction = "strengthening" if change_percent > 0 else "easing"
    return MacroDriver(
        driver_id="broad-usd-momentum",
        label="Broad USD momentum",
        score=score,
        weight=0.25,
        explanation=(
            "The Federal Reserve broad dollar index is {} over the comparison window ({:+.2f}%)."
        ).format(direction, change_percent),
    )


def score_macro_conditions(
    current: Mapping[str, CollectedIndicator],
    histories: Mapping[str, Sequence[CollectedIndicator]],
) -> MacroConditions:
    """Compute macro pressure from available official observations.

    The method deliberately makes no equity-market or volatility claim. Missing
    drivers are omitted and the remaining weights are normalized. At least two
    available drivers are required to produce a public score.
    """

    candidates = (
        _long_rate_driver(current),
        _curve_driver(current),
        _broad_usd_driver(histories),
    )
    drivers = tuple(driver for driver in candidates if driver is not None)
    if len(drivers) < 2:
        raise ValueError("macro conditions require at least two available drivers")

    weight_total = sum(driver.weight for driver in drivers)
    score = sum(driver.score * driver.weight for driver in drivers) / weight_total
    if score < 35:
        label = "low pressure"
    elif score < 60:
        label = "moderate pressure"
    elif score < 75:
        label = "elevated pressure"
    else:
        label = "high pressure"

    strongest = max(drivers, key=lambda driver: driver.score)
    softest = min(drivers, key=lambda driver: driver.score)
    if strongest.driver_id == softest.driver_id:
        summary = "Available official inputs point to {}.".format(label)
    else:
        summary = (
            "{} is the strongest pressure input; {} provides the largest offset."
        ).format(strongest.label, softest.label)

    return MacroConditions(score=score, label=label, summary=summary, drivers=drivers)


def latest_indicators(
    indicators: Sequence[CollectedIndicator],
) -> Dict[str, CollectedIndicator]:
    grouped: Dict[str, list] = {}
    for indicator in indicators:
        grouped.setdefault(indicator.indicator_id, []).append(indicator)
    return {
        indicator_id: latest
        for indicator_id, values in grouped.items()
        for latest in [_latest(values)]
        if latest is not None
    }
