#!/usr/bin/env python3
"""Fail-closed Stage-5 version, digest, artifact, monitor, and provenance audit.

The audit never invokes a simulator.  It validates one Stage-5 campaign and any
number of generated monitors/manifests, then optionally writes a reproducibility
lock.  Digest formulas intentionally mirror stage5_faults.py:

* fault spec digest: all fields except generated_at_utc and the stored digest
  field itself (the stored field does not exist when the generator calculates it)
* campaign digest: source_stage4, mapped_netlist, selected_sites, and faults
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


class GuardError(RuntimeError):
    """Controlled audit failure."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GuardError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise GuardError(f"invalid {label} JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise GuardError(f"{label} must be one JSON object: {path}")
    return payload


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def import_module(path: Path, name: str) -> Any:
    path = path.resolve()
    if not path.is_file():
        raise GuardError(f"Python module not found: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise GuardError(f"cannot import Python module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def resolve_reference(value: Any, base_dir: Path) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


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
        raise GuardError(
            f"git command failed: git {' '.join(args)}\n{str(stderr).strip()}"
        )
    return completed.stdout


def fault_spec_digest_payload(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Return exactly the payload hashed by the Stage-5 generator.

    stage5_faults.py computes the digest before adding
    fault_spec_digest_sha256.  A validator reading the completed JSON therefore
    must explicitly exclude that stored field in addition to generated_at_utc.
    """

    return {
        key: value
        for key, value in spec.items()
        if key not in {"generated_at_utc", "fault_spec_digest_sha256"}
    }


def compute_fault_spec_digest(spec: Mapping[str, Any]) -> str:
    return canonical_json_digest(fault_spec_digest_payload(spec))


def campaign_digest_payload(campaign: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source_stage4": campaign.get("source_stage4"),
        "mapped_netlist": campaign.get("mapped_netlist"),
        "selected_sites": campaign.get("selected_sites"),
        "faults": campaign.get("faults"),
    }


def compute_campaign_digest(campaign: Mapping[str, Any]) -> str:
    return canonical_json_digest(campaign_digest_payload(campaign))


def tracked_worktree_provenance(repo_root: Path) -> dict[str, Any]:
    head = str(run_git(repo_root, "rev-parse", "HEAD")).strip()
    branch = str(run_git(repo_root, "branch", "--show-current")).strip()
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
        "root": str(repo_root),
        "branch": branch,
        "head": head,
        "working_tree_dirty": bool(status.strip()),
        "git_status": status.splitlines(),
        "git_status_sha256": sha256_text(status),
        "tracked_diff_sha256": sha256_bytes(diff_bytes),
        "tracked_diff_size_bytes": len(diff_bytes),
        "untracked_files": untracked_files,
        "untracked_files_digest_sha256": canonical_json_digest(untracked_files),
    }


def check_recorded_file(
    *,
    record: Mapping[str, Any],
    path_key: str,
    sha_key: str,
    base_dir: Path,
    label: str,
    errors: list[str],
    checked_files: list[dict[str, Any]],
) -> None:
    raw_path = record.get(path_key)
    expected_sha = str(record.get(sha_key, ""))
    if not raw_path:
        errors.append(f"{label}: missing {path_key}")
        return
    path = resolve_reference(raw_path, base_dir)
    if not path.is_file():
        errors.append(f"{label}: file not found: {path}")
        return
    actual_sha = sha256_file(path)
    checked_files.append(
        {
            "label": label,
            "path": str(path),
            "sha256": actual_sha,
            "size_bytes": path.stat().st_size,
        }
    )
    if not expected_sha:
        errors.append(f"{label}: missing recorded SHA-256")
    elif expected_sha != actual_sha:
        errors.append(
            f"{label}: SHA-256 mismatch: expected={expected_sha}, actual={actual_sha}"
        )


def audit_monitor(path: Path, errors: list[str]) -> dict[str, Any]:
    path = path.resolve()
    result: dict[str, Any] = {"path": str(path), "exists": path.is_file()}
    if not path.is_file():
        errors.append(f"monitor not found: {path}")
        return result
    text = path.read_text(encoding="utf-8", errors="strict")
    contains_final = bool(re.search(r"\bfinal\s+(?:begin|:)", text))
    contains_removed_flush = bool(re.search(r"::flush\s*\(\s*\)\s*;", text))
    contains_fflush = "$fflush" in text
    result.update(
        {
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
            "contains_final_block": contains_final,
            "contains_removed_flush_task_call": contains_removed_flush,
            "contains_immediate_fflush": contains_fflush,
        }
    )
    if contains_final:
        errors.append(f"monitor contains unsupported final block: {path}")
    if contains_removed_flush:
        errors.append(f"monitor contains removed ::flush() call: {path}")
    if not contains_fflush:
        errors.append(f"monitor contains no immediate $fflush: {path}")
    return result


def audit_manifest(
    path: Path,
    *,
    campaign: Mapping[str, Any],
    campaign_path: Path,
    expected_schema: str,
    errors: list[str],
) -> dict[str, Any]:
    path = path.resolve()
    result: dict[str, Any] = {"path": str(path), "exists": path.is_file()}
    if not path.is_file():
        errors.append(f"monitor manifest not found: {path}")
        return result
    payload = load_json(path, "monitor manifest")
    result.update(
        {
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
            "kind": payload.get("kind"),
            "schema_version": payload.get("schema_version"),
            "fault_id": payload.get("fault_id"),
        }
    )
    if str(payload.get("schema_version", "")) != expected_schema:
        errors.append(
            f"manifest schema mismatch {path}: expected={expected_schema}, "
            f"actual={payload.get('schema_version')}"
        )
    if payload.get("campaign_digest_sha256") is not None:
        expected = campaign.get("campaign_digest_sha256")
        if payload.get("campaign_digest_sha256") != expected:
            errors.append(
                f"manifest campaign digest mismatch {path}: expected={expected}, "
                f"actual={payload.get('campaign_digest_sha256')}"
            )
    if payload.get("campaign") is not None:
        actual_path = resolve_reference(payload["campaign"], path.parent)
        if actual_path != campaign_path.resolve():
            errors.append(
                f"manifest campaign path mismatch {path}: "
                f"expected={campaign_path.resolve()}, actual={actual_path}"
            )
    if payload.get("fault_spec") is not None:
        spec_path = resolve_reference(payload["fault_spec"], path.parent)
        if not spec_path.is_file():
            errors.append(f"manifest fault spec not found {path}: {spec_path}")
        else:
            spec = load_json(spec_path, "manifest fault spec")
            recorded = spec.get("fault_spec_digest_sha256")
            if payload.get("fault_spec_digest_sha256") != recorded:
                errors.append(
                    f"manifest fault-spec digest mismatch {path}: "
                    f"expected={recorded}, "
                    f"actual={payload.get('fault_spec_digest_sha256')}"
                )
            computed = compute_fault_spec_digest(spec)
            if recorded != computed:
                errors.append(
                    f"manifest referenced invalid fault spec {spec_path}: "
                    f"recorded={recorded}, computed={computed}"
                )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit Stage-5 versions and artifacts without simulation."
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--tool", type=Path, required=True)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--monitor", type=Path, action="append", default=[])
    parser.add_argument("--manifest", type=Path, action="append", default=[])
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--write-lock", type=Path)
    parser.add_argument(
        "--require-clean",
        action="store_true",
        help="treat a dirty Git worktree as an error (use for final release locks)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    tool_path = args.tool.resolve()
    campaign_path = args.campaign.resolve()

    errors: list[str] = []
    warnings: list[str] = []
    checked_files: list[dict[str, Any]] = []
    monitor_records: list[dict[str, Any]] = []
    manifest_records: list[dict[str, Any]] = []

    try:
        tool = import_module(tool_path, "f2a_stage5_guard_target")
        program_version = str(tool.PROGRAM_VERSION)
        schema_version = str(tool.SCHEMA_VERSION)
        stage5_campaign_marker = str(tool.STAGE5_CAMPAIGN_MARKER)
        stage5_fault_marker = str(tool.STAGE5_FAULT_MARKER)
    except Exception as exc:
        print(f"ERROR: cannot load Stage-5 tool: {exc}", file=sys.stderr)
        return 1

    try:
        repository = tracked_worktree_provenance(repo_root)
    except GuardError as exc:
        errors.append(str(exc))
        repository = {
            "root": str(repo_root),
            "branch": "",
            "head": "",
            "working_tree_dirty": True,
            "git_status": [],
            "git_status_sha256": "",
            "tracked_diff_sha256": "",
            "tracked_diff_size_bytes": 0,
            "untracked_files": [],
            "untracked_files_digest_sha256": "",
        }

    if repository["working_tree_dirty"]:
        message = (
            "repository working tree is dirty; the lock records HEAD, tracked "
            "diff digest, untracked file digests, and artifact hashes"
        )
        if args.require_clean:
            errors.append(message)
        else:
            warnings.append(message)

    try:
        campaign = load_json(campaign_path, "Stage-5 campaign")
    except GuardError as exc:
        errors.append(str(exc))
        campaign = {}

    spec_versions: Counter[str] = Counter()
    spec_schemas: Counter[str] = Counter()
    fault_count = 0
    patch_count = 0

    if campaign:
        if campaign.get("stage") != stage5_campaign_marker:
            errors.append(f"campaign stage mismatch: {campaign.get('stage')!r}")
        if str(campaign.get("program_version", "")) != program_version:
            errors.append(
                "campaign program-version mismatch: "
                f"tool={program_version}, campaign={campaign.get('program_version')}"
            )
        if str(campaign.get("schema_version", "")) != schema_version:
            errors.append(
                "campaign schema-version mismatch: "
                f"tool={schema_version}, campaign={campaign.get('schema_version')}"
            )
        recorded_campaign_digest = campaign.get("campaign_digest_sha256")
        computed_campaign_digest = compute_campaign_digest(campaign)
        if recorded_campaign_digest != computed_campaign_digest:
            errors.append(
                "campaign digest mismatch: "
                f"recorded={recorded_campaign_digest}, "
                f"computed={computed_campaign_digest}"
            )

        source_stage4 = campaign.get("source_stage4")
        if isinstance(source_stage4, dict):
            check_recorded_file(
                record=source_stage4,
                path_key="candidates_path",
                sha_key="candidates_sha256",
                base_dir=campaign_path.parent,
                label="Stage-4 candidates",
                errors=errors,
                checked_files=checked_files,
            )
            check_recorded_file(
                record=source_stage4,
                path_key="selection_path",
                sha_key="selection_sha256",
                base_dir=campaign_path.parent,
                label="Stage-4 selection",
                errors=errors,
                checked_files=checked_files,
            )
        else:
            errors.append("campaign missing source_stage4 object")

        mapped = campaign.get("mapped_netlist")
        if isinstance(mapped, dict):
            check_recorded_file(
                record=mapped,
                path_key="path",
                sha_key="sha256",
                base_dir=campaign_path.parent,
                label="mapped netlist",
                errors=errors,
                checked_files=checked_files,
            )
        else:
            errors.append("campaign missing mapped_netlist object")

        selected_sites = campaign.get("selected_sites")
        faults = campaign.get("faults")
        if not isinstance(selected_sites, list) or not selected_sites:
            errors.append("campaign contains no selected_sites")
            selected_sites = []
        if not isinstance(faults, list) or not faults:
            errors.append("campaign contains no faults")
            faults = []

        summary = campaign.get("campaign_summary")
        if not isinstance(summary, dict):
            errors.append("campaign missing campaign_summary")
        else:
            if int(summary.get("selected_unique_site_count", -1)) != len(selected_sites):
                errors.append("campaign selected-site summary count mismatch")
            if int(summary.get("fault_instance_count", -1)) != len(faults):
                errors.append("campaign fault-instance summary count mismatch")

        seen_fault_ids: set[str] = set()
        for record in faults:
            if not isinstance(record, dict):
                errors.append("campaign fault record is not an object")
                continue
            fault_id = str(record.get("fault_id", ""))
            if not fault_id:
                errors.append("campaign fault record has no fault_id")
                continue
            if fault_id in seen_fault_ids:
                errors.append(f"duplicate campaign fault ID: {fault_id}")
                continue
            seen_fault_ids.add(fault_id)
            fault_count += 1

            spec_value = record.get("fault_spec")
            patch_value = record.get("patch")
            if not spec_value:
                errors.append(f"{fault_id}: missing fault_spec path")
                continue
            spec_path = resolve_reference(spec_value, campaign_path.parent)
            if not spec_path.is_file():
                errors.append(f"{fault_id}: fault spec not found: {spec_path}")
                continue
            spec = load_json(spec_path, f"fault spec {fault_id}")
            spec_version = str(spec.get("program_version", ""))
            spec_schema = str(spec.get("schema_version", ""))
            spec_versions[spec_version] += 1
            spec_schemas[spec_schema] += 1

            if spec.get("stage") != stage5_fault_marker:
                errors.append(
                    f"{fault_id}: fault-spec stage mismatch: {spec.get('stage')!r}"
                )
            if str(spec.get("fault_id", "")) != fault_id:
                errors.append(
                    f"{fault_id}: fault-spec ID mismatch: {spec.get('fault_id')!r}"
                )
            if spec_version != program_version:
                errors.append(
                    f"{fault_id}: program-version mismatch: "
                    f"tool={program_version}, spec={spec_version}"
                )
            if spec_schema != schema_version:
                errors.append(
                    f"{fault_id}: schema-version mismatch: "
                    f"tool={schema_version}, spec={spec_schema}"
                )

            recorded_spec_digest = spec.get("fault_spec_digest_sha256")
            computed_spec_digest = compute_fault_spec_digest(spec)
            if recorded_spec_digest != computed_spec_digest:
                errors.append(
                    f"{fault_id}: fault-spec digest mismatch: "
                    f"recorded={recorded_spec_digest}, "
                    f"computed={computed_spec_digest}"
                )
            if record.get("fault_spec_digest_sha256") != recorded_spec_digest:
                errors.append(
                    f"{fault_id}: campaign/spec digest mismatch: "
                    f"campaign={record.get('fault_spec_digest_sha256')}, "
                    f"spec={recorded_spec_digest}"
                )

            spec_stage4 = spec.get("source_stage4")
            if isinstance(source_stage4, dict) and isinstance(spec_stage4, dict):
                for key in (
                    "candidates_sha256",
                    "candidate_digest_sha256",
                    "selection_sha256",
                    "selection_digest_sha256",
                ):
                    if spec_stage4.get(key) != source_stage4.get(key):
                        errors.append(f"{fault_id}: source_stage4 mismatch for {key}")
            spec_mapped = spec.get("mapped_netlist")
            if isinstance(mapped, dict) and isinstance(spec_mapped, dict):
                if spec_mapped.get("sha256") != mapped.get("sha256"):
                    errors.append(f"{fault_id}: mapped-netlist digest mismatch")

            if not patch_value:
                errors.append(f"{fault_id}: missing patch path")
                continue
            patch_path = resolve_reference(patch_value, campaign_path.parent)
            if not patch_path.is_file():
                errors.append(f"{fault_id}: patch not found: {patch_path}")
            elif patch_path.stat().st_size == 0:
                errors.append(f"{fault_id}: patch is empty: {patch_path}")
            else:
                patch_count += 1

    for monitor in args.monitor:
        monitor_records.append(audit_monitor(monitor, errors))
    if campaign:
        for manifest in args.manifest:
            manifest_records.append(
                audit_manifest(
                    manifest,
                    campaign=campaign,
                    campaign_path=campaign_path,
                    expected_schema=schema_version,
                    errors=errors,
                )
            )

    guard_path = Path(__file__).resolve()
    report = {
        "kind": "stage5_version_consistency_audit",
        "generated_at_utc": utc_now(),
        "status": "PASS" if not errors else "FAIL",
        "repository": repository,
        "guard": {
            "path": str(guard_path),
            "sha256": sha256_file(guard_path),
        },
        "stage5_tool": {
            "path": str(tool_path),
            "program_version": program_version,
            "schema_version": schema_version,
            "sha256": sha256_file(tool_path),
        },
        "campaign": {
            "path": str(campaign_path),
            "exists": campaign_path.is_file(),
            "file_sha256": sha256_file(campaign_path) if campaign_path.is_file() else None,
            "program_version": campaign.get("program_version"),
            "schema_version": campaign.get("schema_version"),
            "campaign_digest_sha256": campaign.get("campaign_digest_sha256"),
        },
        "fault_artifacts": {
            "fault_spec_count": fault_count,
            "patch_count": patch_count,
            "fault_spec_program_versions": dict(sorted(spec_versions.items())),
            "fault_spec_schema_versions": dict(sorted(spec_schemas.items())),
        },
        "checked_source_files": checked_files,
        "monitors": monitor_records,
        "manifests": manifest_records,
        "warnings": warnings,
        "errors": errors,
    }
    atomic_write_json(args.report.resolve(), report)

    lock_written = False
    if args.write_lock is not None:
        if errors:
            warnings.append("version lock was not written because the audit failed")
            report["warnings"] = warnings
            atomic_write_json(args.report.resolve(), report)
        else:
            lock = dict(report)
            lock["kind"] = "stage5_version_lock"
            lock["lock_generated_at_utc"] = utc_now()
            atomic_write_json(args.write_lock.resolve(), lock)
            lock_written = True

    print()
    print("=" * 78)
    print("Fault2Assertion Stage-5 Version Consistency")
    print("=" * 78)
    print(f"Repository HEAD       : {repository.get('head', '')}")
    print(f"Stage-5 tool version  : {program_version}")
    print(f"Stage-5 tool schema   : {schema_version}")
    print(f"Campaign version      : {campaign.get('program_version')}")
    print(f"Fault specs checked   : {fault_count}")
    print(f"Patches checked       : {patch_count}")
    print(f"Monitors checked      : {len(monitor_records)}")
    print(f"Manifests checked     : {len(manifest_records)}")
    print(f"Warnings              : {len(warnings)}")
    print(f"Errors                : {len(errors)}")
    print(f"Audit report          : {args.report.resolve()}")
    if args.write_lock is not None:
        print(
            f"Version lock          : "
            f"{args.write_lock.resolve() if lock_written else 'NOT WRITTEN'}"
        )
    print(f"Result                : {report['status']}")
    print("=" * 78)

    if warnings:
        print("\nWarnings:")
        for warning in warnings:
            print(f"  - {warning}")
    if errors:
        print("\nErrors:")
        preview_limit = 40
        for error in errors[:preview_limit]:
            print(f"  - {error}")
        if len(errors) > preview_limit:
            print(f"  ... {len(errors) - preview_limit} additional errors omitted")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
