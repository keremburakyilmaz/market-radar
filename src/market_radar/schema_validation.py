"""Dependency-free validation for the bounded JSON Schema subset used by v1."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlsplit


_SCHEMA_CACHE: Dict[str, Dict[str, Any]] = {}


def validate_schema(instance: Any, schema_name: str) -> List[str]:
    schema = _load_schema(schema_name)
    return _schema_errors(instance, schema, schema, "$")


def _load_schema(schema_name: str) -> Dict[str, Any]:
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
            raise ValueError("schema root must be an object: {}".format(path))
        _SCHEMA_CACHE[schema_name] = value
        return value
    raise FileNotFoundError("unable to locate Market Radar schema: {}".format(schema_name))


def _schema_errors(
    instance: Any,
    schema: Dict[str, Any],
    root_schema: Dict[str, Any],
    path: str,
) -> List[str]:
    errors: List[str] = []
    reference = schema.get("$ref")
    if reference is not None:
        definition = reference.rsplit("/", 1)[-1]
        return _schema_errors(instance, root_schema["$defs"][definition], root_schema, path)

    branches = schema.get("oneOf")
    if branches is not None:
        branch_errors = [
            _schema_errors(instance, branch, root_schema, path) for branch in branches
        ]
        if sum(not branch for branch in branch_errors) != 1:
            errors.append("{}: expected exactly one oneOf branch".format(path))
        return errors

    if "const" in schema and instance != schema["const"]:
        errors.append("{}: expected const {!r}".format(path, schema["const"]))
    if "enum" in schema and instance not in schema["enum"]:
        errors.append("{}: value is not in enum".format(path))

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
        errors.append("{}: expected type {}".format(path, expected_type))
        return errors

    if isinstance(instance, dict) and expected_type == "object":
        properties = schema.get("properties", {})
        for key in schema.get("required", ()):
            if key not in instance:
                errors.append("{}.{}: required property is missing".format(path, key))
        if schema.get("additionalProperties") is False:
            for key in set(instance) - set(properties):
                errors.append("{}.{}: additional property is forbidden".format(path, key))
        for key, value in instance.items():
            if key in properties:
                errors.extend(
                    _schema_errors(
                        value,
                        properties[key],
                        root_schema,
                        "{}.{}".format(path, key),
                    )
                )

    if isinstance(instance, list) and expected_type == "array":
        if len(instance) < schema.get("minItems", 0):
            errors.append("{}: array is shorter than minItems".format(path))
        if len(instance) > schema.get("maxItems", float("inf")):
            errors.append("{}: array is longer than maxItems".format(path))
        if schema.get("uniqueItems"):
            encoded = [
                json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                for item in instance
            ]
            if len(encoded) != len(set(encoded)):
                errors.append("{}: array items are not unique".format(path))
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, value in enumerate(instance):
                errors.extend(
                    _schema_errors(
                        value,
                        item_schema,
                        root_schema,
                        "{}[{}]".format(path, index),
                    )
                )

    if isinstance(instance, str) and expected_type == "string":
        if len(instance) < schema.get("minLength", 0):
            errors.append("{}: string is shorter than minLength".format(path))
        if len(instance) > schema.get("maxLength", float("inf")):
            errors.append("{}: string is longer than maxLength".format(path))
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, instance) is None:
            errors.append("{}: string does not match pattern".format(path))
        if schema.get("format") == "date-time" and not _valid_utc(instance):
            errors.append("{}: invalid date-time".format(path))
        if schema.get("format") == "uri":
            parsed = urlsplit(instance)
            if not parsed.scheme or not parsed.netloc:
                errors.append("{}: invalid URI".format(path))

    if expected_type in {"integer", "number"} and type_matches[expected_type]:
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append("{}: number is below minimum".format(path))
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append("{}: number is above maximum".format(path))
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            errors.append("{}: number is not above exclusiveMinimum".format(path))
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
    raise ValueError("non-finite JSON number is forbidden: {}".format(value))

