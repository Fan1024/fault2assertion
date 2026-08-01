#!/usr/bin/env python3
"""Recover the two FAILED cases in the existing Stage-5 four-site batch.

Commands:
  diagnose       Extract the failed IDs, first Xcelium errors, failure phases,
                 signature comparison, bundle inventory, and retained work.
  archive-reset  Move old ERROR evidence outside the active batch and reset
                 only those fault records to PREPARED.
  rerun          Run only the two reset faults with the existing batch engine.
  verify         Require ORACLE_VALIDATED_CLEANED=8.

No fault selection, detector policy, verdict semantics, or oracle semantics are
changed. Archive/reset fixes the recovery-path defect where an existing ERROR
result.json would otherwise be reused; it also forces fresh monitor, trace,
run-local netlist, and Xcelium work generation. A repeated identical Xcelium
error still requires a source-level fix and blocks validation/freezing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "1.0"
PROGRAM_VERSION = "1.0.0"
FINAL_STATE = "ORACLE_VALIDATED_CLEANED"
FAULT_ID_RE = re.compile(r"^TF\d{6}_SA[01]$")
TOOL_ERROR_RE = re.compile(
    r"^(?P<tool>xmvlog|xmelab|xmsim|xrun):\s*"
    r"\*(?P<severity>[EF]),(?P<mnemonic>[A-Za-z0-9_]+):?\s*(?P<message>.*)$",
    re.I,
)
GENERIC_ERROR_RE = re.compile(
    r"^F2A_RUNNER_ERROR:|\b(?:segmentation fault|core dumped|internal error|"
    r"license checkout failed|failed to acquire.*license|no space left on device|"
    r"disk quota exceeded)\b|^FATAL:\s",
    re.I,
)
PATH_RE = re.compile(r"/(?:[^\s,:()]+/)+[^\s,:()]+")
LOCATION_RE = re.compile(r"\([^(),]+,\d+(?:\|\d+)?\)")
HEX_RE = re.compile(r"\b0x[0-9a-f]+\b", re.I)
NUMBER_RE = re.compile(r"\b\d{3,}\b")
SPACE_RE = re.compile(r"\s+")


class RecoveryError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RecoveryError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RecoveryError(f"invalid {label} JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RecoveryError(f"{label} must contain one JSON object: {path}")
    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_text(path: Path, value: str) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except FileNotFoundError:
            pass
    return total


def human(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.2f} {unit}"
        amount /= 1024
    return f"{value} B"


def ensure_under(path: Path, root: Path, label: str) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise RecoveryError(f"{label} escapes expected root: {path}") from exc


def run(arguments: Sequence[str]) -> int:
    print("+ " + " ".join(str(item) for item in arguments), flush=True)
    return subprocess.run([str(item) for item in arguments], check=False).returncode


def normalize(message: str) -> str:
    value = message.strip().lower()
    value = LOCATION_RE.sub("(<location>)", value)
    value = PATH_RE.sub("<path>", value)
    value = HEX_RE.sub("<hex>", value)
    value = NUMBER_RE.sub("<n>", value)
    return SPACE_RE.sub(" ", value).strip()


def manifest_records(batch_root: Path) -> dict[str, dict[str, Any]]:
    manifest = load_json(batch_root / "pilot_manifest.json", "pilot manifest")
    faults = manifest.get("faults")
    if not isinstance(faults, list):
        raise RecoveryError("pilot manifest has no faults array")
    records: dict[str, dict[str, Any]] = {}
    for item in faults:
        if not isinstance(item, dict):
            raise RecoveryError("pilot manifest contains invalid fault record")
        fault_id = str(item.get("fault_id", ""))
        if not FAULT_ID_RE.fullmatch(fault_id) or fault_id in records:
            raise RecoveryError(f"invalid or duplicate manifest fault: {fault_id!r}")
        records[fault_id] = dict(item)
    if len(records) != 8:
        raise RecoveryError(f"four-site manifest must contain 8 faults: {len(records)}")
    return records


def failed_ids(batch_root: Path, records: Mapping[str, Mapping[str, Any]]) -> list[str]:
    report = load_json(batch_root / "pilot_status.json", "pilot status")
    rows = report.get("faults")
    if not isinstance(rows, list):
        raise RecoveryError("pilot status has no faults array")
    values = [
        str(item.get("fault_id"))
        for item in rows
        if isinstance(item, dict) and item.get("state") == "FAILED"
    ]
    if len(values) != 2 or len(set(values)) != 2:
        raise RecoveryError(f"expected exactly two FAILED faults: {values}")
    for fault_id in values:
        if fault_id not in records:
            raise RecoveryError(f"FAILED fault absent from manifest: {fault_id}")
        root = Path(str(records[fault_id]["fault_root"])).resolve()
        ensure_under(root, batch_root, f"fault root for {fault_id}")
        status = load_json(root / "status.json", f"status for {fault_id}")
        if status.get("state") != "FAILED":
            raise RecoveryError(f"active status is not FAILED for {fault_id}")
    return sorted(values, key=lambda item: int(records[item].get("order", 10**9)))


def events_from_result(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = result.get("raw_facts")
    tool = raw.get("tool") if isinstance(raw, dict) else None
    events = tool.get("infrastructure_events") if isinstance(tool, dict) else None
    if not isinstance(events, list):
        return []
    return [dict(item) for item in events if isinstance(item, dict)]


def events_from_log(lines: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        match = TOOL_ERROR_RE.match(line)
        if match:
            tool = match.group("tool").lower()
            mnemonic = match.group("mnemonic").upper()
            if tool == "xmsim" and mnemonic == "ASRTST":
                continue
            events.append(
                {
                    "log_line": line_number,
                    "tool": tool,
                    "severity": match.group("severity").upper(),
                    "mnemonic": mnemonic,
                    "message": line,
                }
            )
        elif GENERIC_ERROR_RE.search(line):
            events.append(
                {
                    "log_line": line_number,
                    "tool": "stage5_runner" if line.startswith("F2A_RUNNER_ERROR:") else "unknown",
                    "severity": "F",
                    "mnemonic": "GENERIC_INFRASTRUCTURE_FAILURE",
                    "message": line,
                }
            )
    return events


def failure_phase(event: Mapping[str, Any], lines: list[str], manifest: Mapping[str, Any] | None) -> str:
    preflight = str(manifest.get("preflight_failure", "")) if manifest else ""
    head = "\n".join(lines[:400]).lower()
    if preflight or "f2a_runner_error:" in head:
        text = f"{preflight}\n{head}"
        return "MATERIALIZATION" if "material" in text or "fault_netlist" in text else "PREFLIGHT"
    tool = str(event.get("tool", "")).lower()
    if tool == "xmvlog":
        return "COMPILE"
    if tool == "xmelab":
        return "ELABORATION"
    if tool == "xmsim":
        return "RUNTIME"
    if tool == "xrun":
        return "XRUN_LAUNCH"
    return "UNKNOWN"


def context(lines: list[str], line_number: int | None) -> list[str]:
    if not isinstance(line_number, int) or line_number < 1:
        return []
    start = max(0, line_number - 5)
    end = min(len(lines), line_number + 4)
    return [f"{index + 1}: {lines[index]}" for index in range(start, end)]


def bundle_inventory(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False, "path": str(path), "bytes": 0, "members": []}
    report: dict[str, Any] = {
        "exists": True,
        "path": str(path),
        "bytes": path.stat().st_size,
        "human_bytes": human(path.stat().st_size),
        "sha256": sha256(path),
    }
    try:
        with tarfile.open(path, "r:*") as archive:
            members = [item for item in archive.getmembers() if item.isfile()]
            members.sort(key=lambda item: (-item.size, item.name))
            names = [item.name for item in members]
            report.update(
                {
                    "member_count": len(members),
                    "uncompressed_bytes": sum(item.size for item in members),
                    "members": [
                        {"name": item.name, "bytes": item.size}
                        for item in members[:25]
                    ],
                    "contains": {
                        "xrun_log": any(name.endswith("xrun.log") for name in names),
                        "result_json": any(name.endswith("result.json") for name in names),
                        "command": any(name.endswith("command.txt") for name in names),
                        "fault_json": any(name.endswith("fault.json") for name in names),
                        "monitor_sv": any(name.endswith("monitor.sv") for name in names),
                        "fault_netlist": any(name.endswith("fault_netlist.v") for name in names),
                        "work_tree": any("/work/" in f"/{name}/" for name in names),
                    },
                    "error": None,
                }
            )
    except (tarfile.TarError, OSError) as exc:
        report.update({"member_count": 0, "members": [], "error": str(exc)})
    return report


def work_inventory(path: Path) -> dict[str, Any]:
    if not path.is_dir():
        return {"exists": False, "path": str(path), "bytes": 0, "files": []}
    files: list[tuple[int, str]] = []
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                files.append((item.stat().st_size, str(item.relative_to(path))))
        except FileNotFoundError:
            pass
    files.sort(key=lambda item: (-item[0], item[1]))
    total = sum(size for size, _ in files)
    return {
        "exists": True,
        "path": str(path),
        "bytes": total,
        "human_bytes": human(total),
        "file_count": len(files),
        "largest_files": [
            {"path": name, "bytes": size, "human_bytes": human(size)}
            for size, name in files[:25]
        ],
    }


def analyze_fault(fault_id: str, record: Mapping[str, Any], batch_root: Path) -> dict[str, Any]:
    fault_root = Path(str(record["fault_root"])).resolve()
    ensure_under(fault_root, batch_root, f"fault root for {fault_id}")
    run_root = fault_root / "native/run"
    result_path = run_root / "result.json"
    log_path = run_root / "xrun.log"
    result = load_json(result_path, f"Native result for {fault_id}")
    if result.get("status") != "ERROR" or result.get("reason") != "xcelium_infrastructure_error_marker":
        raise RecoveryError(
            f"{fault_id} is not the expected Xcelium infrastructure ERROR: "
            f"{result.get('status')}/{result.get('reason')}"
        )
    if not log_path.is_file():
        raise RecoveryError(f"xrun.log missing for {fault_id}: {log_path}")
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    events = events_from_result(result) or events_from_log(lines)
    if not events:
        raise RecoveryError(f"no Xcelium infrastructure event found for {fault_id}")
    events.sort(key=lambda item: int(item.get("log_line", 10**12)))
    first = events[0]
    run_manifest_path = run_root / "manifest.json"
    run_manifest = load_json(run_manifest_path, f"run manifest for {fault_id}") if run_manifest_path.is_file() else None
    signatures = [
        f"{str(item.get('tool', 'unknown')).lower()}|"
        f"{str(item.get('mnemonic', 'UNKNOWN')).upper()}|"
        f"{normalize(str(item.get('message', '')))}"
        for item in events
    ]
    return {
        "fault_id": fault_id,
        "order": record.get("order"),
        "base_fault_id": record.get("base_fault_id"),
        "polarity": record.get("polarity_directory"),
        "fault_class": record.get("fault_class"),
        "module": record.get("module"),
        "source_net": record.get("source_net"),
        "fault_root": str(fault_root),
        "failure_phase": failure_phase(first, lines, run_manifest),
        "first_error": first,
        "first_signature": signatures[0],
        "all_signatures": signatures,
        "log": {
            "path": str(log_path),
            "bytes": log_path.stat().st_size,
            "sha256": sha256(log_path),
            "context": context(lines, first.get("log_line")),
        },
        "result": {
            "path": str(result_path),
            "sha256": sha256(result_path),
            "xrun_exit_status": result.get("xrun_exit_status"),
        },
        "bundle": bundle_inventory(run_root / "reproduction_bundle.tar.gz"),
        "work": work_inventory(run_root / "work"),
    }


def compare(failures: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    left, right = failures
    same_phase = left["failure_phase"] == right["failure_phase"]
    same_first = left["first_signature"] == right["first_signature"]
    overlap = set(left["all_signatures"]) & set(right["all_signatures"])
    if same_phase and same_first:
        return "CONFIRMED", "same phase and identical first normalized Xcelium signature"
    if same_phase and overlap:
        return "PROBABLE", "same phase and at least one shared normalized Xcelium signature"
    return "NOT_CONFIRMED", "different phase or no shared normalized Xcelium signature"


def render_text(report: Mapping[str, Any]) -> str:
    output = [
        "Stage5 four-site failed-case diagnosis",
        "========================================",
        f"Failed faults    : {', '.join(report['failed_fault_ids'])}",
        f"Same root cause : {report['comparison']['same_root_cause']}",
        f"Reason          : {report['comparison']['reason']}",
        f"Same site       : {report['comparison']['same_base_fault_id']}",
        "",
    ]
    for item in report["failures"]:
        error = item["first_error"]
        output.extend(
            [
                f"Fault           : {item['fault_id']}",
                f"  Site          : {item['base_fault_id']}",
                f"  Signal        : {item['module']}.{item['source_net']}",
                f"  Phase         : {item['failure_phase']}",
                f"  Error         : {error.get('tool')} *{error.get('severity')},{error.get('mnemonic')}",
                f"  Log line      : {error.get('log_line')}",
                f"  Message       : {error.get('message')}",
                f"  Work          : {item['work'].get('human_bytes', '0 B')}",
                f"  Bundle        : {item['bundle'].get('human_bytes', '0 B')} ({item['bundle'].get('member_count', 0)} files)",
                "  Context:",
            ]
        )
        output.extend(f"    {line}" for line in item["log"]["context"])
        output.append("")
    output.extend(
        [
            "Recovery contract",
            "-----------------",
            "Archive/reset removes the active ERROR result that would otherwise be",
            "reused and forces all failed native-mode generated inputs to be rebuilt.",
            "It does not ignore a real Xcelium error. A repeated signature blocks",
            "four-site validation and freezing.",
        ]
    )
    return "\n".join(output) + "\n"


def diagnose(args: argparse.Namespace) -> int:
    batch_root = args.batch_root.resolve()
    records = manifest_records(batch_root)
    fault_ids = failed_ids(batch_root, records)
    failures = [analyze_fault(item, records[item], batch_root) for item in fault_ids]
    root_status, reason = compare(failures)
    report = {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "kind": "stage5_4site_failed_case_diagnosis",
        "generated_at_utc": now(),
        "status": "PASS",
        "batch_root": str(batch_root),
        "failed_fault_ids": fault_ids,
        "failures": failures,
        "comparison": {
            "same_root_cause": root_status,
            "reason": reason,
            "same_base_fault_id": failures[0]["base_fault_id"] == failures[1]["base_fault_id"],
            "same_module": failures[0]["module"] == failures[1]["module"],
            "same_source_net": failures[0]["source_net"] == failures[1]["source_net"],
            "same_phase": failures[0]["failure_phase"] == failures[1]["failure_phase"],
            "same_first_signature": failures[0]["first_signature"] == failures[1]["first_signature"],
        },
        "repair_contract": {
            "repair": "ARCHIVE_ERROR_MODE_RESET_PREPARED_REGENERATE",
            "fault_selection_changed": False,
            "detector_policy_changed": False,
            "verdict_policy_changed": False,
            "oracle_policy_changed": False,
            "real_xcelium_error_suppressed": False,
            "archive_outside_active_batch": True,
        },
    }
    write_json(args.output, report)
    write_text(args.text_output, render_text(report))
    print("\n======================================================================")
    print("Stage5 four-site failed-case diagnosis: PASS")
    print("======================================================================")
    print(f"Failed fault IDs : {', '.join(fault_ids)}")
    for item in failures:
        error = item["first_error"]
        print(f"{item['fault_id']} phase : {item['failure_phase']}")
        print(f"{item['fault_id']} error : {error.get('tool')} *{error.get('severity')},{error.get('mnemonic')} line={error.get('log_line')}")
        print(f"{item['fault_id']} work  : {item['work'].get('human_bytes', '0 B')}")
        print(f"{item['fault_id']} bundle: {item['bundle'].get('human_bytes', '0 B')}")
    print(f"Same root cause  : {root_status}")
    print(f"Same physical site: {report['comparison']['same_base_fault_id']}")
    print(f"Diagnosis JSON   : {args.output.resolve()}")
    print(f"Diagnosis text   : {args.text_output.resolve()}")
    return 0


def move_item(source: Path, destination_root: Path) -> dict[str, Any] | None:
    if not source.exists():
        return None
    destination = destination_root / source.name
    if destination.exists():
        raise RecoveryError(f"archive destination exists: {destination}")
    size = tree_bytes(source) if source.is_dir() else source.stat().st_size
    shutil.move(str(source), str(destination))
    return {"source": str(source), "destination": str(destination), "bytes": size}


def archive_reset(args: argparse.Namespace) -> int:
    batch_root = args.batch_root.resolve()
    diagnosis = load_json(args.diagnosis.resolve(), "failure diagnosis")
    fault_ids = diagnosis.get("failed_fault_ids")
    if diagnosis.get("status") != "PASS" or not isinstance(fault_ids, list) or len(fault_ids) != 2:
        raise RecoveryError("diagnosis is not a valid two-fault PASS report")
    root_status = diagnosis.get("comparison", {}).get("same_root_cause")
    if root_status == "NOT_CONFIRMED" and not args.allow_distinct:
        raise RecoveryError("root cause is NOT_CONFIRMED; review both errors, then use --allow-distinct only when intentional")

    records = manifest_records(batch_root)
    if failed_ids(batch_root, records) != fault_ids:
        raise RecoveryError("active FAILED set changed after diagnosis")

    # Fail-closed preflight before moving anything.
    for fault_id in fault_ids:
        fault_root = Path(str(records[fault_id]["fault_root"])).resolve()
        if not (fault_root / "native").is_dir() or not (fault_root / "status.json").is_file():
            raise RecoveryError(f"required failed evidence missing for {fault_id}")

    archive_root = args.archive_parent.resolve() / f"recovery_{stamp()}"
    if archive_root.exists():
        raise RecoveryError(f"archive root exists: {archive_root}")
    archive_root.mkdir(parents=True)
    shutil.copy2(args.diagnosis.resolve(), archive_root / "failure_diagnosis.json")
    text = args.diagnosis.resolve().with_suffix(".txt")
    if text.is_file():
        shutil.copy2(text, archive_root / "failure_diagnosis.txt")

    archives: list[dict[str, Any]] = []
    for fault_id in fault_ids:
        record = records[fault_id]
        fault_root = Path(str(record["fault_root"])).resolve()
        destination = archive_root / fault_id
        destination.mkdir()
        moved = [
            item
            for name in ("native", "observe", "diagnostic_quarantine", "oracle", "routing.json", "cleanup.json", "status.json")
            if (item := move_item(fault_root / name, destination)) is not None
        ]
        reset = {
            "schema_version": "1.0",
            "state": "PREPARED",
            "updated_at_utc": now(),
            "fault_id": fault_id,
            "base_fault_id": record.get("base_fault_id"),
            "polarity": record.get("polarity_directory"),
            "order": record.get("order"),
            "recovery": {
                "reason": "archive_non_scientific_error_and_regenerate",
                "archive_root": str(destination),
                "diagnosis": str(args.diagnosis.resolve()),
            },
        }
        write_json(fault_root / "status.json", reset)
        archived = {"fault_id": fault_id, "archive_root": str(destination), "moved": moved}
        write_json(destination / "archive_manifest.json", archived)
        archives.append(archived)

    plan = {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "kind": "stage5_4site_failed_fault_rerun_plan",
        "generated_at_utc": now(),
        "status": "READY",
        "root": str(args.root.resolve()),
        "batch_root": str(batch_root),
        "fault_ids": fault_ids,
        "diagnosis": str(args.diagnosis.resolve()),
        "same_root_cause": root_status,
        "archive_root": str(archive_root),
        "archives": archives,
        "scientific_semantics_changed": False,
    }
    write_json(archive_root / "rerun_plan.json", plan)
    active_plan = batch_root / "recovery/latest_rerun_plan.json"
    write_json(active_plan, plan)
    print("\n======================================================================")
    print("Stage5 failed-case archive/reset: PASS")
    print("======================================================================")
    print(f"Archived faults : {', '.join(fault_ids)}")
    print(f"Archive root    : {archive_root}")
    print(f"Rerun plan      : {active_plan}")
    print("Reset state     : PREPARED")
    return 0


def current_rows(batch_root: Path) -> tuple[Counter[str], list[dict[str, Any]]]:
    records = manifest_records(batch_root)
    counts: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    for fault_id, record in sorted(records.items(), key=lambda item: int(item[1].get("order", 10**9))):
        root = Path(str(record["fault_root"])).resolve()
        status = load_json(root / "status.json", f"status for {fault_id}")
        state = str(status.get("state", "UNKNOWN"))
        counts[state] += 1
        rows.append(
            {
                "fault_id": fault_id,
                "state": state,
                "native_status": status.get("native_status"),
                "route": status.get("route"),
                "failure_reason": status.get("failure_reason"),
            }
        )
    return counts, rows


def verify(batch_root: Path, output: Path) -> int:
    counts, rows = current_rows(batch_root)
    errors = [] if counts == Counter({FINAL_STATE: 8}) else [f"expected {FINAL_STATE}=8, found {dict(counts)}"]
    errors.extend(f"{row['fault_id']}: {row['state']} {row['failure_reason']}" for row in rows if row["state"] != FINAL_STATE)
    report = {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "kind": "stage5_4site_post_recovery_verification",
        "generated_at_utc": now(),
        "status": "PASS" if not errors else "FAIL",
        "batch_root": str(batch_root),
        "counts": dict(counts),
        "faults": rows,
        "errors": errors,
    }
    write_json(output, report)
    print("\n======================================================================")
    print(f"Stage5 four-site post-recovery verification: {report['status']}")
    print("======================================================================")
    print(f"Final states       : {dict(counts)}")
    print(f"Verification report: {output.resolve()}")
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    return 0 if not errors else 2


def rerun(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    batch_root = args.batch_root.resolve()
    plan = load_json(args.rerun_plan.resolve(), "rerun plan")
    fault_ids = plan.get("fault_ids")
    if plan.get("status") != "READY" or not isinstance(fault_ids, list) or len(fault_ids) != 2:
        raise RecoveryError("rerun plan is not READY for exactly two faults")
    if Path(str(plan.get("batch_root", ""))).resolve() != batch_root:
        raise RecoveryError("rerun plan points to a different batch")
    engine = root / "scripts/fault_characterization/stage5_batch.py"
    if not engine.is_file():
        raise RecoveryError(f"batch engine not found: {engine}")

    command_failures = []
    for fault_id in fault_ids:
        status = run(
            [
                sys.executable,
                str(engine),
                "--root", str(root),
                "--pilot-root", str(batch_root),
                "run-one",
                "--fault-id", str(fault_id),
                "--maxcycles", str(args.maxcycles),
            ]
        )
        if status != 0:
            command_failures.append({"fault_id": fault_id, "exit_status": status})

    run([sys.executable, str(engine), "--root", str(root), "--pilot-root", str(batch_root), "status"])
    run([sys.executable, str(engine), "--root", str(root), "--pilot-root", str(batch_root), "storage-report"])
    result = verify(batch_root, args.verification_output.resolve())
    if command_failures or result != 0:
        print(f"Rerun command errors: {command_failures}", file=sys.stderr)
        print("Rerun verdict: REVIEW_REQUIRED")
        print("Do not validate or freeze. Compare the new error with the archived diagnosis.")
        return 2

    plan["status"] = "PASS"
    plan["completed_at_utc"] = now()
    plan["verification"] = str(args.verification_output.resolve())
    write_json(args.rerun_plan.resolve(), plan)
    print("Rerun verdict       : PASS")
    print(f"Recovered fault IDs : {', '.join(str(item) for item in fault_ids)}")
    print(f"Final state          : {FINAL_STATE} = 8")
    return 0


def parser() -> argparse.ArgumentParser:
    root_default = Path(__file__).resolve().parents[2]
    batch_default = root_default / "runs/stage5_campaign_v2/cv32e40p/crc32/sites_4"
    diagnosis_default = batch_default / "recovery/failure_diagnosis.json"
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", type=Path, default=root_default)
    result.add_argument("--batch-root", type=Path, default=batch_default)
    sub = result.add_subparsers(dest="command", required=True)

    command = sub.add_parser("diagnose")
    command.add_argument("--output", type=Path, default=diagnosis_default)
    command.add_argument("--text-output", type=Path, default=diagnosis_default.with_suffix(".txt"))
    command.set_defaults(func=diagnose)

    command = sub.add_parser("archive-reset")
    command.add_argument("--diagnosis", type=Path, default=diagnosis_default)
    command.add_argument("--archive-parent", type=Path, default=root_default / "runs/stage5_failure_archives/cv32e40p/crc32/sites_4")
    command.add_argument("--allow-distinct", action="store_true")
    command.set_defaults(func=archive_reset)

    command = sub.add_parser("rerun")
    command.add_argument("--rerun-plan", type=Path, default=batch_default / "recovery/latest_rerun_plan.json")
    command.add_argument("--maxcycles", type=int, default=2_000_000)
    command.add_argument("--verification-output", type=Path, default=batch_default / "recovery/rerun_verification.json")
    command.set_defaults(func=rerun)

    command = sub.add_parser("verify")
    command.add_argument("--output", type=Path, default=batch_default / "recovery/rerun_verification.json")
    command.set_defaults(func=lambda args: verify(args.batch_root.resolve(), args.output.resolve()))
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if hasattr(args, "maxcycles") and args.maxcycles <= 0:
            raise RecoveryError("MAXCYCLES must be positive")
        return int(args.func(args))
    except RecoveryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: unexpected recovery failure: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
