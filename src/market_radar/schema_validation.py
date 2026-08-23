"""Dependency-free validation for the bounded JSON Schema subset used by v1."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}


def validate_schema(instance: Any, schema_name: str) -> list[str]:
    schema = _load_schema(schema_name)
    return _schema_errors(instance, schema, schema, "$")


def _load_schema(schema_name: str) -> dict[str, Any]:
    cached = _SCHEMA_CACHE.get(schema_name)
    if cached is not None:
        return cached

    configured = os.environ.get("MARKET_RADAR_SCHEMA_DIR")
    candidates = []
    if configured:
        candidates.append(Path(configured) / schema_name)
    candidates.extend(
        (
            Path.cwd() / "schemas" / schema_name,
            Path(__file__).resolve().parents[2] / "schemas" / schema_name,
            Path(sys.prefix) / "share" / "market-radar" / "schemas" / schema_name,
        )
    )
    for path in candidates:
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle, parse_constant=_reject_constant)
        if not isinstance(value, dict):
            raise ValueError(f"schema root must be an object: {path}")
        _SCHEMA_CACHE[schema_name] = value
        return value
    raise FileNotFoundError(f"unable to locate Market Radar schema: {schema_name}")


def _schema_errors(
    instance: Any,
    schema: dict[str, Any],
    root_schema: dict[str, Any],
    path: str,
) -> list[str]:
    errors: list[str] = []
    reference = schema.get("$ref")
    if reference is not None:
        definition = reference.rsplit("/", 1)[-1]
        return _schema_errors(instance, root_schema["$defs"][definition], root_schema, path)

    branches = schema.get("oneOf")
    if branches is not None:
        branch_errors = [_schema_errors(instance, branch, root_schema, path) for branch in branches]
        if sum(not branch for branch in branch_errors) != 1:
            errors.append(f"{path}: expected exactly one oneOf branch")
        return errors

    if "const" in schema and instance != schema["const"]:
        errors.append("{}: expected const {!r}".format(path, schema["const"]))
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: value is not in enum")

    expected_type = schema.get("type")
    type_matches = {
        "object": isinstance(instance, dict),
        "array": isinstance(instance, list),
        "string": isinstance(instance, str),
        "integer": isinstance(instance, int) and not isinstance(instance, bool),
        "number": isinstance(instance, (int, float)) and not isinstance(instance, bool),
        "null": instance is None,
    }
    if expected_type is not None and not type_matches[expected_type]:
        errors.append(f"{path}: expected type {expected_type}")
        return errors

    if isinstance(instance, dict) and expected_type == "object":
        properties = schema.get("properties", {})
        for key in schema.get("required", ()):
            if key not in instance:
                errors.append(f"{path}.{key}: required property is missing")
        if schema.get("additionalProperties") is False:
            for key in set(instance) - set(properties):
                errors.append(f"{path}.{key}: additional property is forbidden")
        for key, value in instance.items():
            if key in properties:
                errors.extend(
                    _schema_errors(
                        value,
                        properties[key],
                        root_schema,
                        f"{path}.{key}",
                    )
                )

    if isinstance(instance, list) and expected_type == "array":
        if len(instance) < schema.get("minItems", 0):
            errors.append(f"{path}: array is shorter than minItems")
        if len(instance) > schema.get("maxItems", float("inf")):
            errors.append(f"{path}: array is longer than maxItems")
        if schema.get("uniqueItems"):
            encoded = [
                json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                for item in instance
            ]
            if len(encoded) != len(set(encoded)):
                errors.append(f"{path}: array items are not unique")
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, value in enumerate(instance):
                errors.extend(
                    _schema_errors(
                        value,
                        item_schema,
                        root_schema,
                        f"{path}[{index}]",
                    )
                )

    if isinstance(instance, str) and expected_type == "string":
        if len(instance) < schema.get("minLength", 0):
            errors.append(f"{path}: string is shorter than minLength")
        if len(instance) > schema.get("maxLength", float("inf")):
            errors.append(f"{path}: string is longer than maxLength")
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, instance) is None:
            errors.append(f"{path}: string does not match pattern")
        if schema.get("format") == "date-time" and not _valid_utc(instance):
            errors.append(f"{path}: invalid date-time")
        if schema.get("format") == "uri":
            parsed = urlsplit(instance)
            if not parsed.scheme or not parsed.netloc:
                errors.append(f"{path}: invalid URI")

    if expected_type in {"integer", "number"} and type_matches[expected_type]:
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: number is below minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: number is above maximum")
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: number is not above exclusiveMinimum")
    return errors


def _valid_utc(value: str) -> bool:
    if not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
        r"(?:\.[0-9]{1,6})?Z",
        value,
    ):
        return False
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return True


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")
