"""Content-addressed durable state with a conditional latest pointer."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from market_radar.canonical import canonical_json_bytes, sha256_hex
from market_radar.state import RadarState
from market_radar.timeutil import format_utc

from .publisher import JSON_CONTENT_TYPE, POINTER_CACHE_CONTROL, SNAPSHOT_CACHE_CONTROL
from .store import ObjectStore, ObjectStoreConflictError, StoredObject

STATE_LATEST_KEY = "state/latest.json"
_CHECKPOINT_PATH = re.compile(
    r"^state/checkpoints/\d{4}/\d{2}/\d{2}/[A-Za-z0-9._-]+-[a-f0-9]{64}\.json$"
)


class StateRepositoryError(RuntimeError):
    pass


class StateConflictError(StateRepositoryError):
    pass


class StateIntegrityError(StateRepositoryError):
    pass


@dataclass(frozen=True)
class LoadedState:
    state: RadarState
    pointer_etag: str | None
    checkpoint_key: str | None


@dataclass(frozen=True)
class SavedState:
    checkpoint_key: str
    checkpoint_sha256: str
    pointer_etag: str
    checkpoint_created: bool


class StateRepository:
    def __init__(self, store: ObjectStore, *, latest_key: str = STATE_LATEST_KEY) -> None:
        self.store = store
        self.latest_key = latest_key

    def load(self) -> LoadedState:
        pointer_object = self.store.get(self.latest_key)
        if pointer_object is None:
            return LoadedState(RadarState(), None, None)

        pointer = self._parse_json(pointer_object.body, "state pointer")
        if pointer.get("stateManifestVersion") != 1:
            raise StateIntegrityError("unsupported state manifest version")
        checkpoint = pointer.get("checkpoint")
        if not isinstance(checkpoint, dict):
            raise StateIntegrityError("state pointer checkpoint is missing")

        key = checkpoint.get("path")
        expected_digest = checkpoint.get("sha256")
        expected_size = checkpoint.get("sizeBytes")
        if not isinstance(key, str) or not _CHECKPOINT_PATH.fullmatch(key):
            raise StateIntegrityError("state checkpoint path is unsafe")
        if not isinstance(expected_digest, str) or not re.fullmatch(
            r"[a-f0-9]{64}", expected_digest
        ):
            raise StateIntegrityError("state checkpoint digest is invalid")
        if (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 1
        ):
            raise StateIntegrityError("state checkpoint size is invalid")

        checkpoint_object = self.store.get(key)
        if checkpoint_object is None:
            raise StateIntegrityError("state checkpoint is missing")
        if len(checkpoint_object.body) != expected_size:
            raise StateIntegrityError("state checkpoint size mismatch")
        if sha256_hex(checkpoint_object.body) != expected_digest:
            raise StateIntegrityError("state checkpoint hash mismatch")
        state_value = self._parse_json(checkpoint_object.body, "state checkpoint")
        try:
            state = RadarState.from_dict(state_value)
        except ValueError as error:
            raise StateIntegrityError("state checkpoint contract is invalid") from error
        return LoadedState(state, pointer_object.etag, key)

    def save(
        self,
        state: RadarState,
        *,
        created_at: datetime,
        source_snapshot_key: str,
        previous: LoadedState,
    ) -> SavedState:
        if created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        if not source_snapshot_key.startswith("v1/snapshots/"):
            raise ValueError("source_snapshot_key must reference an immutable public snapshot")

        created_at = created_at.astimezone(timezone.utc).replace(microsecond=0)
        checkpoint_body = canonical_json_bytes(state.public_dict())
        digest = sha256_hex(checkpoint_body)
        checkpoint_key = self._checkpoint_key(created_at, digest)
        checkpoint_created = self._ensure_checkpoint(
            checkpoint_key, checkpoint_body, expected_digest=digest
        )

        pointer_body = canonical_json_bytes(
            {
                "stateManifestVersion": 1,
                "createdAt": format_utc(created_at),
                "checkpoint": {
                    "path": checkpoint_key,
                    "sha256": digest,
                    "sizeBytes": len(checkpoint_body),
                },
                "previousCheckpointPath": previous.checkpoint_key,
                "sourceSnapshotPath": source_snapshot_key,
            }
        )
        try:
            self.store.put(
                self.latest_key,
                pointer_body,
                content_type=JSON_CONTENT_TYPE,
                cache_control=POINTER_CACHE_CONTROL,
                if_match=previous.pointer_etag,
                if_none_match=previous.pointer_etag is None,
            )
        except ObjectStoreConflictError as error:
            raise StateConflictError("state pointer changed during publication") from error

        stored_pointer = self.store.get(self.latest_key)
        if stored_pointer is None or stored_pointer.body != pointer_body:
            raise StateIntegrityError("state pointer failed readback verification")
        return SavedState(checkpoint_key, digest, stored_pointer.etag, checkpoint_created)

    def _ensure_checkpoint(self, key: str, body: bytes, *, expected_digest: str) -> bool:
        existing = self.store.get(key)
        if existing is not None:
            self._verify_checkpoint(existing, body, expected_digest)
            return False
        try:
            self.store.put(
                key,
                body,
                content_type=JSON_CONTENT_TYPE,
                cache_control=SNAPSHOT_CACHE_CONTROL,
                if_none_match=True,
            )
            created = True
        except ObjectStoreConflictError:
            created = False
        stored = self.store.get(key)
        if stored is None:
            raise StateIntegrityError("state checkpoint is missing after write")
        self._verify_checkpoint(stored, body, expected_digest)
        return created

    @staticmethod
    def _verify_checkpoint(stored: StoredObject, body: bytes, digest: str) -> None:
        if stored.body != body or sha256_hex(stored.body) != digest:
            raise StateIntegrityError("state checkpoint failed readback verification")
        if stored.content_type != JSON_CONTENT_TYPE:
            raise StateIntegrityError("state checkpoint Content-Type mismatch")
        if stored.cache_control != SNAPSHOT_CACHE_CONTROL:
            raise StateIntegrityError("state checkpoint Cache-Control mismatch")

    @staticmethod
    def _parse_json(body: bytes, label: str) -> dict[str, Any]:
        try:
            value = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError) as error:
            raise StateIntegrityError(f"{label} is not strict JSON") from error
        if not isinstance(value, dict):
            raise StateIntegrityError(f"{label} root must be an object")
        return value

    @staticmethod
    def _checkpoint_key(created_at: datetime, digest: str) -> str:
        return "state/checkpoints/{}/{}-{}.json".format(
            created_at.strftime("%Y/%m/%d"),
            created_at.strftime("%Y-%m-%dT%H-%M-%SZ"),
            digest,
        )


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")
