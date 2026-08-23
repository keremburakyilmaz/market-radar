"""Durable, compact pipeline state for change detection and deduplication."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from market_radar.canonical import canonical_json_bytes


STATE_VERSION = 1


@dataclass(frozen=True)
class RadarState:
    previous_snapshot_id: Optional[str] = None
    indicator_values: Mapping[str, float] = field(default_factory=dict)
    seen_release_urls: Tuple[str, ...] = ()
    successful_slots: Tuple[str, ...] = ()

    def public_dict(self) -> dict:
        return {
            "stateVersion": STATE_VERSION,
            "previousSnapshotId": self.previous_snapshot_id,
            "indicatorValues": dict(sorted(self.indicator_values.items())),
            "seenReleaseUrls": list(self.seen_release_urls[-2000:]),
            "successfulSlots": list(self.successful_slots[-500:]),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RadarState":
        if value.get("stateVersion") != STATE_VERSION:
            raise ValueError("unsupported state version")
        raw_values = value.get("indicatorValues", {})
        raw_urls = value.get("seenReleaseUrls", [])
        raw_slots = value.get("successfulSlots", [])
        if not isinstance(raw_values, dict) or not isinstance(raw_urls, list) or not isinstance(
            raw_slots, list
        ):
            raise ValueError("invalid state shape")
        indicator_values = {
            str(key): float(number)
            for key, number in raw_values.items()
            if isinstance(number, (int, float)) and not isinstance(number, bool)
        }
        return cls(
            previous_snapshot_id=(
                str(value["previousSnapshotId"])
                if value.get("previousSnapshotId") is not None
                else None
            ),
            indicator_values=indicator_values,
            seen_release_urls=tuple(str(item) for item in raw_urls if isinstance(item, str))[-2000:],
            successful_slots=tuple(str(item) for item in raw_slots if isinstance(item, str))[-500:],
        )


def load_state(path: Path) -> RadarState:
    if not path.exists():
        return RadarState()
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle, parse_constant=_reject_constant)
    if not isinstance(value, dict):
        raise ValueError("state root must be an object")
    return RadarState.from_dict(value)


def save_state(path: Path, state: RadarState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = canonical_json_bytes(state.public_dict())
    descriptor, temporary_name = tempfile.mkstemp(dir=str(path.parent), prefix=".state-")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite state value is not allowed: {}".format(value))

