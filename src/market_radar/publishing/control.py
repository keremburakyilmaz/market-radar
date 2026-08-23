"""Private, conflict-safe publication pause and resume state."""

from __future__ import annotations

import html
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast

from market_radar.canonical import canonical_json_bytes
from market_radar.timeutil import format_utc, parse_utc

from .publisher import JSON_CONTENT_TYPE, POINTER_CACHE_CONTROL
from .store import ObjectStore, ObjectStoreConflictError, StoredObject

PUBLICATION_CONTROL_KEY = "control/publication.json"
PUBLICATION_CONTROL_SCHEMA_VERSION = 1
PUBLICATION_CONTROL_MAX_BYTES = 2_048
PUBLICATION_CONTROL_MAX_REASON_LENGTH = 240
PUBLICATION_CONTROL_MAX_ACTOR_LENGTH = 120

_CONTROL_FIELDS = frozenset({"schemaVersion", "paused", "updatedAt", "reason", "actor"})
_UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_HTML_TAG = re.compile(r"<[^<>]*>")


class PublicationControlError(RuntimeError):
    """Base class for private publication-control failures."""


class PublicationControlConflictError(PublicationControlError):
    """Raised when another writer changes publication control first."""


class PublicationControlIntegrityError(PublicationControlError):
    """Raised when stored publication control is malformed or unverifiable."""


@dataclass(frozen=True)
class PublicationControl:
    """The effective publication state.

    ``updated_at``, ``reason``, and ``actor`` are absent only for a bucket that
    has never stored a control object. A missing object deliberately means that
    publication is enabled.
    """

    paused: bool
    updated_at: datetime | None
    reason: str | None
    actor: str | None

    @classmethod
    def default_enabled(cls) -> PublicationControl:
        return cls(paused=False, updated_at=None, reason=None, actor=None)

    @property
    def enabled(self) -> bool:
        return not self.paused


@dataclass(frozen=True)
class LoadedPublicationControl:
    """Publication control plus the ETag required for the next update."""

    control: PublicationControl
    etag: str | None


class PublicationControlRepository:
    """Store one bounded control object in a private object store."""

    def __init__(
        self,
        store: ObjectStore,
        *,
        key: str = PUBLICATION_CONTROL_KEY,
    ) -> None:
        self.store = store
        self.key = key

    def load(self) -> LoadedPublicationControl:
        stored = self.store.get(self.key)
        if stored is None:
            return LoadedPublicationControl(PublicationControl.default_enabled(), None)
        self._verify_metadata(stored)
        control = self._decode(stored.body)
        return LoadedPublicationControl(control, stored.etag)

    def pause(
        self,
        *,
        reason: str,
        actor: str,
        updated_at: datetime,
        previous: LoadedPublicationControl,
    ) -> LoadedPublicationControl:
        return self._set(
            paused=True,
            reason=reason,
            actor=actor,
            updated_at=updated_at,
            previous=previous,
        )

    def resume(
        self,
        *,
        reason: str,
        actor: str,
        updated_at: datetime,
        previous: LoadedPublicationControl,
    ) -> LoadedPublicationControl:
        return self._set(
            paused=False,
            reason=reason,
            actor=actor,
            updated_at=updated_at,
            previous=previous,
        )

    def _set(
        self,
        *,
        paused: bool,
        reason: str,
        actor: str,
        updated_at: datetime,
        previous: LoadedPublicationControl,
    ) -> LoadedPublicationControl:
        if not isinstance(paused, bool):
            raise TypeError("paused must be a boolean")
        if updated_at.tzinfo is None:
            raise ValueError("updated_at must be timezone-aware")

        normalized_time = updated_at.astimezone(timezone.utc).replace(microsecond=0)
        normalized_reason = _sanitize_text(
            reason,
            label="reason",
            maximum_length=PUBLICATION_CONTROL_MAX_REASON_LENGTH,
        )
        normalized_actor = _sanitize_text(
            actor,
            label="actor",
            maximum_length=PUBLICATION_CONTROL_MAX_ACTOR_LENGTH,
        )
        value: dict[str, Any] = {
            "schemaVersion": PUBLICATION_CONTROL_SCHEMA_VERSION,
            "paused": paused,
            "updatedAt": format_utc(normalized_time),
            "reason": normalized_reason,
            "actor": normalized_actor,
        }
        body = canonical_json_bytes(value)
        if len(body) > PUBLICATION_CONTROL_MAX_BYTES:
            raise ValueError("publication control exceeds its byte limit")

        try:
            self.store.put(
                self.key,
                body,
                content_type=JSON_CONTENT_TYPE,
                cache_control=POINTER_CACHE_CONTROL,
                if_match=previous.etag,
                if_none_match=previous.etag is None,
            )
        except ObjectStoreConflictError as error:
            raise PublicationControlConflictError(
                "publication control changed before the conditional write"
            ) from error

        stored = self.store.get(self.key)
        if stored is None or stored.body != body:
            raise PublicationControlIntegrityError(
                "publication control failed readback verification"
            )
        self._verify_metadata(stored)
        control = self._decode(stored.body)
        return LoadedPublicationControl(control, stored.etag)

    @staticmethod
    def _decode(body: bytes) -> PublicationControl:
        if not body or len(body) > PUBLICATION_CONTROL_MAX_BYTES:
            raise PublicationControlIntegrityError("publication control has an invalid object size")
        try:
            decoded = body.decode("utf-8")
            raw_value = json.loads(
                decoded,
                parse_constant=_reject_constant,
                object_pairs_hook=_reject_duplicate_keys,
            )
        except (UnicodeDecodeError, ValueError) as error:
            raise PublicationControlIntegrityError(
                "publication control is not strict JSON"
            ) from error
        if not isinstance(raw_value, dict):
            raise PublicationControlIntegrityError("publication control root must be an object")

        value = cast(dict[str, Any], raw_value)
        if set(value) != _CONTROL_FIELDS:
            raise PublicationControlIntegrityError(
                "publication control fields do not match the schema"
            )
        version = value["schemaVersion"]
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version != PUBLICATION_CONTROL_SCHEMA_VERSION
        ):
            raise PublicationControlIntegrityError(
                "publication control schema version is unsupported"
            )
        paused = value["paused"]
        if not isinstance(paused, bool):
            raise PublicationControlIntegrityError(
                "publication control paused value must be a boolean"
            )
        updated_at = _parse_timestamp(value["updatedAt"])
        reason = _validate_stored_text(
            value["reason"],
            label="reason",
            maximum_length=PUBLICATION_CONTROL_MAX_REASON_LENGTH,
        )
        actor = _validate_stored_text(
            value["actor"],
            label="actor",
            maximum_length=PUBLICATION_CONTROL_MAX_ACTOR_LENGTH,
        )
        return PublicationControl(paused, updated_at, reason, actor)

    def _verify_metadata(self, stored: StoredObject) -> None:
        if stored.content_type != JSON_CONTENT_TYPE:
            raise PublicationControlIntegrityError("publication control Content-Type mismatch")
        if stored.cache_control != POINTER_CACHE_CONTROL:
            raise PublicationControlIntegrityError("publication control Cache-Control mismatch")


def _sanitize_text(value: str, *, label: str, maximum_length: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    normalized = unicodedata.normalize("NFKC", html.unescape(value))
    without_tags = _HTML_TAG.sub(" ", normalized)
    printable = "".join(
        " " if unicodedata.category(character).startswith("C") or character in "<>" else character
        for character in without_tags
    )
    sanitized = " ".join(printable.split())
    if not sanitized:
        raise ValueError(f"{label} must contain visible text")
    if len(sanitized) > maximum_length:
        raise ValueError(f"{label} exceeds {maximum_length} characters")
    return sanitized


def _validate_stored_text(value: Any, *, label: str, maximum_length: int) -> str:
    if not isinstance(value, str):
        raise PublicationControlIntegrityError(f"publication control {label} must be a string")
    try:
        sanitized = _sanitize_text(value, label=label, maximum_length=maximum_length)
    except (TypeError, ValueError) as error:
        raise PublicationControlIntegrityError(f"publication control {label} is invalid") from error
    if sanitized != value:
        raise PublicationControlIntegrityError(f"publication control {label} is not sanitized")
    return value


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not _UTC_TIMESTAMP.fullmatch(value):
        raise PublicationControlIntegrityError(
            "publication control updatedAt must be canonical UTC"
        )
    try:
        parsed = parse_utc(value)
    except ValueError as error:
        raise PublicationControlIntegrityError(
            "publication control updatedAt is invalid"
        ) from error
    if format_utc(parsed) != value:
        raise PublicationControlIntegrityError(
            "publication control updatedAt must be canonical UTC"
        )
    return parsed


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key is not allowed: {key}")
        value[key] = item
    return value
