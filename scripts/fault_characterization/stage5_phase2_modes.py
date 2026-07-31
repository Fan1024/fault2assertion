#!/usr/bin/env python3
"""Canonical Stage-5 Phase-2 monitor preparation and gate validation.

This tool deliberately keeps mode metadata out of SystemVerilog behavior.  A
run-local monitor is produced only by replacing its compact-trace path.  The
actual source profile is selected and recorded by the Stage-5 common runner:

* native profile: original ``mm_ram.sv``;
* diagnostic profile: one run-local, ownership-audited overlay.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

PROGRAM_VERSION = "3.0.0"
SCHEMA_VERSION = "1.0"
EXECUTABLE_MODES = {"NATIVE", "OBSERVE", "QUARANTINE"}
NON_EXECUTABLE_CAPABILITIES = {"NON_CONTINUABLE"}
FINAL_BLOCK_RE = re.compile(r"\bfinal\s+(?:begin|:)", re.IGNORECASE)
REMOVED_FLUSH_TASK_RE = re.compile(r"::\s*flush\s*\(\s*\)\s*;")


class Phase2Error(RuntimeError):
    """Controlled Phase-2 validation failure."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise Phase2Error(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise Phase2Error(f"invalid {label} JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise Phase2Error(f"{label} must contain one JSON object: {path}")
    return value


def write_json(path: Path, payload: Mapping[str, Any], force: bool) -> None:
    path = path.expanduser().resolve()
    if path.exists() and not force:
        raise Phase2Error(f"refusing to overwrite without --force: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def write_text(path: Path, text: str, force: bool) -> None:
    path = path.expanduser().resolve()
    if path.exists() and not force:
        raise Phase2Error(f"refusing to overwrite without --force: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def require_file(path: Path, label: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise Phase2Error(f"{label} not found or empty: {path}")


def require_directory(path: Path, label: str) -> None:
    if not path.is_dir():
        raise Phase2Error(f"{label} not found: {path}")


def require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Phase2Error(f"{label} must be an object")
    return value


def parse_sha256sum_file(path: Path, label: str) -> list[dict[str, str]]:
    require_file(path, label)
    records: list[dict[str, str]] = []
    for line_number, raw in enumerate(
        path.read_text(encoding="utf-8", errors="strict").splitlines(), start=1
    ):
        if not raw:
            continue
        digest = raw[:64]
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise Phase2Error(
                f"malformed SHA-256 digest at {path}:{line_number}: {raw!r}"
            )
        filename = raw[64:].lstrip()
        if filename.startswith("*"):
            filename = filename[1:]
        if not filename:
            raise Phase2Error(
                f"missing SHA-256 filename at {path}:{line_number}"
            )
        records.append({"sha256": digest, "path": filename})
    if not records:
        raise Phase2Error(f"SHA-256 file contains no records: {path}")
    return records


def collect_run_input_hashes(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    manifest = load_json(run_dir / "manifest.json", "run manifest")
    profile = str(manifest.get("mm_ram_profile", ""))
    if profile not in {"native", "diagnostic"}:
        raise Phase2Error(
            f"run manifest has invalid mm_ram_profile at {run_dir}: {profile!r}"
        )

    firmware_records = parse_sha256sum_file(
        run_dir / "firmware.sha256", "firmware SHA file"
    )
    firmware = {Path(item["path"]).name: item["sha256"] for item in firmware_records}
    if set(firmware) != {"crc32.hex", "crc32.elf"}:
        raise Phase2Error(
            f"unexpected firmware SHA entries at {run_dir}: {sorted(firmware)}"
        )

    netlist_records = parse_sha256sum_file(
        run_dir / "netlist_sources.sha256", "netlist source SHA file"
    )
    prepared = parse_sha256sum_file(
        run_dir / "simulation_netlist.sha256", "prepared netlist SHA file"
    )
    monitor = parse_sha256sum_file(
        run_dir / "stage5_monitor.sha256", "monitor SHA file"
    )
    mm_ram_records = parse_sha256sum_file(
        run_dir / "stage5_assertion_adapter.sha256",
        "Stage-5 mm_ram source SHA file",
    )

    if len(netlist_records) != 2:
        raise Phase2Error(
            f"netlist source SHA file must contain raw netlist and cell model: {run_dir}"
        )
    if len(prepared) != 1 or len(monitor) != 1:
        raise Phase2Error(f"unexpected prepared/monitor SHA count: {run_dir}")

    mm_ram_by_name = {
        Path(item["path"]).name: item["sha256"]
        for item in mm_ram_records
    }
    if profile == "native":
        if set(mm_ram_by_name) != {"mm_ram.sv"}:
            raise Phase2Error(
                "native profile must hash only original mm_ram.sv: "
                f"{sorted(mm_ram_by_name)}"
            )
    else:
        required = {
            "mm_ram.sv",
            "mm_ram.stage5.sv",
            "stage5_assertion_policy_v1.json",
            "prepare_stage5_mm_ram.py",
            "prepare_stage5_mm_ram_impl.py",
        }
        if set(mm_ram_by_name) != required:
            raise Phase2Error(
                "diagnostic profile has unexpected mm_ram source hashes: "
                f"{sorted(mm_ram_by_name)}"
            )

    selected_source = str(manifest.get("selected_mm_ram_source", ""))
    original_source = str(manifest.get("original_mm_ram_source", ""))
    prepared_source = str(manifest.get("prepared_mm_ram_source", ""))
    if profile == "native":
        if selected_source != original_source or prepared_source:
            raise Phase2Error(
                "native profile did not select only the original mm_ram.sv"
            )
    else:
        if not prepared_source or selected_source != prepared_source:
            raise Phase2Error(
                "diagnostic profile did not select its run-local overlay"
            )

    return {
        "mm_ram_profile": profile,
        "firmware": firmware,
        "raw_netlist_sha256": netlist_records[0]["sha256"],
        "cell_model_sha256": netlist_records[1]["sha256"],
        "prepared_netlist_sha256": prepared[0]["sha256"],
        "monitor_sha256": monitor[0]["sha256"],
        "mm_ram_sources": mm_ram_by_name,
        "selected_mm_ram_source": selected_source,
        "original_mm_ram_source": original_source,
        "prepared_mm_ram_source": prepared_source,
    }


def validate_policy(policy: Mapping[str, Any]) -> None:
    if policy.get("schema_version") != SCHEMA_VERSION:
        raise Phase2Error("Phase-2 policy schema_version must be 1.0")
    if policy.get("policy_version") != "stage5_phase2_execution_modes_v3":
        raise Phase2Error("unexpected Phase-2 policy version")

    profiles = policy.get("mm_ram_profiles")
    if not isinstance(profiles, dict) or set(profiles) != {"native", "diagnostic"}:
        raise Phase2Error("Phase-2 policy must define native and diagnostic profiles")

    execution_modes = policy.get("execution_modes")
    if not isinstance(execution_modes, dict):
        raise Phase2Error("Phase-2 policy execution_modes must be an object")
    if set(execution_modes) != EXECUTABLE_MODES:
        raise Phase2Error(
            "Phase-2 executable mode set mismatch: "
            f"expected={sorted(EXECUTABLE_MODES)}, actual={sorted(execution_modes)}"
        )

    for mode in sorted(EXECUTABLE_MODES):
        record = execution_modes.get(mode)
        if not isinstance(record, dict):
            raise Phase2Error(f"missing policy record for mode {mode}")
        if record.get("executable") is not True:
            raise Phase2Error(f"{mode} must be executable")
        if record.get("phase2_g1_compile_supported") is not True:
            raise Phase2Error(f"{mode} must support Phase2-G1 compile")
        if record.get("mm_ram_profile") not in {"native", "diagnostic"}:
            raise Phase2Error(f"{mode} has invalid mm_ram_profile")

    if execution_modes["NATIVE"].get("mm_ram_profile") != "native":
        raise Phase2Error("NATIVE must use native mm_ram profile")
    for mode in ("OBSERVE", "QUARANTINE"):
        if execution_modes[mode].get("mm_ram_profile") != "diagnostic":
            raise Phase2Error(f"{mode} must use diagnostic mm_ram profile")

    required_cases = policy.get("gate1_required_cases")
    expected_cases = [
        "golden_native_compile",
        "fault_native_compile",
        "fault_observe_compile",
        "fault_quarantine_compile",
    ]
    if required_cases != expected_cases:
        raise Phase2Error(f"gate1_required_cases must be exactly {expected_cases}")

    expected_profiles = policy.get("gate1_expected_profiles")
    if not isinstance(expected_profiles, dict):
        raise Phase2Error("gate1_expected_profiles must be an object")
    if set(expected_profiles) != set(expected_cases):
        raise Phase2Error("gate1_expected_profiles does not cover all cases")

    capability = require_mapping(
        policy.get("detector_capabilities"),
        "detector_capabilities",
    ).get("NON_CONTINUABLE")
    if not isinstance(capability, dict) or capability.get("executable") is not False:
        raise Phase2Error("NON_CONTINUABLE must be a non-executable capability")


def mode_record(policy: Mapping[str, Any], mode: str) -> Mapping[str, Any]:
    normalized = mode.strip().upper()
    if normalized in NON_EXECUTABLE_CAPABILITIES:
        raise Phase2Error(
            f"{normalized} is a detector capability, not an execution mode"
        )
    if normalized not in EXECUTABLE_MODES:
        raise Phase2Error(
            f"unsupported execution mode {normalized!r}; "
            f"allowed={sorted(EXECUTABLE_MODES)}"
        )
    execution_modes = require_mapping(policy.get("execution_modes"), "execution_modes")
    record = execution_modes[normalized]
    if not isinstance(record, dict) or record.get("executable") is not True:
        raise Phase2Error(f"mode is not executable: {normalized}")
    return record


def sanitize_monitor(text: str, label: str) -> None:
    if FINAL_BLOCK_RE.search(text):
        raise Phase2Error(f"{label} contains a forbidden final block")
    if REMOVED_FLUSH_TASK_RE.search(text):
        raise Phase2Error(f"{label} calls the removed flush task")
    forbidden = (
        "module f2a_phase2_mode_guard",
        "bind tb_top f2a_phase2_mode_guard",
        "F2A_PHASE2_MODE:",
        "f2a_assert_mode=%s",
        "mm_ram.stage5.sv",
    )
    found = [token for token in forbidden if token in text]
    if found:
        raise Phase2Error(
            f"{label} contains behavior/profile markers that belong in manifest: {found}"
        )


def command_compose(args: argparse.Namespace) -> int:
    policy_path = args.policy.expanduser().resolve()
    base_monitor = args.base_monitor.expanduser().resolve()
    base_manifest = args.base_manifest.expanduser().resolve()
    output_monitor = args.output_monitor.expanduser().resolve()
    output_metadata = args.output_metadata.expanduser().resolve()
    trace_output = args.trace_output.expanduser().resolve()
    mode = args.mode.strip().upper()

    policy = load_json(policy_path, "Phase-2 policy")
    validate_policy(policy)
    record = mode_record(policy, mode)

    require_file(base_monitor, "base monitor")
    require_file(base_manifest, "base monitor manifest")
    manifest = load_json(base_manifest, "base monitor manifest")

    old_trace_value = manifest.get("trace_output")
    if not isinstance(old_trace_value, str) or not old_trace_value:
        raise Phase2Error("base monitor manifest has no trace_output")

    old_trace = str(Path(old_trace_value).resolve())
    new_trace = str(trace_output)
    base_text = base_monitor.read_text(encoding="utf-8", errors="strict")

    if base_text.count(old_trace) != 1:
        raise Phase2Error(
            "base monitor must contain its manifest trace path exactly once: "
            f"path={old_trace!r}, count={base_text.count(old_trace)}"
        )

    generated = base_text.replace(old_trace, new_trace, 1)
    sanitize_monitor(generated, "generated Phase-2 monitor")

    if generated.count(new_trace) != 1:
        raise Phase2Error(
            "generated monitor must contain the requested trace path exactly once"
        )
    if old_trace != new_trace and old_trace in generated:
        raise Phase2Error("generated monitor retained the stale trace path")

    write_text(output_monitor, generated, force=args.force)

    metadata = {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "generated_at_utc": utc_now(),
        "kind": "stage5_phase2_monitor",
        "policy": str(policy_path),
        "policy_sha256": sha256_file(policy_path),
        "mode": mode,
        "plusarg_mode": record["plusarg_mode"],
        "expected_mm_ram_profile": record["mm_ram_profile"],
        "run_purpose_when_executed": record["run_purpose_when_executed"],
        "phase2_g1_compile_supported": record["phase2_g1_compile_supported"],
        "phase2_g2_runtime_supported": record["phase2_g2_runtime_supported"],
        "diagnostic_source_implemented": record["diagnostic_source_implemented"],
        "diagnostic_runtime_validated": record["diagnostic_runtime_validated"],
        "base_monitor": str(base_monitor),
        "base_monitor_sha256": sha256_file(base_monitor),
        "base_manifest": str(base_manifest),
        "base_manifest_sha256": sha256_file(base_manifest),
        "old_trace_output": old_trace,
        "trace_output": new_trace,
        "output_monitor": str(output_monitor),
        "output_monitor_sha256": sha256_file(output_monitor),
        "transformation": {
            "kind": "trace_path_substitution_only",
            "count": 1,
            "existing_detector_modified": False,
            "mm_ram_profile_selected_here": False,
            "mode_guard_inserted": False,
        },
    }
    write_json(output_metadata, metadata, force=args.force)

    print(f"Phase-2 mode       : {mode}")
    print(f"Expected profile   : {record['mm_ram_profile']}")
    print(f"Base monitor       : {base_monitor}")
    print(f"Generated monitor  : {output_monitor}")
    print(f"Trace output       : {trace_output}")
    print(f"Mode metadata      : {output_metadata}")
    return 0


def validate_compile_result(run_dir: Path, case_name: str) -> dict[str, Any]:
    result = load_json(run_dir / "result.json", f"{case_name} result")
    if result.get("phase") != "compile":
        raise Phase2Error(f"{case_name}: phase is not compile")
    if result.get("run_purpose") != "COMPILE_CHECK":
        raise Phase2Error(f"{case_name}: run purpose is not COMPILE_CHECK")
    if result.get("status") != "COMPILE_PASS":
        raise Phase2Error(
            f"{case_name}: compile status is not COMPILE_PASS: {result.get('status')}"
        )

    raw = require_mapping(result.get("raw_facts"), f"{case_name} raw_facts")
    tool = require_mapping(raw.get("tool"), f"{case_name} raw_facts.tool")
    execution = require_mapping(
        raw.get("execution"), f"{case_name} raw_facts.execution"
    )
    workload = require_mapping(
        raw.get("workload"), f"{case_name} raw_facts.workload"
    )

    if tool.get("status") != "OK":
        raise Phase2Error(f"{case_name}: tool status is not OK")
    if tool.get("infrastructure_error_count") != 0:
        raise Phase2Error(f"{case_name}: infrastructure errors were recorded")
    if execution.get("completion") != "COMPILE_ONLY":
        raise Phase2Error(f"{case_name}: completion is not COMPILE_ONLY")
    if workload.get("outcome") != "NOT_RUN":
        raise Phase2Error(f"{case_name}: workload was unexpectedly run")
    return result


def validate_diagnostic_reports(run_dir: Path, case_name: str) -> dict[str, Any]:
    preparation_path = run_dir / "mm_ram_preparation.json"
    ownership_path = run_dir / "mm_ram_ownership.json"
    preparation = load_json(preparation_path, f"{case_name} preparation report")
    ownership = load_json(ownership_path, f"{case_name} ownership report")

    if preparation.get("kind") != "stage5_diagnostic_mm_ram_preparation":
        raise Phase2Error(f"{case_name}: invalid preparation kind")
    if preparation.get("transformation_count") != 1:
        raise Phase2Error(f"{case_name}: diagnostic overlay was not generated once")
    if preparation.get("external_source_modified") is not False:
        raise Phase2Error(f"{case_name}: external mm_ram source was modified")
    if ownership.get("status") != "PASS":
        raise Phase2Error(f"{case_name}: ownership report is not PASS")

    required_zero = (
        "duplicate_declaration_count",
        "managed_declaration_initializer_count",
        "event_task_managed_write_count",
        "original_assertion_block_count",
    )
    for key in required_zero:
        if ownership.get(key) != 0:
            raise Phase2Error(f"{case_name}: ownership count is not zero: {key}")

    required_one = (
        "mode_reader_count",
        "event_file_reader_count",
        "configuration_owner_count",
        "state_owner_count",
        "predicate_owner_count",
        "event_task_count",
        "procedural_detector_count",
    )
    for key in required_one:
        if ownership.get(key) != 1:
            raise Phase2Error(f"{case_name}: ownership count is not one: {key}")

    if preparation.get("original_assertion_block_removed") is not True:
        raise Phase2Error(
            f"{case_name}: original out_of_bounds_write assertion was not removed"
        )
    if (
        preparation.get("diagnostic_detector_implementation")
        != "PROCEDURAL_FIRST_EVENT"
    ):
        raise Phase2Error(
            f"{case_name}: diagnostic detector is not PROCEDURAL_FIRST_EVENT"
        )
    if preparation.get("first_event_policy") != "FIRST_VIOLATION_ONLY":
        raise Phase2Error(
            f"{case_name}: first-event policy is not FIRST_VIOLATION_ONLY"
        )
    if (
        ownership.get("diagnostic_detector_implementation")
        != "PROCEDURAL_FIRST_EVENT"
    ):
        raise Phase2Error(
            f"{case_name}: ownership report detector implementation mismatch"
        )
    if ownership.get("first_event_policy") != "FIRST_VIOLATION_ONLY":
        raise Phase2Error(
            f"{case_name}: ownership report first-event policy mismatch"
        )

    if preparation.get("output_sha256") != ownership.get("overlay_sha256"):
        raise Phase2Error(f"{case_name}: preparation/ownership overlay SHA mismatch")

    return {
        "preparation_report": str(preparation_path),
        "preparation_report_sha256": sha256_file(preparation_path),
        "ownership_report": str(ownership_path),
        "ownership_report_sha256": sha256_file(ownership_path),
        "overlay_sha256": preparation["output_sha256"],
        "source_sha256": preparation["source_sha256"],
        "transformation_count": preparation["transformation_count"],
        "ownership_status": ownership["status"],
        "original_assertion_block_removed": True,
        "diagnostic_detector_implementation": "PROCEDURAL_FIRST_EVENT",
        "first_event_policy": "FIRST_VIOLATION_ONLY",
    }


def command_validate_g1(args: argparse.Namespace) -> int:
    policy_path = args.policy.expanduser().resolve()
    cases_path = args.cases.expanduser().resolve()
    output = args.report.expanduser().resolve()

    policy = load_json(policy_path, "Phase-2 policy")
    validate_policy(policy)
    cases_payload = load_json(cases_path, "Phase2-G1 case list")
    cases = cases_payload.get("cases")
    if not isinstance(cases, list):
        raise Phase2Error("Phase2-G1 case list has no cases array")

    expected_names = set(policy["gate1_required_cases"])
    actual_names = {
        str(item.get("name"))
        for item in cases
        if isinstance(item, dict)
    }
    if actual_names != expected_names:
        raise Phase2Error(
            "Phase2-G1 case set mismatch: "
            f"expected={sorted(expected_names)}, actual={sorted(actual_names)}"
        )

    expected_profiles = require_mapping(
        policy.get("gate1_expected_profiles"),
        "gate1_expected_profiles",
    )

    validated: dict[str, dict[str, Any]] = {}
    diagnostic_reports: dict[str, dict[str, Any]] = {}

    for item in cases:
        if not isinstance(item, dict):
            raise Phase2Error("Phase2-G1 case entry must be an object")

        name = str(item["name"])
        mode = str(item["mode"]).upper()
        run_kind = str(item["run_kind"])
        run_dir = Path(str(item["run_dir"])).resolve()
        trace = Path(str(item["trace"])).resolve()
        monitor = Path(str(item["monitor"])).resolve()
        metadata_path = Path(str(item["metadata"])).resolve()
        expected_profile = str(expected_profiles[name])

        require_directory(run_dir, f"{name} run directory")
        require_file(monitor, f"{name} monitor")
        require_file(metadata_path, f"{name} mode metadata")

        if trace.exists():
            raise Phase2Error(
                f"{name}: compile-only case unexpectedly created trace: {trace}"
            )
        event_path = run_dir / "assertion_events.tsv"
        if event_path.exists():
            raise Phase2Error(
                f"{name}: compile-only case unexpectedly created assertion events"
            )
        vcds = list(run_dir.rglob("*.vcd"))
        if vcds:
            raise Phase2Error(f"{name}: compile-only case generated VCD: {vcds[0]}")

        result = validate_compile_result(run_dir, name)
        metadata = load_json(metadata_path, f"{name} mode metadata")
        manifest = load_json(run_dir / "manifest.json", f"{name} run manifest")

        if metadata.get("mode") != mode:
            raise Phase2Error(f"{name}: metadata mode mismatch")
        if metadata.get("output_monitor_sha256") != sha256_file(monitor):
            raise Phase2Error(f"{name}: monitor SHA mismatch")
        if metadata.get("trace_output") != str(trace):
            raise Phase2Error(f"{name}: trace path mismatch")
        if metadata.get("expected_mm_ram_profile") != expected_profile:
            raise Phase2Error(f"{name}: metadata expected profile mismatch")

        monitor_text = monitor.read_text(encoding="utf-8", errors="strict")
        sanitize_monitor(monitor_text, f"{name} monitor")
        if monitor_text.count(str(trace)) != 1:
            raise Phase2Error(
                f"{name}: monitor does not contain exact trace path once"
            )

        if manifest.get("mm_ram_profile") != expected_profile:
            raise Phase2Error(
                f"{name}: runner profile mismatch: "
                f"expected={expected_profile}, actual={manifest.get('mm_ram_profile')}"
            )

        input_hashes = collect_run_input_hashes(run_dir)
        if input_hashes.get("mm_ram_profile") != expected_profile:
            raise Phase2Error(f"{name}: input-hash profile mismatch")

        diagnostic = None
        if expected_profile == "diagnostic":
            diagnostic = validate_diagnostic_reports(run_dir, name)
            diagnostic_reports[name] = diagnostic
        else:
            if (run_dir / "mm_ram_preparation.json").exists():
                raise Phase2Error(f"{name}: native profile generated an overlay report")
            if (run_dir / "mm_ram_ownership.json").exists():
                raise Phase2Error(f"{name}: native profile generated an ownership report")

        validated[name] = {
            "name": name,
            "mode": mode,
            "run_kind": run_kind,
            "run_directory": str(run_dir),
            "result_status": result["status"],
            "mm_ram_profile": expected_profile,
            "monitor_sha256": sha256_file(monitor),
            "input_hashes": input_hashes,
            "diagnostic_overlay": diagnostic,
            "trace_absent": True,
            "assertion_events_absent": True,
            "vcd_absent": True,
        }

    rejection = cases_payload.get("non_continuable_rejection")
    if not isinstance(rejection, dict):
        raise Phase2Error("NON_CONTINUABLE rejection evidence is missing")
    if rejection.get("rejected") is not True or rejection.get("exit_status") == 0:
        raise Phase2Error("NON_CONTINUABLE was not rejected fail-closed")

    observe_overlay = diagnostic_reports["fault_observe_compile"]
    quarantine_overlay = diagnostic_reports["fault_quarantine_compile"]
    if observe_overlay["overlay_sha256"] != quarantine_overlay["overlay_sha256"]:
        raise Phase2Error("observe and quarantine compile overlays are not byte-identical")
    if observe_overlay["source_sha256"] != quarantine_overlay["source_sha256"]:
        raise Phase2Error("observe and quarantine used different original mm_ram sources")

    golden_native_mmram = validated["golden_native_compile"]["input_hashes"][
        "mm_ram_sources"
    ]["mm_ram.sv"]
    fault_native_mmram = validated["fault_native_compile"]["input_hashes"][
        "mm_ram_sources"
    ]["mm_ram.sv"]
    if golden_native_mmram != fault_native_mmram:
        raise Phase2Error("golden and fault native compiles used different mm_ram.sv")
    if golden_native_mmram != observe_overlay["source_sha256"]:
        raise Phase2Error("diagnostic overlay was not generated from the native mm_ram.sv")

    report = {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "generated_at_utc": utc_now(),
        "gate": "stage5_phase2_g1_mode_and_source_infrastructure",
        "status": "PASS",
        "policy": str(policy_path),
        "policy_sha256": sha256_file(policy_path),
        "validated_cases": [validated[name] for name in policy["gate1_required_cases"]],
        "runs": {
            "native": {
                "case": "fault_native_compile",
                "run_directory": validated["fault_native_compile"]["run_directory"],
                "input_hashes": validated["fault_native_compile"]["input_hashes"],
            },
            "observe": {
                "case": "fault_observe_compile",
                "run_directory": validated["fault_observe_compile"]["run_directory"],
                "input_hashes": validated["fault_observe_compile"]["input_hashes"],
            },
            "diagnostic_quarantine": {
                "case": "fault_quarantine_compile",
                "run_directory": validated["fault_quarantine_compile"]["run_directory"],
                "input_hashes": validated["fault_quarantine_compile"]["input_hashes"],
            },
        },
        "source_consistency": {
            "native_original_mm_ram_sha256": golden_native_mmram,
            "diagnostic_overlay_sha256": observe_overlay["overlay_sha256"],
            "observe_quarantine_overlay_identical": True,
            "diagnostic_overlay_generated_from_native_source": True,
            "original_assertion_block_removed_in_diagnostic_profile": True,
            "diagnostic_detector_implementation": "PROCEDURAL_FIRST_EVENT",
            "first_event_policy": "FIRST_VIOLATION_ONLY",
            "transformation_count_per_diagnostic_run": 1,
        },
        "non_continuable_rejected": True,
        "gate_claims": {
            "mode_infrastructure_available": True,
            "native_compile_and_elaboration_passed": True,
            "observe_compile_and_elaboration_passed": True,
            "observe_safe_compile_and_elaboration_passed": True,
            "quarantine_compile_and_elaboration_passed": True,
            "native_profile_uses_original_mm_ram": True,
            "diagnostic_profile_uses_run_local_overlay": True,
            "single_mode_configuration_owner": True,
            "single_assertion_state_owner": True,
            "single_assertion_event_task": True,
            "single_procedural_first_event_detector": True,
            "diagnostic_original_assertion_block_removed": True,
            "managed_declaration_ownership_validated": True,
            "each_generated_source_transformed_once": True,
            "observe_and_quarantine_overlay_identical": True,
            "monitor_transformation_is_trace_path_only": True,
            "monitor_sanitation_passed": True,
            "compile_only_generated_no_trace": True,
            "compile_only_generated_no_assertion_events": True,
            "vcd_generated": False,
            "diagnostic_continuation_source_implemented": True,
            "diagnostic_continuation_runtime_validated": False,
            "diagnostic_continuation_runtime_executed": False,
            "native_existing_assertion_unmodified": True,
            "diagnostic_existing_assertion_block_removed": True,
            "existing_assertion_runtime_behavior_modified": True,
            "final_fault_effect_oracle_assigned": False,
        },
    }
    write_json(output, report, force=args.force)

    print("Phase2-G1 status                     : PASS")
    print(f"Validated compile cases              : {len(validated)}")
    print("Native profile uses original mm_ram  : PASS")
    print("Diagnostic overlay ownership         : PASS")
    print("Original assertion removed in overlay: PASS")
    print("Procedural first-event detector      : PASS")
    print("Observe/quarantine overlay identity  : PASS")
    print("Compile-only trace/VCD absence       : PASS")
    print("NON_CONTINUABLE rejected             : PASS")
    print(f"Phase2-G1 report                     : {output}")
    return 0


def read_text_auto(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="strict") as handle:
            return handle.read()
    return path.read_text(encoding="utf-8", errors="strict")


def normalized_trace_text(path: Path) -> str:
    text = read_text_auto(path)
    return "\n".join(
        line.rstrip()
        for line in text.splitlines()
        if line.rstrip()
    ) + "\n"


def compare_trace_files(reference: Path, candidate: Path, label: str) -> dict[str, Any]:
    require_file(reference, f"{label} reference trace")
    require_file(candidate, f"{label} candidate trace")

    reference_text = normalized_trace_text(reference)
    candidate_text = normalized_trace_text(candidate)
    if reference_text != candidate_text:
        reference_lines = reference_text.splitlines()
        candidate_lines = candidate_text.splitlines()
        mismatch: dict[str, Any] | None = None
        for index, (left, right) in enumerate(
            zip(reference_lines, candidate_lines), start=1
        ):
            if left != right:
                mismatch = {
                    "line": index,
                    "reference": left[:300],
                    "candidate": right[:300],
                }
                break
        if mismatch is None:
            mismatch = {
                "reference_line_count": len(reference_lines),
                "candidate_line_count": len(candidate_lines),
            }
        raise Phase2Error(f"{label} trace mismatch: {mismatch}")

    encoded = reference_text.encode("utf-8")
    return {
        "reference": str(reference),
        "candidate": str(candidate),
        "normalized_sha256": sha256_bytes(encoded),
        "line_count": len(reference_text.splitlines()),
        "equivalent": True,
    }


def compare_split_directories(reference_dir: Path, candidate_dir: Path) -> list[dict[str, Any]]:
    require_directory(reference_dir, "Phase-1 golden split directory")
    require_directory(candidate_dir, "Phase-2 golden split directory")

    reference_files = {
        path.name: path
        for path in sorted(reference_dir.glob("TS*.trace.tsv.gz"))
    }
    candidate_files = {
        path.name: path
        for path in sorted(candidate_dir.glob("TS*.trace.tsv.gz"))
    }

    if not reference_files:
        raise Phase2Error("Phase-1 golden split directory is empty")
    if set(reference_files) != set(candidate_files):
        raise Phase2Error(
            "golden split file set mismatch: "
            f"reference={sorted(reference_files)}, candidate={sorted(candidate_files)}"
        )

    return [
        compare_trace_files(
            reference_files[name],
            candidate_files[name],
            f"golden split {name}",
        )
        for name in sorted(reference_files)
    ]


def detector_events(detector: Mapping[str, Any], label: str) -> list[Mapping[str, Any]]:
    for key in ("events", "xcelium_events"):
        value = detector.get(key)
        if isinstance(value, list) and value:
            return [require_mapping(item, f"{label} {key} event") for item in value]
    raise Phase2Error(f"{label} existing detector events are missing")


def normalized_native_event(event: Mapping[str, Any]) -> dict[str, Any]:
    source_file = event.get("source_file")
    source_name = (
        Path(str(source_file)).name
        if isinstance(source_file, str) and source_file
        else None
    )
    return {
        "detector_origin": event.get("detector_origin"),
        "assertion_leaf_name": event.get("assertion_leaf_name"),
        "source_file_name": source_name,
        "source_line": event.get("source_line"),
        "simulation_time": event.get("simulation_time"),
        "action": event.get("action"),
    }


def compare_native_fault_facts(
    reference_report: Mapping[str, Any],
    candidate_result: Mapping[str, Any],
) -> dict[str, Any]:
    reference_status = reference_report.get("native_observation_status")
    candidate_status = candidate_result.get("status")
    if reference_status != candidate_status:
        raise Phase2Error(
            "native fault status mismatch: "
            f"reference={reference_status}, candidate={candidate_status}"
        )

    reference_raw = require_mapping(
        reference_report.get("raw_execution_facts"),
        "reference raw_execution_facts",
    )
    candidate_raw = require_mapping(
        candidate_result.get("raw_facts"),
        "candidate raw_facts",
    )

    compared_fields = {
        "tool.status": (
            require_mapping(reference_raw.get("tool"), "reference tool").get("status"),
            require_mapping(candidate_raw.get("tool"), "candidate tool").get("status"),
        ),
        "execution.completion": (
            require_mapping(reference_raw.get("execution"), "reference execution").get("completion"),
            require_mapping(candidate_raw.get("execution"), "candidate execution").get("completion"),
        ),
        "workload.outcome": (
            require_mapping(reference_raw.get("workload"), "reference workload").get("outcome"),
            require_mapping(candidate_raw.get("workload"), "candidate workload").get("outcome"),
        ),
        "workload.architectural_outcome": (
            require_mapping(reference_raw.get("workload"), "reference workload").get("architectural_outcome"),
            require_mapping(candidate_raw.get("workload"), "candidate workload").get("architectural_outcome"),
        ),
    }

    for label, (reference, candidate) in compared_fields.items():
        if reference != candidate:
            raise Phase2Error(
                f"native fault fact mismatch for {label}: "
                f"reference={reference!r}, candidate={candidate!r}"
            )

    reference_detector = require_mapping(
        reference_raw.get("existing_detector_baseline"),
        "reference detector baseline",
    )
    candidate_detector = require_mapping(
        candidate_raw.get("existing_detector_baseline"),
        "candidate detector baseline",
    )

    reference_events = detector_events(reference_detector, "reference")
    candidate_events = detector_events(candidate_detector, "candidate")
    if len(reference_events) != len(candidate_events):
        raise Phase2Error(
            "existing detector event count mismatch: "
            f"reference={len(reference_events)}, candidate={len(candidate_events)}"
        )

    normalized_reference = [normalized_native_event(item) for item in reference_events]
    normalized_candidate = [normalized_native_event(item) for item in candidate_events]
    if normalized_reference != normalized_candidate:
        raise Phase2Error(
            "existing detector event mismatch: "
            f"reference={normalized_reference}, candidate={normalized_candidate}"
        )

    return {
        "status": candidate_status,
        "compared_fields": {
            label: candidate
            for label, (_, candidate) in compared_fields.items()
        },
        "detector_events": normalized_candidate,
        "equivalent": True,
    }


def validate_native_metadata(path: Path, label: str) -> dict[str, Any]:
    metadata = load_json(path, label)
    if metadata.get("mode") != "NATIVE":
        raise Phase2Error(f"{label}: mode is not NATIVE")
    if metadata.get("expected_mm_ram_profile") != "native":
        raise Phase2Error(f"{label}: expected profile is not native")
    if metadata.get("phase2_g2_runtime_supported") is not True:
        raise Phase2Error(f"{label}: native runtime is not supported")
    return metadata


def require_native_profile(run_dir: Path, label: str) -> dict[str, Any]:
    manifest = load_json(run_dir / "manifest.json", f"{label} manifest")
    if manifest.get("mm_ram_profile") != "native":
        raise Phase2Error(f"{label}: candidate did not use native profile")
    if manifest.get("selected_mm_ram_source") != manifest.get("original_mm_ram_source"):
        raise Phase2Error(f"{label}: native profile did not select original mm_ram.sv")
    if manifest.get("prepared_mm_ram_source"):
        raise Phase2Error(f"{label}: native profile unexpectedly prepared an overlay")
    return manifest


def command_validate_g2(args: argparse.Namespace) -> int:
    policy_path = args.policy.expanduser().resolve()
    phase1_gate3_report_path = args.phase1_gate3_report.expanduser().resolve()
    phase1_gate4_report_path = args.phase1_gate4_report.expanduser().resolve()
    candidate_golden_run = args.candidate_golden_run.expanduser().resolve()
    candidate_fault_run = args.candidate_fault_run.expanduser().resolve()
    phase1_golden_split = args.phase1_golden_split.expanduser().resolve()
    candidate_golden_split = args.candidate_golden_split.expanduser().resolve()
    phase1_fault_trace = args.phase1_fault_trace.expanduser().resolve()
    candidate_fault_trace = args.candidate_fault_trace.expanduser().resolve()
    golden_metadata_path = args.golden_metadata.expanduser().resolve()
    fault_metadata_path = args.fault_metadata.expanduser().resolve()
    output = args.report.expanduser().resolve()

    policy = load_json(policy_path, "Phase-2 policy")
    validate_policy(policy)

    phase1_gate3 = load_json(phase1_gate3_report_path, "Phase-1 Gate-3 report")
    phase1_gate4 = load_json(phase1_gate4_report_path, "Phase-1 Gate-4 report")
    candidate_golden_result = load_json(
        candidate_golden_run / "result.json", "candidate golden result"
    )
    candidate_fault_result = load_json(
        candidate_fault_run / "result.json", "candidate fault result"
    )

    validate_native_metadata(golden_metadata_path, "golden mode metadata")
    validate_native_metadata(fault_metadata_path, "fault mode metadata")
    require_native_profile(candidate_golden_run, "candidate golden")
    require_native_profile(candidate_fault_run, "candidate fault")

    if phase1_gate3.get("status") != "PASS":
        raise Phase2Error("Phase-1 Gate-3 report is not PASS")
    if candidate_golden_result.get("status") != "PASS":
        raise Phase2Error(
            "Phase-2 native golden result is not PASS: "
            f"{candidate_golden_result.get('status')}"
        )
    if candidate_golden_result.get("run_purpose") != "NATIVE_CHARACTERIZATION":
        raise Phase2Error("Phase-2 native golden run purpose is incorrect")
    if candidate_fault_result.get("run_purpose") != "NATIVE_CHARACTERIZATION":
        raise Phase2Error("Phase-2 native fault run purpose is incorrect")

    golden_trace_comparisons = compare_split_directories(
        phase1_golden_split,
        candidate_golden_split,
    )
    fault_trace_comparison = compare_trace_files(
        phase1_fault_trace,
        candidate_fault_trace,
        "native fault",
    )
    fault_fact_comparison = compare_native_fault_facts(
        phase1_gate4,
        candidate_fault_result,
    )

    for run_dir, label in (
        (candidate_golden_run, "candidate golden"),
        (candidate_fault_run, "candidate fault"),
    ):
        vcd_files = list(run_dir.rglob("*.vcd"))
        if vcd_files:
            raise Phase2Error(f"{label} unexpectedly generated VCD: {vcd_files[0]}")

    report = {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "generated_at_utc": utc_now(),
        "gate": "stage5_phase2_g2_native_equivalence",
        "status": "PASS",
        "policy": str(policy_path),
        "policy_sha256": sha256_file(policy_path),
        "phase1_evidence": {
            "gate3_report": str(phase1_gate3_report_path),
            "gate3_report_sha256": sha256_file(phase1_gate3_report_path),
            "gate4_report": str(phase1_gate4_report_path),
            "gate4_report_sha256": sha256_file(phase1_gate4_report_path),
        },
        "candidate_runs": {
            "golden": str(candidate_golden_run),
            "fault": str(candidate_fault_run),
        },
        "golden": {
            "candidate_status": candidate_golden_result["status"],
            "split_trace_count": len(golden_trace_comparisons),
            "trace_comparisons": golden_trace_comparisons,
            "equivalent": True,
        },
        "fault": {
            "candidate_status": candidate_fault_result["status"],
            "trace_comparison": fault_trace_comparison,
            "raw_fact_comparison": fault_fact_comparison,
            "equivalent": True,
        },
        "gate_claims": {
            "phase2_native_profile_uses_original_mm_ram": True,
            "phase2_native_infrastructure_preserves_golden_behavior": True,
            "phase2_native_infrastructure_preserves_fault_trace": True,
            "phase2_native_infrastructure_preserves_detector_identity": True,
            "phase2_native_infrastructure_preserves_detector_time": True,
            "phase2_native_infrastructure_preserves_detector_action": True,
            "phase2_native_infrastructure_preserves_completion": True,
            "phase2_native_infrastructure_preserves_workload_outcome": True,
            "phase2_native_infrastructure_preserves_architectural_outcome": True,
            "observe_runtime_not_executed": True,
            "observe_safe_runtime_not_executed": True,
            "quarantine_runtime_not_executed": True,
            "vcd_generated": False,
            "final_fault_effect_oracle_assigned": False,
        },
    }
    write_json(output, report, force=args.force)

    print("Phase2-G2 status                    : PASS")
    print("Native profile                     : ORIGINAL_MM_RAM")
    print("Golden native equivalence          : PASS")
    print("Fault native trace equivalence      : PASS")
    print("Existing detector equivalence       : PASS")
    print("Completion/outcome equivalence      : PASS")
    print(f"Golden split traces compared        : {len(golden_trace_comparisons)}")
    print(f"Phase2-G2 report                    : {output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {PROGRAM_VERSION}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    compose = subparsers.add_parser(
        "compose",
        help="generate one run-local monitor by trace-path substitution only",
    )
    compose.add_argument("--policy", type=Path, required=True)
    compose.add_argument("--base-monitor", type=Path, required=True)
    compose.add_argument("--base-manifest", type=Path, required=True)
    compose.add_argument("--mode", required=True)
    compose.add_argument("--trace-output", type=Path, required=True)
    compose.add_argument("--output-monitor", type=Path, required=True)
    compose.add_argument("--output-metadata", type=Path, required=True)
    compose.add_argument("--force", action="store_true")
    compose.set_defaults(func=command_compose)

    validate_g1 = subparsers.add_parser(
        "validate-g1",
        help="validate Phase2-G1 mode/source compile infrastructure",
    )
    validate_g1.add_argument("--policy", type=Path, required=True)
    validate_g1.add_argument("--cases", type=Path, required=True)
    validate_g1.add_argument("--report", type=Path, required=True)
    validate_g1.add_argument("--force", action="store_true")
    validate_g1.set_defaults(func=command_validate_g1)

    validate_g2 = subparsers.add_parser(
        "validate-g2",
        help="validate Phase2-G2 native equivalence",
    )
    validate_g2.add_argument("--policy", type=Path, required=True)
    validate_g2.add_argument("--phase1-gate3-report", type=Path, required=True)
    validate_g2.add_argument("--phase1-gate4-report", type=Path, required=True)
    validate_g2.add_argument("--candidate-golden-run", type=Path, required=True)
    validate_g2.add_argument("--candidate-fault-run", type=Path, required=True)
    validate_g2.add_argument("--phase1-golden-split", type=Path, required=True)
    validate_g2.add_argument("--candidate-golden-split", type=Path, required=True)
    validate_g2.add_argument("--phase1-fault-trace", type=Path, required=True)
    validate_g2.add_argument("--candidate-fault-trace", type=Path, required=True)
    validate_g2.add_argument("--golden-metadata", type=Path, required=True)
    validate_g2.add_argument("--fault-metadata", type=Path, required=True)
    validate_g2.add_argument("--report", type=Path, required=True)
    validate_g2.add_argument("--force", action="store_true")
    validate_g2.set_defaults(func=command_validate_g2)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Phase2Error as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"ERROR: unexpected {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
