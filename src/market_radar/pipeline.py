"""End-to-end collection, validation, publication, and state advancement."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from market_radar.canonical import canonical_json_bytes, sha256_hex
from market_radar.domain import CollectionBundle
from market_radar.publishing import (
    LATEST_KEY,
    LoadedState,
    PublicationControlRepository,
    Publisher,
    PublishResult,
    StateRepository,
)
from market_radar.snapshot import build_snapshot
from market_radar.state import RadarState, load_state, save_state
from market_radar.timeutil import utc_now
from market_radar.validation import validate_manifest, validate_snapshot


class PipelineError(RuntimeError):
    pass


class PublicationSmokeError(PipelineError):
    pass


@dataclass(frozen=True)
class PipelineOutcome:
    run_id: str
    no_op: bool
    published: bool
    candidate_path: Path | None
    report_path: Path
    snapshot: dict[str, Any] | None
    publish_result: PublishResult | None


class PipelineRunner:
    def __init__(
        self,
        *,
        collector: Callable[[datetime], CollectionBundle],
        output_dir: Path,
        publisher: Publisher | None = None,
        state_repository: StateRepository | None = None,
        control_repository: PublicationControlRepository | None = None,
        local_state_path: Path | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if state_repository is not None and local_state_path is not None:
            raise ValueError("configure either remote or local state, not both")
        self.collector = collector
        self.output_dir = Path(output_dir)
        self.publisher = publisher
        self.state_repository = state_repository
        self.control_repository = control_repository
        self.local_state_path = Path(local_state_path) if local_state_path else None
        self.clock = clock

    def run(
        self,
        *,
        publish: bool,
        slot: str | None = None,
    ) -> PipelineOutcome:
        started_at = self._now()
        run_id = "run-{}".format(started_at.strftime("%Y%m%dt%H%M%Sz").lower())
        if publish and self.control_repository is not None:
            control = self.control_repository.load().control
            if control.paused:
                report_path = self._write_report(
                    run_id,
                    {
                        "runId": run_id,
                        "status": "no-op",
                        "reason": "publication-paused",
                        "slot": slot,
                        "published": False,
                    },
                )
                return PipelineOutcome(
                    run_id,
                    True,
                    False,
                    None,
                    report_path,
                    None,
                    None,
                )
        state, loaded_remote = self._load_state()
        if publish and slot and slot in state.successful_slots:
            report_path = self._write_report(
                run_id,
                {
                    "runId": run_id,
                    "status": "no-op",
                    "reason": "slot-already-published",
                    "slot": slot,
                    "published": False,
                },
            )
            return PipelineOutcome(run_id, True, False, None, report_path, None, None)

        bundle = self.collector(started_at)
        completed_at = self._now()
        build = build_snapshot(
            bundle,
            state,
            generated_at=completed_at,
            started_at=started_at,
            run_id=run_id,
            successful_slot=slot if publish else None,
        )
        validate_snapshot(build.snapshot, now=completed_at, enforce_publish_time=publish)
        candidate_path = self._write_candidate(build.snapshot)

        publish_result = None
        if publish:
            if self.publisher is None:
                raise PipelineError("publication was requested without an object-store publisher")
            publish_result = self.publisher.publish(build.snapshot)
            self._smoke_published_snapshot(publish_result)
            self._save_state(
                build.next_state,
                created_at=completed_at,
                snapshot_key=publish_result.snapshot_key,
                loaded_remote=loaded_remote,
            )

        report = {
            "runId": run_id,
            "status": "success",
            "published": publish,
            "slot": slot,
            "generatedAt": build.snapshot["generatedAt"],
            "pipelineStatus": build.snapshot["pipeline"]["status"],
            "sourceCoverage": build.snapshot["pipeline"]["coverage"],
            "snapshotKey": publish_result.snapshot_key if publish_result else None,
            "snapshotSha256": publish_result.snapshot_sha256 if publish_result else None,
        }
        report_path = self._write_report(run_id, report)
        return PipelineOutcome(
            run_id,
            False,
            publish,
            candidate_path,
            report_path,
            build.snapshot,
            publish_result,
        )

    def _load_state(self) -> tuple[RadarState, LoadedState | None]:
        if self.state_repository is not None:
            loaded = self.state_repository.load()
            return loaded.state, loaded
        if self.local_state_path is not None:
            return load_state(self.local_state_path), None
        return RadarState(), None

    def _save_state(
        self,
        state: RadarState,
        *,
        created_at: datetime,
        snapshot_key: str,
        loaded_remote: LoadedState | None,
    ) -> None:
        if self.state_repository is not None:
            if loaded_remote is None:
                raise PipelineError("remote state was not loaded before publication")
            self.state_repository.save(
                state,
                created_at=created_at,
                source_snapshot_key=snapshot_key,
                previous=loaded_remote,
            )
        elif self.local_state_path is not None:
            save_state(self.local_state_path, state)

    def _smoke_published_snapshot(self, result: PublishResult) -> None:
        if self.publisher is None:
            raise PipelineError("publisher is unavailable")
        latest_object = self.publisher.store.get(LATEST_KEY)
        snapshot_object = self.publisher.store.get(result.snapshot_key)
        if latest_object is None or snapshot_object is None:
            raise PublicationSmokeError("published objects are missing during smoke verification")
        try:
            manifest = json.loads(
                latest_object.body.decode("utf-8"), parse_constant=_reject_constant
            )
            snapshot = json.loads(
                snapshot_object.body.decode("utf-8"), parse_constant=_reject_constant
            )
        except (UnicodeDecodeError, ValueError) as error:
            raise PublicationSmokeError("published object is not strict JSON") from error
        validate_manifest(manifest)
        validate_snapshot(snapshot)
        pointer = manifest["snapshot"]
        if pointer["path"] != result.snapshot_key:
            raise PublicationSmokeError("latest manifest references a different snapshot")
        if pointer["sizeBytes"] != len(snapshot_object.body):
            raise PublicationSmokeError("latest manifest size does not match the snapshot")
        if pointer["sha256"] != sha256_hex(snapshot_object.body):
            raise PublicationSmokeError("latest manifest hash does not match the snapshot")

    def _write_candidate(self, snapshot: dict[str, Any]) -> Path:
        generated = snapshot["generatedAt"].replace(":", "-")
        path = self.output_dir / "candidates" / f"{generated}.json"
        _atomic_write(path, canonical_json_bytes(snapshot))
        return path

    def _write_report(self, run_id: str, report: dict[str, Any]) -> Path:
        path = self.output_dir / "reports" / f"{run_id}.json"
        _atomic_write(path, canonical_json_bytes(report))
        return path

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("pipeline clock must return a timezone-aware timestamp")
        return value.astimezone(timezone.utc).replace(microsecond=0)


def _atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=str(path.parent), prefix=".run-")
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
    raise ValueError(f"non-finite JSON value is forbidden: {value}")
