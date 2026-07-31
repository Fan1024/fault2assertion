#!/usr/bin/env python3
"""Create and verify a fail-closed SHA-256 lock for durable Stage-5 artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

PROGRAM_VERSION = "1.0.0"


class ArtifactLockError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def parse_file_argument(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise ArtifactLockError(
            f"--file must use ROLE=/absolute/or/relative/path syntax: {value!r}"
        )
    role, raw_path = value.split("=", 1)
    role = role.strip()
    raw_path = raw_path.strip()
    if not role or not raw_path:
        raise ArtifactLockError(f"invalid --file argument: {value!r}")
    return role, Path(raw_path).expanduser().resolve()


def build_payload(kind: str, file_args: list[str], output: Path) -> dict[str, Any]:
    if not kind or any(char.isspace() for char in kind):
        raise ArtifactLockError("lock kind must be one non-empty token")
    records: list[dict[str, Any]] = []
    roles: set[str] = set()
    paths: set[Path] = set()
    output = output.resolve()
    for value in file_args:
        role, path = parse_file_argument(value)
        if role in roles:
            raise ArtifactLockError(f"duplicate artifact role: {role}")
        if path in paths:
            raise ArtifactLockError(f"duplicate artifact path: {path}")
        if path == output:
            raise ArtifactLockError("lock output cannot lock itself")
        if not path.is_file() or path.stat().st_size == 0:
            raise ArtifactLockError(f"artifact missing or empty: {role}: {path}")
        roles.add(role)
        paths.add(path)
        records.append(
            {
                "role": role,
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    if not records:
        raise ArtifactLockError("at least one --file is required")
    records.sort(key=lambda item: (item["role"], item["path"]))
    core = {
        "tool_version": PROGRAM_VERSION,
        "artifact_kind": kind,
        "files": records,
    }
    return {
        "schema_version": "1.0",
        "generated_at_utc": utc_now(),
        "kind": "stage5_artifact_lock",
        **core,
        "artifact_digest_sha256": canonical_digest(core),
    }


def verify_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if payload.get("kind") != "stage5_artifact_lock":
        errors.append("lock kind marker mismatch")
    if payload.get("tool_version") != PROGRAM_VERSION:
        errors.append("lock tool version mismatch")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        errors.append("lock contains no artifact files")
        files = []
    core = {
        "tool_version": payload.get("tool_version"),
        "artifact_kind": payload.get("artifact_kind"),
        "files": files,
    }
    if payload.get("artifact_digest_sha256") != canonical_digest(core):
        errors.append("stored artifact digest mismatch")

    roles: set[str] = set()
    paths: set[Path] = set()
    for record in files:
        if not isinstance(record, dict):
            errors.append("invalid artifact file record")
            continue
        role = str(record.get("role", ""))
        path = Path(str(record.get("path", ""))).resolve()
        if not role:
            errors.append(f"artifact role missing for path: {path}")
        elif role in roles:
            errors.append(f"duplicate artifact role in lock: {role}")
        roles.add(role)
        if path in paths:
            errors.append(f"duplicate artifact path in lock: {path}")
        paths.add(path)
        if not path.is_file():
            errors.append(f"locked artifact missing: {role}: {path}")
            continue
        if path.stat().st_size != record.get("size_bytes"):
            errors.append(f"locked artifact size changed: {role}: {path}")
        actual = sha256_file(path)
        if actual != record.get("sha256"):
            errors.append(f"locked artifact SHA changed: {role}: {path}")
    return errors


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--kind", required=True)
    create.add_argument("--file", action="append", required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--force", action="store_true")
    verify = sub.add_parser("verify")
    verify.add_argument("--lock", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "create":
            output = args.output.resolve()
            if output.exists() and not args.force:
                raise ArtifactLockError(
                    f"refusing to overwrite without --force: {output}"
                )
            payload = build_payload(args.kind, args.file, output)
            atomic_write(output, payload)
            print(f"Artifact kind              : {payload['artifact_kind']}")
            print(f"Artifact files             : {len(payload['files'])}")
            print(f"Artifact digest            : {payload['artifact_digest_sha256']}")
            print(f"Artifact lock              : {output}")
            return 0

        path = args.lock.resolve()
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ArtifactLockError("artifact lock must contain one JSON object")
        errors = verify_payload(payload)
        print(f"Artifact-lock errors       : {len(errors)}")
        print(f"Artifact-lock result       : {'PASS' if not errors else 'FAIL'}")
        if errors:
            for error in errors[:50]:
                print(f"  - {error}")
            return 1
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
