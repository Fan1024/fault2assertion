#!/usr/bin/env python3
"""Verify that the current local Stage-5 state still matches a saved lock.

This tool does not run simulation and does not regenerate artifacts.  It checks
Git provenance plus the hashes of the guard, Stage-5 tool, campaign, monitors,
manifests, and recorded source files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


class LockError(RuntimeError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return sha256_bytes(encoded)


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LockError(f"lock not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise LockError(f"invalid lock JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise LockError("lock must contain one JSON object")
    return payload


def run_git(repo_root: Path, *args: str, binary: bool = False) -> bytes | str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=not binary,
    )
    if completed.returncode != 0:
        stderr = completed.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        raise LockError(
            f"git command failed: git {' '.join(args)}\n{str(stderr).strip()}"
        )
    return completed.stdout


def current_repo_state(repo_root: Path) -> dict[str, Any]:
    status = str(run_git(repo_root, "status", "--porcelain=v1", "--untracked-files=all"))
    diff_bytes = bytes(run_git(repo_root, "diff", "--binary", "HEAD", binary=True))
    untracked_text = str(
        run_git(repo_root, "ls-files", "--others", "--exclude-standard")
    )
    untracked_files: list[dict[str, Any]] = []
    for relative in sorted(line for line in untracked_text.splitlines() if line):
        path = (repo_root / relative).resolve()
        if path.is_file():
            untracked_files.append(
                {
                    "path": relative,
                    "sha256": sha256_file(path),
                    "size_bytes": path.stat().st_size,
                }
            )
    return {
        "head": str(run_git(repo_root, "rev-parse", "HEAD")).strip(),
        "branch": str(run_git(repo_root, "branch", "--show-current")).strip(),
        "working_tree_dirty": bool(status.strip()),
        "git_status_sha256": sha256_text(status),
        "tracked_diff_sha256": sha256_bytes(diff_bytes),
        "untracked_files_digest_sha256": canonical_json_digest(untracked_files),
    }


def verify_file_record(
    record: Mapping[str, Any],
    *,
    label: str,
    errors: list[str],
    path_key: str = "path",
    sha_key: str = "sha256",
) -> None:
    raw_path = record.get(path_key)
    expected = record.get(sha_key)
    if not raw_path:
        errors.append(f"{label}: lock has no path")
        return
    path = Path(str(raw_path)).resolve()
    if not path.is_file():
        errors.append(f"{label}: file missing: {path}")
        return
    actual = sha256_file(path)
    if actual != expected:
        errors.append(
            f"{label}: SHA-256 mismatch: expected={expected}, actual={actual}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify the current local state against a Stage-5 lock."
    )
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    lock_path = args.lock.resolve()
    repo_root = args.repo_root.resolve()

    try:
        lock = load_json(lock_path)
        current = current_repo_state(repo_root)
    except LockError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    errors: list[str] = []
    if lock.get("kind") != "stage5_version_lock":
        errors.append(f"invalid lock kind: {lock.get('kind')!r}")
    if lock.get("status") != "PASS":
        errors.append(f"lock was not created from a PASS audit: {lock.get('status')!r}")

    expected_repo = lock.get("repository")
    if not isinstance(expected_repo, dict):
        errors.append("lock missing repository provenance")
        expected_repo = {}
    for key in (
        "head",
        "branch",
        "working_tree_dirty",
        "git_status_sha256",
        "tracked_diff_sha256",
        "untracked_files_digest_sha256",
    ):
        if expected_repo.get(key) != current.get(key):
            errors.append(
                f"repository {key} mismatch: "
                f"expected={expected_repo.get(key)!r}, actual={current.get(key)!r}"
            )

    guard = lock.get("guard")
    tool = lock.get("stage5_tool")
    campaign = lock.get("campaign")
    if isinstance(guard, dict):
        verify_file_record(guard, label="version guard", errors=errors)
    else:
        errors.append("lock missing guard record")
    if isinstance(tool, dict):
        verify_file_record(tool, label="Stage-5 tool", errors=errors)
    else:
        errors.append("lock missing Stage-5 tool record")
    if isinstance(campaign, dict):
        verify_file_record(
            campaign,
            label="campaign",
            errors=errors,
            sha_key="file_sha256",
        )
    else:
        errors.append("lock missing campaign record")

    for index, record in enumerate(lock.get("checked_source_files", [])):
        if isinstance(record, dict):
            verify_file_record(
                record,
                label=f"source file {index}: {record.get('label')}",
                errors=errors,
            )
        else:
            errors.append(f"invalid checked_source_files record at index {index}")

    for index, record in enumerate(lock.get("monitors", [])):
        if isinstance(record, dict):
            verify_file_record(record, label=f"monitor {index}", errors=errors)
        else:
            errors.append(f"invalid monitor record at index {index}")

    for index, record in enumerate(lock.get("manifests", [])):
        if isinstance(record, dict):
            verify_file_record(record, label=f"manifest {index}", errors=errors)
        else:
            errors.append(f"invalid manifest record at index {index}")

    print()
    print("=" * 78)
    print("Fault2Assertion Stage-5 Lock Verification")
    print("=" * 78)
    print(f"Lock                  : {lock_path}")
    print(f"Repository HEAD       : {current['head']}")
    print(f"Working tree dirty    : {current['working_tree_dirty']}")
    print(f"Errors                : {len(errors)}")
    print(f"Result                : {'PASS' if not errors else 'FAIL'}")
    print("=" * 78)

    if errors:
        print("\nErrors:")
        for error in errors[:40]:
            print(f"  - {error}")
        if len(errors) > 40:
            print(f"  ... {len(errors) - 40} additional errors omitted")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
