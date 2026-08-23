"""Command-line interface for local and automated Market Radar operations."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from market_radar.canonical import canonical_json_bytes, sha256_hex
from market_radar.collector import collect_sources
from market_radar.domain import CollectionBundle
from market_radar.operations import OperationsService
from market_radar.pipeline import PipelineRunner
from market_radar.publishing import (
    Boto3R2ObjectStore,
    LocalObjectStore,
    ObjectStore,
    PublicationControlError,
    PublicationControlRepository,
    Publisher,
    PublishingError,
    StateRepository,
    StateRepositoryError,
)
from market_radar.timeutil import parse_utc, utc_now
from market_radar.validation import ContractValidationError, validate_manifest, validate_snapshot


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle, parse_constant=lambda value: _reject_constant(value))


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _cmd_validate(args: argparse.Namespace) -> int:
    payload = _read_json(args.path)
    if args.manifest:
        validate_manifest(payload)
    else:
        validate_snapshot(payload, enforce_publish_time=args.publish_gate)
    encoded = canonical_json_bytes(payload)
    print(f"valid sha256={sha256_hex(encoded)} bytes={len(encoded)}")
    return 0


def _cmd_canonicalize(args: argparse.Namespace) -> int:
    payload = _read_json(args.path)
    encoded = canonical_json_bytes(payload)
    if args.output:
        args.output.write_bytes(encoded)
    else:
        sys.stdout.buffer.write(encoded)
    return 0


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"required environment variable is missing: {name}")
    return value


def _r2_store(prefix: str) -> Boto3R2ObjectStore:
    endpoint = os.environ.get("R2_ENDPOINT")
    if not endpoint:
        account_id = _required_env("CLOUDFLARE_ACCOUNT_ID")
        endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
    return Boto3R2ObjectStore(
        bucket=_required_env(f"R2_{prefix}_BUCKET"),
        endpoint_url=endpoint,
        access_key_id=_required_env(f"R2_{prefix}_ACCESS_KEY_ID"),
        secret_access_key=_required_env(f"R2_{prefix}_SECRET_ACCESS_KEY"),
    )


def _cmd_refresh(args: argparse.Namespace) -> int:
    if args.publish and args.as_of:
        raise ValueError("--as-of is restricted to dry runs")
    fixed_time: datetime | None = parse_utc(args.as_of) if args.as_of else None
    clock = (lambda: fixed_time) if fixed_time is not None else utc_now
    fred_api_key = os.environ.get("FRED_API_KEY")

    def collector(at: datetime) -> CollectionBundle:
        return collect_sources(at=at, fred_api_key=fred_api_key)

    if args.target == "r2":
        if not args.publish:
            raise ValueError("the r2 target is only used with --publish")
        public_store = _r2_store("PUBLIC")
        state_store = _r2_store("STATE")
        runner = PipelineRunner(
            collector=collector,
            output_dir=args.output_dir,
            publisher=Publisher(public_store),
            state_repository=StateRepository(state_store),
            control_repository=PublicationControlRepository(state_store),
            clock=clock,
        )
    else:
        local_store = LocalObjectStore(args.object_store_dir)
        control_repository = (
            PublicationControlRepository(LocalObjectStore(args.control_store_dir))
            if args.publish
            else None
        )
        runner = PipelineRunner(
            collector=collector,
            output_dir=args.output_dir,
            publisher=Publisher(local_store),
            control_repository=control_repository,
            local_state_path=args.state_file,
            clock=clock,
        )

    outcome = runner.run(publish=args.publish, slot=args.slot)
    if outcome.no_op:
        print(f"no-op slot={args.slot} report={outcome.report_path}")
    elif outcome.publish_result:
        print(
            f"published key={outcome.publish_result.snapshot_key} "
            f"sha256={outcome.publish_result.snapshot_sha256} "
            f"report={outcome.report_path}"
        )
    else:
        print(f"dry-run candidate={outcome.candidate_path} report={outcome.report_path}")
    return 0


def _cmd_ops(args: argparse.Namespace) -> int:
    actor = args.actor or os.environ.get("GITHUB_ACTOR") or "local-operator"
    if args.operation == "rollback" and not args.snapshot_key:
        raise ValueError("rollback requires --snapshot-key")
    if args.operation != "rollback" and args.snapshot_key:
        raise ValueError("--snapshot-key is accepted only for rollback")

    state_store: ObjectStore
    public_store: ObjectStore | None
    if args.target == "r2":
        state_store = _r2_store("STATE")
        public_store = _r2_store("PUBLIC") if args.operation == "rollback" else None
    else:
        state_store = LocalObjectStore(args.state_object_store_dir)
        public_store = (
            LocalObjectStore(args.public_object_store_dir) if args.operation == "rollback" else None
        )

    service = OperationsService(
        PublicationControlRepository(state_store),
        publisher=Publisher(public_store) if public_store is not None else None,
    )
    if args.operation == "pause":
        result = service.pause(reason=args.reason, actor=actor)
        print(f"publication paused etag={result.control.etag}")
    elif args.operation == "resume":
        result = service.resume(reason=args.reason, actor=actor)
        print(f"publication resumed etag={result.control.etag}")
    else:
        result = service.rollback(
            snapshot_key=args.snapshot_key,
            reason=args.reason,
            actor=actor,
        )
        assert result.promotion is not None
        print(
            f"rollback promoted={result.promotion.snapshot_key} "
            f"previous={result.promotion.previous_snapshot_key} publication=paused"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="market-radar")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate a public snapshot")
    validate.add_argument("path", type=Path)
    validate.add_argument("--manifest", action="store_true")
    validate.add_argument("--publish-gate", action="store_true")
    validate.set_defaults(handler=_cmd_validate)

    canonicalize = subparsers.add_parser("canonicalize", help="write canonical JSON")
    canonicalize.add_argument("path", type=Path)
    canonicalize.add_argument("--output", type=Path)
    canonicalize.set_defaults(handler=_cmd_canonicalize)

    refresh = subparsers.add_parser("refresh", help="collect sources and build a snapshot")
    refresh.add_argument("--publish", action="store_true")
    refresh.add_argument("--target", choices=("local", "r2"), default="local")
    refresh.add_argument("--slot")
    refresh.add_argument("--as-of", help="fixed UTC time for a dry run")
    refresh.add_argument("--output-dir", type=Path, default=Path("out"))
    refresh.add_argument("--object-store-dir", type=Path, default=Path("out/public"))
    refresh.add_argument("--state-file", type=Path, default=Path("state/state.json"))
    refresh.add_argument("--control-store-dir", type=Path, default=Path("state/control"))
    refresh.set_defaults(handler=_cmd_refresh)

    ops = subparsers.add_parser("ops", help="pause, resume, or roll back publication")
    ops.add_argument("operation", choices=("pause", "resume", "rollback"))
    ops.add_argument("--target", choices=("local", "r2"), default="local")
    ops.add_argument("--snapshot-key")
    ops.add_argument("--reason", required=True)
    ops.add_argument("--actor")
    ops.add_argument(
        "--public-object-store-dir",
        type=Path,
        default=Path("out/public"),
    )
    ops.add_argument(
        "--state-object-store-dir",
        type=Path,
        default=Path("state/control"),
    )
    ops.set_defaults(handler=_cmd_ops)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (
        ContractValidationError,
        OSError,
        PublicationControlError,
        PublishingError,
        StateRepositoryError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
