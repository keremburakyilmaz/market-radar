"""Fail-closed semantic validation for public Market Radar payloads."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence
from urllib.parse import urlparse

from market_radar.timeutil import parse_utc


SNAPSHOT_PATH_PATTERN = re.compile(
    r"^v1/snapshots/\d{4}/\d{2}/\d{2}/[A-Za-z0-9._-]+-[a-f0-9]{12,64}\.json$"
)
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
ALLOWED_PIPELINE_STATUSES = {"healthy", "degraded"}
ALLOWED_FRESHNESS = {"fresh", "stale", "unavailable"}


@dataclass(frozen=True)
class ValidationIssue:
    path: str
    message: str


class ContractValidationError(ValueError):
    def __init__(self, issues: Sequence[ValidationIssue]):
        self.issues = tuple(issues)
        detail = "; ".join(f"{issue.path}: {issue.message}" for issue in issues)
        super().__init__(detail)


def _expect_mapping(value: Any, path: str, issues: List[ValidationIssue]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        issues.append(ValidationIssue(path, "must be an object"))
        return {}
    return value


def _expect_list(value: Any, path: str, issues: List[ValidationIssue]) -> List[Any]:
    if not isinstance(value, list):
        issues.append(ValidationIssue(path, "must be an array"))
        return []
    return value


def _require_keys(
    value: Mapping[str, Any], required: Iterable[str], path: str, issues: List[ValidationIssue]
) -> None:
    for key in required:
        if key not in value:
            issues.append(ValidationIssue(f"{path}.{key}", "is required"))


def _parse_timestamp(value: Any, path: str, issues: List[ValidationIssue]) -> Optional[datetime]:
    if not isinstance(value, str):
        issues.append(ValidationIssue(path, "must be a UTC timestamp string"))
        return None
    try:
        return parse_utc(value)
    except (TypeError, ValueError):
        issues.append(ValidationIssue(path, "must be ISO-8601 UTC ending in Z"))
        return None


def _validate_https(value: Any, path: str, issues: List[ValidationIssue]) -> None:
    if not isinstance(value, str):
        issues.append(ValidationIssue(path, "must be a URL string"))
        return
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        issues.append(ValidationIssue(path, "must be an absolute HTTPS URL without credentials"))


def _walk_json(value: Any, path: str, issues: List[ValidationIssue]) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        issues.append(ValidationIssue(path, "must not contain NaN or infinity"))
    elif isinstance(value, str):
        if "<script" in value.lower() or "javascript:" in value.lower():
            issues.append(ValidationIssue(path, "contains unsafe executable content"))
    elif isinstance(value, Mapping):
        for key, child in value.items():
            _walk_json(child, f"{path}.{key}", issues)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_json(child, f"{path}[{index}]", issues)


def _validate_source(source: Any, path: str, issues: List[ValidationIssue]) -> None:
    item = _expect_mapping(source, path, issues)
    _require_keys(item, ("id", "name", "url"), path, issues)
    if "url" in item:
        _validate_https(item["url"], f"{path}.url", issues)


def validate_snapshot(
    snapshot: Any,
    *,
    now: Optional[datetime] = None,
    enforce_publish_time: bool = False,
) -> Dict[str, Any]:
    """Validate semantic invariants not expressible safely in JSON Schema alone."""

    issues: List[ValidationIssue] = []
    root = _expect_mapping(snapshot, "$", issues)
    _require_keys(
        root,
        (
            "schemaVersion",
            "id",
            "generatedAt",
            "validUntil",
            "pipeline",
            "macroConditions",
            "indicators",
            "priorityDevelopments",
            "stories",
            "calendar",
            "digest",
            "sources",
        ),
        "$",
        issues,
    )

    if root.get("schemaVersion") != 1:
        issues.append(ValidationIssue("$.schemaVersion", "unsupported schema version"))

    generated_at = _parse_timestamp(root.get("generatedAt"), "$.generatedAt", issues)
    valid_until = _parse_timestamp(root.get("validUntil"), "$.validUntil", issues)
    if generated_at and valid_until and valid_until <= generated_at:
        issues.append(ValidationIssue("$.validUntil", "must be after generatedAt"))

    if enforce_publish_time and generated_at:
        clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if generated_at > clock + timedelta(minutes=5):
            issues.append(ValidationIssue("$.generatedAt", "must not be in the future"))
        if generated_at < clock - timedelta(hours=2):
            issues.append(ValidationIssue("$.generatedAt", "candidate is too old to publish"))

    pipeline = _expect_mapping(root.get("pipeline"), "$.pipeline", issues)
    _require_keys(pipeline, ("status", "coverage"), "$.pipeline", issues)
    if pipeline.get("status") not in ALLOWED_PIPELINE_STATUSES:
        issues.append(ValidationIssue("$.pipeline.status", "must be healthy or degraded"))

    conditions = _expect_mapping(root.get("macroConditions"), "$.macroConditions", issues)
    _require_keys(
        conditions,
        ("score", "label", "summary", "methodologyVersion", "drivers"),
        "$.macroConditions",
        issues,
    )
    score = conditions.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 100:
        issues.append(ValidationIssue("$.macroConditions.score", "must be a number from 0 to 100"))

    indicators = _expect_list(root.get("indicators"), "$.indicators", issues)
    if len(indicators) > 50:
        issues.append(ValidationIssue("$.indicators", "must contain at most 50 items"))
    indicator_ids = set()
    for index, raw in enumerate(indicators):
        path = f"$.indicators[{index}]"
        item = _expect_mapping(raw, path, issues)
        _require_keys(
            item,
            (
                "id",
                "label",
                "value",
                "unit",
                "displayValue",
                "observedAt",
                "retrievedAt",
                "freshness",
                "source",
            ),
            path,
            issues,
        )
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            issues.append(ValidationIssue(f"{path}.id", "must be a non-empty string"))
        elif item_id in indicator_ids:
            issues.append(ValidationIssue(f"{path}.id", "must be unique"))
        else:
            indicator_ids.add(item_id)
        if item.get("freshness") not in ALLOWED_FRESHNESS:
            issues.append(ValidationIssue(f"{path}.freshness", "has an unsupported state"))
        observed = _parse_timestamp(item.get("observedAt"), f"{path}.observedAt", issues)
        retrieved = _parse_timestamp(item.get("retrievedAt"), f"{path}.retrievedAt", issues)
        if observed and retrieved and observed > retrieved + timedelta(minutes=5):
            issues.append(ValidationIssue(f"{path}.observedAt", "must not be after retrieval"))
        _validate_source(item.get("source"), f"{path}.source", issues)

    for section_name in ("priorityDevelopments", "stories"):
        section = _expect_list(root.get(section_name), f"$.{section_name}", issues)
        if len(section) > 100:
            issues.append(ValidationIssue(f"$.{section_name}", "must contain at most 100 items"))
        for index, raw in enumerate(section):
            item = _expect_mapping(raw, f"$.{section_name}[{index}]", issues)
            if "url" in item:
                _validate_https(item["url"], f"$.{section_name}[{index}].url", issues)

    calendar = _expect_list(root.get("calendar"), "$.calendar", issues)
    for index, raw in enumerate(calendar):
        item = _expect_mapping(raw, f"$.calendar[{index}]", issues)
        if "scheduledAt" in item:
            _parse_timestamp(item["scheduledAt"], f"$.calendar[{index}].scheduledAt", issues)
        if "sourceUrl" in item:
            _validate_https(item["sourceUrl"], f"$.calendar[{index}].sourceUrl", issues)

    sources = _expect_list(root.get("sources"), "$.sources", issues)
    for index, raw in enumerate(sources):
        item = _expect_mapping(raw, f"$.sources[{index}]", issues)
        if "url" in item:
            _validate_https(item["url"], f"$.sources[{index}].url", issues)

    _walk_json(root, "$", issues)
    if issues:
        raise ContractValidationError(issues)
    return dict(root)


def validate_manifest(manifest: Any) -> Dict[str, Any]:
    issues: List[ValidationIssue] = []
    root = _expect_mapping(manifest, "$", issues)
    _require_keys(root, ("manifestVersion", "publishedAt", "snapshot"), "$", issues)
    if root.get("manifestVersion") != 1:
        issues.append(ValidationIssue("$.manifestVersion", "unsupported manifest version"))
    _parse_timestamp(root.get("publishedAt"), "$.publishedAt", issues)

    snapshot = _expect_mapping(root.get("snapshot"), "$.snapshot", issues)
    _require_keys(
        snapshot,
        ("schemaVersion", "id", "path", "generatedAt", "validUntil", "sizeBytes", "sha256"),
        "$.snapshot",
        issues,
    )
    if snapshot.get("schemaVersion") != 1:
        issues.append(ValidationIssue("$.snapshot.schemaVersion", "unsupported schema version"))
    path = snapshot.get("path")
    if not isinstance(path, str) or not SNAPSHOT_PATH_PATTERN.fullmatch(path):
        issues.append(ValidationIssue("$.snapshot.path", "must be a safe immutable v1 snapshot path"))
    digest = snapshot.get("sha256")
    if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
        issues.append(ValidationIssue("$.snapshot.sha256", "must be a lowercase SHA-256 digest"))
    size = snapshot.get("sizeBytes")
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= 524_288:
        issues.append(ValidationIssue("$.snapshot.sizeBytes", "must be between 1 and 524,288"))
    _parse_timestamp(snapshot.get("generatedAt"), "$.snapshot.generatedAt", issues)
    _parse_timestamp(snapshot.get("validUntil"), "$.snapshot.validUntil", issues)
    _walk_json(root, "$", issues)
    if issues:
        raise ContractValidationError(issues)
    return dict(root)
