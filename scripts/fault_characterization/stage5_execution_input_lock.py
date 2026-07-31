#!/usr/bin/env python3
"""Create or verify the exact Stage-5 mini-smoke execution input lock.

This lock covers inputs outside normal Git tracking, including firmware, ELF,
standard-cell models, the external CV32E40P testbench/source tree actually used
by the runner, the mapped netlist, runner tools, and selected monitors.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

PROGRAM_VERSION = "1.0.0"


class LockError(RuntimeError):
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


def require_file(path: Path, label: str) -> Path:
    path = path.expanduser().resolve()
    if not path.is_file() or path.stat().st_size == 0:
        raise LockError(f"{label} missing or empty: {path}")
    return path


def git_value(root: Path, *args: str) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def git_metadata(root: Path) -> dict[str, Any]:
    status = git_value(root, "status", "--porcelain=v1", "--untracked-files=all")
    return {
        "root": str(root),
        "head": git_value(root, "rev-parse", "HEAD"),
        "branch": git_value(root, "branch", "--show-current"),
        "status_sha256": (
            hashlib.sha256((status or "").encode("utf-8")).hexdigest()
            if status is not None
            else None
        ),
        "dirty": bool(status),
    }


def directory_inventory(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        return {"root": str(root), "exists": False, "files": []}
    files: list[dict[str, Any]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if ".git" in path.parts:
            continue
        files.append(
            {
                "relative_path": str(path.relative_to(root)),
                "resolved_path": str(path.resolve()),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return {"root": str(root), "exists": True, "files": files}


def file_record(path: Path, role: str) -> dict[str, Any]:
    path = require_file(path, role)
    return {
        "role": role,
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    repo = args.repo_root.resolve()
    cv = args.cv32e40p_home.resolve()
    cell = require_file(args.cell_model, "standard-cell model")
    mapped = require_file(args.mapped_netlist, "mapped netlist")
    monitor_paths = [require_file(path, "Stage-5 monitor") for path in args.monitor]

    tb = cv / "verification/shared/tb"
    explicit: list[tuple[str, Path]] = [
        ("firmware_hex", repo / "build/cv32e40p/crc32/crc32.hex"),
        ("firmware_elf", repo / "build/cv32e40p/crc32/crc32.elf"),
        ("setup_env", repo / "scripts/setup_env.sh"),
        ("stage5_common_runner", repo / "scripts/lib/xrun_stage5_common.sh"),
        ("stage5_golden_wrapper", repo / "scripts/run_xrun_stage5_golden.sh"),
        ("stage5_fault_wrapper", repo / "scripts/run_xrun_stage5_fault.sh"),
        ("stage5_verdict", repo / "scripts/fault_characterization/stage5_verdict.py"),
        (
            "stage5_reproduction_bundle",
            repo / "scripts/fault_characterization/stage5_reproduction_bundle.py",
        ),
        ("stage5_fault_materializer", repo / "scripts/fault_characterization/stage5_faults.py"),
        ("netlist_preparation", repo / "platform/cv32e40p/prepare_netlist.py"),
        (
            "tb_subsystem",
            repo / "platform/cv32e40p/tb/cv32e40p_tb_subsystem.sv",
        ),
        ("standard_cell_model", cell),
        ("mapped_netlist", mapped),
        ("cv_pkg_apu", cv / "rtl/include/cv32e40p_apu_core_pkg.sv"),
        ("cv_pkg_core", cv / "rtl/include/cv32e40p_pkg.sv"),
        ("cv_pkg_fpu", cv / "rtl/include/cv32e40p_fpu_pkg.sv"),
        ("tb_perturbation_pkg", tb / "include/perturbation_pkg.sv"),
        ("tb_amo_shim", tb / "amo_shim.sv"),
        (
            "tb_random_interrupt",
            tb / "cv32e40p_random_interrupt_generator.sv",
        ),
        ("tb_dp_ram", tb / "dp_ram.sv"),
        ("tb_gnt_stall", tb / "riscv_gnt_stall.sv"),
        ("tb_rvalid_stall", tb / "riscv_rvalid_stall.sv"),
        ("tb_mm_ram", tb / "mm_ram.sv"),
        ("tb_top", tb / "tb_top.sv"),
    ]
    for index, monitor in enumerate(monitor_paths):
        explicit.append((f"stage5_monitor_{index:02d}", monitor))

    records: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for role, path in explicit:
        resolved = require_file(path, role)
        if resolved in seen:
            continue
        seen.add(resolved)
        records.append(file_record(resolved, role))

    include_roots = [
        cv / "rtl/include",
        cv / "bhv",
        cv / "bhv/include",
        cv / "sva",
        tb / "include",
    ]
    directory_records = [directory_inventory(path) for path in include_roots]
    core = {
        "tool_version": PROGRAM_VERSION,
        "repository_root": str(repo),
        "cv32e40p_home": str(cv),
        "git": {
            "fault2assertion": git_metadata(repo),
            "cv32e40p": git_metadata(cv),
        },
        "files": records,
        "include_directories": directory_records,
    }
    return {
        "schema_version": "1.0",
        "generated_at_utc": utc_now(),
        "kind": "stage5_execution_input_lock",
        **core,
        "execution_input_digest_sha256": canonical_digest(core),
    }


def verify_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if payload.get("kind") != "stage5_execution_input_lock":
        errors.append("lock kind mismatch")
    if payload.get("tool_version") != PROGRAM_VERSION:
        errors.append("lock tool version mismatch")

    core = {
        key: payload.get(key)
        for key in (
            "tool_version",
            "repository_root",
            "cv32e40p_home",
            "git",
            "files",
            "include_directories",
        )
    }
    if payload.get("execution_input_digest_sha256") != canonical_digest(core):
        errors.append("stored execution-input digest mismatch")

    files = payload.get("files")
    if not isinstance(files, list) or not files:
        errors.append("lock contains no files")
        files = []
    for record in files:
        if not isinstance(record, dict):
            errors.append("invalid file record")
            continue
        path = Path(str(record.get("path", ""))).resolve()
        role = str(record.get("role", "unknown"))
        if not path.is_file():
            errors.append(f"locked file missing: {role}: {path}")
            continue
        if path.stat().st_size != record.get("size_bytes"):
            errors.append(f"locked file size changed: {role}: {path}")
        actual = sha256_file(path)
        if actual != record.get("sha256"):
            errors.append(f"locked file SHA changed: {role}: {path}")

    directories = payload.get("include_directories")
    if not isinstance(directories, list):
        errors.append("include-directory inventory missing")
        directories = []
    for stored in directories:
        if not isinstance(stored, dict):
            errors.append("invalid include-directory record")
            continue
        current = directory_inventory(Path(str(stored.get("root", ""))))
        if current != stored:
            errors.append(f"include-directory inventory changed: {stored.get('root')}")

    for name, metadata in (payload.get("git") or {}).items():
        if not isinstance(metadata, dict):
            errors.append(f"invalid git metadata: {name}")
            continue
        current = git_metadata(Path(str(metadata.get("root", ""))))
        # The F2A repository has a separate full dirty-worktree lock.  Here we
        # still require both repositories' commit and status digest unchanged.
        for key in ("head", "branch", "status_sha256", "dirty"):
            if current.get(key) != metadata.get(key):
                errors.append(f"git state changed: {name}.{key}")
    return errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--repo-root", type=Path, required=True)
    create.add_argument("--cv32e40p-home", type=Path, required=True)
    create.add_argument("--cell-model", type=Path, required=True)
    create.add_argument("--mapped-netlist", type=Path, required=True)
    create.add_argument("--monitor", type=Path, action="append", required=True)
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
                raise LockError(f"refusing to overwrite without --force: {output}")
            payload = build_payload(args)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            print(f"Execution-input files       : {len(payload['files'])}")
            print(f"Include-directory roots     : {len(payload['include_directories'])}")
            print(f"Execution-input digest      : {payload['execution_input_digest_sha256']}")
            print(f"Execution-input lock        : {output}")
            return 0

        payload = json.loads(args.lock.resolve().read_text(encoding="utf-8"))
        errors = verify_payload(payload)
        print(f"Execution-input errors      : {len(errors)}")
        print(f"Execution-input result      : {'PASS' if not errors else 'FAIL'}")
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
