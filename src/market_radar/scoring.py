"""Transparent, deterministic macro-pressure scoring."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from market_radar.domain import CollectedIndicator


@dataclass(frozen=True)
class MacroDriver:
    driver_id: str
    indicator_id: str
    label: str
    score: float
    weight: float
    explanation: str


@dataclass(frozen=True)
class MacroConditions:
    score: float
    label: str
    summary: str
    drivers: tuple[MacroDriver, ...]

    def public_dict(self) -> dict[str, Any]:
        weight_total = sum(driver.weight for driver in self.drivers)
        driver_payload = []
        contribution_total = 0.0
        for index, driver in enumerate(self.drivers):
            normalized_weight = driver.weight / weight_total
            normalized_signal = _clamp((driver.score - 50.0) / 50.0, -1.0, 1.0)
            contribution = normalized_weight * (driver.score - 50.0)
            if index == len(self.drivers) - 1:
                contribution = round(self.score, 1) - 50.0 - contribution_total
            contribution = round(contribution, 2)
            contribution_total += contribution
            if normalized_signal > 0.1:
                direction = "restrictive"
            elif normalized_signal < -0.1:
                direction = "supportive"
            else:
                direction = "balanced"
            driver_payload.append(
                {
                    "indicatorId": driver.indicator_id,
                    "label": driver.label,
                    "weight": round(normalized_weight, 4),
                    "normalizedSignal": round(normalized_signal, 4),
                    "contributionPoints": contribution,
                    "direction": direction,
                    "explanation": driver.explanation,
                    "marketTags": _driver_tags(driver.indicator_id),
                }
            )

        if self.score < 40:
            public_label = "supportive"
        elif self.score < 60:
            public_label = "balanced"
        else:
            public_label = "restrictive"
        return {
            "score": round(self.score, 1),
            "label": public_label,
            "summary": self.summary,
            "scoreScale": {
                "minimum": 0,
                "maximum": 100,
                "higherMeans": "More restrictive macro-financial conditions",
            },
            "methodology": {
                "id": "macro-conditions-v1",
                "version": "1.0.0",
                "description": (
                    "A deterministic weighted score built only from the published official macro "
                    "indicators. Missing drivers are omitted and remaining weights are normalized."
                ),
                "baselineScore": 50,
                "formula": "score = clamp(baselineScore + sum(driver contributionPoints), 0, 100)",
            },
            "drivers": driver_payload,
        }


def _clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    return max(minimum, min(maximum, value))


def _latest(values: Sequence[CollectedIndicator]) -> CollectedIndicator | None:
    if not values:
        return None
    return max(values, key=lambda item: item.observed_at)


def _long_rate_driver(
    indicators: Mapping[str, CollectedIndicator],
) -> MacroDriver | None:
    observation = indicators.get("us-treasury-10y")
    if observation is None:
        return None
    value = float(observation.value)
    score = _clamp((value - 2.5) / 3.0 * 100.0)
    return MacroDriver(
        driver_id="us-10y-level",
        indicator_id="us-treasury-10y",
        label="US 10Y level",
        score=score,
        weight=0.45,
        explanation=(
            "Higher long-term Treasury yields increase the discount-rate and "
            "financing-pressure input."
        ),
    )


def _curve_driver(
    indicators: Mapping[str, CollectedIndicator],
) -> MacroDriver | None:
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
        indicator_id="us-curve-2s10s",
        label="US 2s10s curve",
        score=score,
        weight=0.30,
        explanation=(
            "A more inverted curve raises the slowdown-pressure input; a steeper positive "
            "curve lowers it."
        ),
    )


def _broad_usd_driver(
    histories: Mapping[str, Sequence[CollectedIndicator]],
) -> MacroDriver | None:
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
        indicator_id="fed-broad-usd",
        label="Broad USD momentum",
        score=score,
        weight=0.25,
        explanation=(
            f"The Federal Reserve broad dollar index is {direction} over the "
            f"comparison window ({change_percent:+.2f}%)."
        ),
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
        summary = f"Available official inputs point to {label}."
    else:
        summary = (
            f"{strongest.label} is the strongest pressure input; "
            f"{softest.label} provides the largest offset."
        )

    return MacroConditions(score=score, label=label, summary=summary, drivers=drivers)


def latest_indicators(
    indicators: Sequence[CollectedIndicator],
) -> dict[str, CollectedIndicator]:
    grouped: dict[str, list[CollectedIndicator]] = {}
    for indicator in indicators:
        grouped.setdefault(indicator.indicator_id, []).append(indicator)
    return {
        indicator_id: latest
        for indicator_id, values in grouped.items()
        for latest in [_latest(values)]
        if latest is not None
    }


def _driver_tags(indicator_id: str) -> list[str]:
    if indicator_id == "fed-broad-usd":
        return ["global", "united-states", "fx"]
    return ["global", "united-states", "rates"]
