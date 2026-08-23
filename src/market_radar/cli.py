"""Command-line interface for local and automated Market Radar operations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from market_radar.canonical import canonical_json_bytes, sha256_hex
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
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (ContractValidationError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

