"""Fail-closed schema and semantic validation for public v1 payloads."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from market_radar.schema_validation import validate_schema
from market_radar.timeutil import parse_utc

SNAPSHOT_PATH_PATTERN = re.compile(
    r"^v1/snapshots/"
    r"(?P<year>\d{4})/(?P<month>\d{2})/(?P<day>\d{2})/"
    r"(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z)-"
    r"(?P<sha256>[a-f0-9]{64})\.json$"
)


@dataclass(frozen=True)
class ValidationIssue:
    path: str
    message: str


class ContractValidationError(ValueError):
    def __init__(self, issues: Sequence[ValidationIssue]):
        self.issues = tuple(issues)
        detail = "; ".join(f"{issue.path}: {issue.message}" for issue in issues)
        super().__init__(detail)


def _schema_gate(value: Any, schema_name: str) -> None:
    raw_errors = validate_schema(value, schema_name)
    if not raw_errors:
        return
    issues = []
    for error in raw_errors:
        path, separator, message = error.partition(": ")
        issues.append(ValidationIssue(path if separator else "$", message or error))
    raise ContractValidationError(issues)


def _walk_json(value: Any, path: str, issues: list[ValidationIssue]) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        issues.append(ValidationIssue(path, "must not contain NaN or infinity"))
    elif isinstance(value, str):
        lowered = value.lower()
        if "<script" in lowered or "javascript:" in lowered:
            issues.append(ValidationIssue(path, "contains unsafe executable content"))
    elif isinstance(value, Mapping):
        for key, child in value.items():
            _walk_json(child, f"{path}.{key}", issues)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_json(child, f"{path}[{index}]", issues)


def _timestamp(value: str, path: str, issues: list[ValidationIssue]) -> datetime | None:
    try:
        return parse_utc(value)
    except (TypeError, ValueError):
        issues.append(ValidationIssue(path, "must be canonical ISO-8601 UTC"))
        return None


def validate_snapshot(
    snapshot: Any,
    *,
    now: datetime | None = None,
    enforce_publish_time: bool = False,
) -> dict[str, Any]:
    """Validate the closed schema plus cross-field and scoring invariants."""

    _schema_gate(snapshot, "snapshot.v1.schema.json")
    root = dict(snapshot)
    issues: list[ValidationIssue] = []

    generated_at = _timestamp(root["generatedAt"], "$.generatedAt", issues)
    valid_until = _timestamp(root["validUntil"], "$.validUntil", issues)
    pipeline = root["pipeline"]
    started_at = _timestamp(pipeline["startedAt"], "$.pipeline.startedAt", issues)
    completed_at = _timestamp(pipeline["completedAt"], "$.pipeline.completedAt", issues)
    if generated_at and valid_until and valid_until <= generated_at:
        issues.append(ValidationIssue("$.validUntil", "must be after generatedAt"))
    if started_at and completed_at and completed_at < started_at:
        issues.append(ValidationIssue("$.pipeline.completedAt", "must not precede startedAt"))
    if completed_at and generated_at and completed_at > generated_at + timedelta(minutes=5):
        issues.append(ValidationIssue("$.pipeline.completedAt", "must not be after generation"))

    if enforce_publish_time and generated_at:
        clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if generated_at > clock + timedelta(minutes=5):
            issues.append(ValidationIssue("$.generatedAt", "must not be in the future"))
        if generated_at < clock - timedelta(hours=2):
            issues.append(ValidationIssue("$.generatedAt", "candidate is too old to publish"))

    coverage = pipeline["coverage"]
    counted_sources = (
        coverage["successfulSources"] + coverage["staleSources"] + coverage["failedSources"]
    )
    if counted_sources != coverage["expectedSources"]:
        issues.append(
            ValidationIssue(
                "$.pipeline.coverage",
                "successful, stale, and failed source counts must equal expectedSources",
            )
        )
    if coverage["expectedSources"] != len(root["sources"]):
        issues.append(
            ValidationIssue("$.pipeline.coverage.expectedSources", "must equal sources length")
        )

    actual_status_counts = {
        "healthy": sum(source["status"] == "healthy" for source in root["sources"]),
        "stale": sum(source["status"] == "stale" for source in root["sources"]),
        "unavailable": sum(source["status"] == "unavailable" for source in root["sources"]),
    }
    expected_counts = {
        "healthy": coverage["successfulSources"],
        "stale": coverage["staleSources"],
        "unavailable": coverage["failedSources"],
    }
    if actual_status_counts != expected_counts:
        issues.append(ValidationIssue("$.pipeline.coverage", "does not match source health states"))

    conditions = root["macroConditions"]
    drivers = conditions["drivers"]
    weight_sum = sum(float(driver["weight"]) for driver in drivers)
    if abs(weight_sum - 1.0) > 0.001:
        issues.append(ValidationIssue("$.macroConditions.drivers", "weights must sum to one"))
    contribution_sum = sum(float(driver["contributionPoints"]) for driver in drivers)
    expected_score = max(
        0.0,
        min(100.0, float(conditions["methodology"]["baselineScore"]) + contribution_sum),
    )
    if abs(float(conditions["score"]) - expected_score) > 0.11:
        issues.append(
            ValidationIssue(
                "$.macroConditions.score", "must equal baseline plus driver contributions"
            )
        )
    score = float(conditions["score"])
    expected_label = "supportive" if score < 40 else "balanced" if score < 60 else "restrictive"
    if conditions["label"] != expected_label:
        issues.append(ValidationIssue("$.macroConditions.label", "does not match score band"))

    indicator_ids = [indicator["id"] for indicator in root["indicators"]]
    if len(indicator_ids) != len(set(indicator_ids)):
        issues.append(ValidationIssue("$.indicators", "indicator IDs must be unique"))
    indicator_id_set = set(indicator_ids)
    for index, driver in enumerate(drivers):
        if driver["indicatorId"] not in indicator_id_set:
            issues.append(
                ValidationIssue(
                    f"$.macroConditions.drivers[{index}].indicatorId",
                    "must reference a published indicator",
                )
            )
    for index, indicator in enumerate(root["indicators"]):
        observed = _timestamp(indicator["observedAt"], f"$.indicators[{index}].observedAt", issues)
        retrieved = _timestamp(
            indicator["retrievedAt"], f"$.indicators[{index}].retrievedAt", issues
        )
        if observed and retrieved and observed > retrieved + timedelta(minutes=5):
            issues.append(
                ValidationIssue(
                    f"$.indicators[{index}].observedAt",
                    "must not be after retrieval",
                )
            )

    story_ids = [story["id"] for story in root["stories"]]
    if len(story_ids) != len(set(story_ids)):
        issues.append(ValidationIssue("$.stories", "story IDs must be unique"))
    unknown_digest_ids = set(root["digest"]["storyIds"]) - set(story_ids)
    if unknown_digest_ids:
        issues.append(ValidationIssue("$.digest.storyIds", "must reference published stories"))

    calendar_ids = {event["id"] for event in root["calendar"]}
    commentary_evidence_ids = indicator_id_set | set(story_ids) | calendar_ids
    commentary = root["digest"].get("commentary")
    if commentary is not None:
        for section_name in ("dataRead", "newsRead", "watchNext"):
            unknown_evidence_ids = (
                set(commentary[section_name]["evidenceIds"]) - commentary_evidence_ids
            )
            if unknown_evidence_ids:
                issues.append(
                    ValidationIssue(
                        f"$.digest.commentary.{section_name}.evidenceIds",
                        "must reference a published indicator, story, or calendar event",
                    )
                )

    source_ids = [source["id"] for source in root["sources"]]
    if len(source_ids) != len(set(source_ids)):
        issues.append(ValidationIssue("$.sources", "source IDs must be unique"))

    _walk_json(root, "$", issues)
    if issues:
        raise ContractValidationError(issues)
    return root


def validate_manifest(manifest: Any) -> dict[str, Any]:
    _schema_gate(manifest, "manifest.v1.schema.json")
    root = dict(manifest)
    issues: list[ValidationIssue] = []
    published_at = _timestamp(root["publishedAt"], "$.publishedAt", issues)
    pointer = root["snapshot"]
    generated_at = _timestamp(pointer["generatedAt"], "$.snapshot.generatedAt", issues)
    valid_until = _timestamp(pointer["validUntil"], "$.snapshot.validUntil", issues)
    if generated_at and valid_until and valid_until <= generated_at:
        issues.append(ValidationIssue("$.snapshot.validUntil", "must be after generatedAt"))
    if generated_at and published_at and published_at < generated_at:
        issues.append(ValidationIssue("$.publishedAt", "must not precede snapshot generation"))

    match = SNAPSHOT_PATH_PATTERN.fullmatch(pointer["path"])
    if match is None:
        issues.append(ValidationIssue("$.snapshot.path", "must be an immutable snapshot path"))
    else:
        if match.group("sha256") != pointer["sha256"]:
            issues.append(ValidationIssue("$.snapshot.path", "digest must match snapshot.sha256"))
        if generated_at:
            expected_directory = generated_at.strftime("%Y/%m/%d")
            actual_directory = "{}/{}/{}".format(
                match.group("year"), match.group("month"), match.group("day")
            )
            if actual_directory != expected_directory:
                issues.append(ValidationIssue("$.snapshot.path", "date must match generatedAt"))
            expected_timestamp = generated_at.strftime("%Y-%m-%dT%H-%M-%SZ")
            if match.group("timestamp") != expected_timestamp:
                issues.append(
                    ValidationIssue("$.snapshot.path", "timestamp must match generatedAt")
                )

    _walk_json(root, "$", issues)
    if issues:
        raise ContractValidationError(issues)
    return root
