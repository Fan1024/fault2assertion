#!/usr/bin/env python3
"""Stage-5 Phase-2 execution-mode infrastructure and Gate validation.

This tool does not modify the design or the external cv32e40p testbench.

Phase2-G1:
    - generate mode-aware run-local monitors;
    - compile/elaborate NATIVE, OBSERVE_SAFE, and QUARANTINE configurations;
    - reject NON_CONTINUABLE as an executable mode;
    - validate monitor sanitation and artifact provenance.

Phase2-G2:
    - run only NATIVE mode;
    - compare the Phase-2 native golden and fault executions with the frozen
      Phase-1 evidence;
    - require exact compact-trace equivalence and equivalent detector facts.

OBSERVE_SAFE and QUARANTINE runtime behavior remains intentionally unimplemented
until Phase2-G3.
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
from typing import Any, Iterable, Mapping, Sequence


PROGRAM_VERSION = "1.0.0"
SCHEMA_VERSION = "1.0"

EXECUTABLE_MODES = {"NATIVE", "OBSERVE_SAFE", "QUARANTINE"}
NON_EXECUTABLE_CAPABILITIES = {"NON_CONTINUABLE"}

FINAL_BLOCK_RE = re.compile(r"\bfinal\s+(?:begin|:)", re.IGNORECASE)
REMOVED_FLUSH_TASK_RE = re.compile(r"::\s*flush\s*\(\s*\)\s*;")
MODE_MARKER_RE = re.compile(
    r"F2A_PHASE2_MODE:\s+"
    r"mode=(?P<mode>[A-Z_]+)\s+"
    r"behavior_implemented=(?P<implemented>[01])"
)


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
    path = path.resolve()
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
    path = path.resolve()
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


def validate_policy(policy: Mapping[str, Any]) -> None:
    if policy.get("schema_version") != "1.0":
        raise Phase2Error("Phase-2 policy schema_version must be 1.0")
    if policy.get("policy_version") != "stage5_phase2_execution_modes_v1":
        raise Phase2Error("unexpected Phase-2 policy version")

    execution_modes = policy.get("execution_modes")
    if not isinstance(execution_modes, dict):
        raise Phase2Error("Phase-2 policy execution_modes must be an object")
    if set(execution_modes) != EXECUTABLE_MODES:
        raise Phase2Error(
            "Phase-2 executable mode set mismatch: "
            f"expected={sorted(EXECUTABLE_MODES)}, "
            f"actual={sorted(execution_modes)}"
        )

    for mode in sorted(EXECUTABLE_MODES):
        record = execution_modes.get(mode)
        if not isinstance(record, dict):
            raise Phase2Error(f"missing policy record for mode {mode}")
        if record.get("executable") is not True:
            raise Phase2Error(f"{mode} must be executable")
        if record.get("phase2_g1_compile_supported") is not True:
            raise Phase2Error(f"{mode} must support Phase2-G1 compile")

    native = execution_modes["NATIVE"]
    if native.get("phase2_g2_runtime_supported") is not True:
        raise Phase2Error("NATIVE must support Phase2-G2 runtime")
    if native.get("continuation_behavior_implemented") is not True:
        raise Phase2Error("NATIVE behavior must be implemented")

    for mode in ("OBSERVE_SAFE", "QUARANTINE"):
        record = execution_modes[mode]
        if record.get("phase2_g2_runtime_supported") is not False:
            raise Phase2Error(f"{mode} runtime must be disabled before Gate 3")
        if record.get("continuation_behavior_implemented") is not False:
            raise Phase2Error(
                f"{mode} continuation behavior must remain unimplemented"
            )

    capabilities = policy.get("detector_capabilities")
    if not isinstance(capabilities, dict):
        raise Phase2Error("detector_capabilities must be an object")
    non_continuable = capabilities.get("NON_CONTINUABLE")
    if not isinstance(non_continuable, dict):
        raise Phase2Error("NON_CONTINUABLE capability is missing")
    if non_continuable.get("executable") is not False:
        raise Phase2Error("NON_CONTINUABLE must not be executable")


def mode_record(
    policy: Mapping[str, Any],
    mode: str,
) -> Mapping[str, Any]:
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

    execution_modes = policy["execution_modes"]
    record = execution_modes[normalized]
    if not isinstance(record, dict) or record.get("executable") is not True:
        raise Phase2Error(f"mode is not executable: {normalized}")
    return record


def sanitize_monitor(text: str, label: str) -> None:
    if FINAL_BLOCK_RE.search(text):
        raise Phase2Error(f"{label} contains a forbidden final block")
    if REMOVED_FLUSH_TASK_RE.search(text):
        raise Phase2Error(f"{label} calls the removed flush task")
    if text.count("module f2a_phase2_mode_guard;") != 1:
        raise Phase2Error(f"{label} does not contain exactly one mode guard")
    if text.count("bind tb_top f2a_phase2_mode_guard") != 1:
        raise Phase2Error(f"{label} does not contain exactly one mode bind")


def build_mode_source(mode: str, implemented: bool) -> tuple[str, str]:
    implemented_bit = "1'b1" if implemented else "1'b0"
    implemented_digit = "1" if implemented else "0"

    prefix = f"""\
`timescale 1ns/1ps

// -------------------------------------------------------------------------
// Fault2Assertion Stage-5 Phase-2 execution-mode metadata.
//
// This package and guard do not change design or testbench behavior.
// OBSERVE_SAFE and QUARANTINE are compile-only infrastructure in Phase2-G1.
// Their continuation semantics are implemented later in Phase2-G3.
// -------------------------------------------------------------------------
package f2a_phase2_mode_pkg;
  localparam string MODE = "{mode}";
  localparam bit CONTINUATION_BEHAVIOR_IMPLEMENTED = {implemented_bit};
endpackage

"""

    suffix = f"""\

// -------------------------------------------------------------------------
// Fault2Assertion Stage-5 Phase-2 elaborated mode guard.
// -------------------------------------------------------------------------
module f2a_phase2_mode_guard;
  initial begin
    $display(
      "F2A_PHASE2_MODE: mode={mode} behavior_implemented={implemented_digit}"
    );
  end
endmodule

bind tb_top f2a_phase2_mode_guard f2a_phase2_mode_guard_i();

"""
    return prefix, suffix


def command_compose(args: argparse.Namespace) -> int:
    policy_path = args.policy.resolve()
    base_monitor = args.base_monitor.resolve()
    base_manifest = args.base_manifest.resolve()
    output_monitor = args.output_monitor.resolve()
    output_metadata = args.output_metadata.resolve()
    trace_output = args.trace_output.resolve()
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
    old_count = base_text.count(old_trace)
    if old_count != 1:
        raise Phase2Error(
            "base monitor must contain its manifest trace path exactly once; "
            f"path={old_trace!r}, occurrences={old_count}, monitor={base_monitor}"
        )

    rewritten_base = base_text.replace(old_trace, new_trace, 1)

    implemented = bool(record["continuation_behavior_implemented"])
    prefix, suffix = build_mode_source(mode, implemented)
    combined = prefix + rewritten_base.rstrip() + "\n" + suffix

    sanitize_monitor(combined, "generated Phase-2 monitor")

    if combined.count(new_trace) != 1:
        raise Phase2Error(
            "generated monitor must contain the requested trace path exactly once"
        )
    if old_trace != new_trace and old_trace in combined:
        raise Phase2Error("generated monitor retained the stale trace path")

    write_text(output_monitor, combined, force=args.force)

    metadata = {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "generated_at_utc": utc_now(),
        "kind": "stage5_phase2_mode_monitor",
        "policy": str(policy_path),
        "policy_sha256": sha256_file(policy_path),
        "mode": mode,
        "mode_executable": True,
        "run_purpose_when_executed": record["run_purpose_when_executed"],
        "continuation_behavior_implemented": implemented,
        "phase2_g1_compile_supported": record["phase2_g1_compile_supported"],
        "phase2_g2_runtime_supported": record["phase2_g2_runtime_supported"],
        "base_monitor": str(base_monitor),
        "base_monitor_sha256": sha256_file(base_monitor),
        "base_manifest": str(base_manifest),
        "base_manifest_sha256": sha256_file(base_manifest),
        "old_trace_output": old_trace,
        "trace_output": new_trace,
        "output_monitor": str(output_monitor),
        "output_monitor_sha256": sha256_file(output_monitor),
        "sanitation": {
            "final_block_absent": True,
            "removed_flush_task_absent": True,
            "mode_guard_count": 1,
            "tb_top_bind_count": 1,
            "requested_trace_path_count": 1,
            "stale_trace_path_absent": old_trace == new_trace
            or old_trace not in combined,
        },
        "behavior_guardrail": {
            "design_behavior_modified": False,
            "testbench_detector_behavior_modified": False,
            "observe_safe_runtime_implemented": False,
            "quarantine_runtime_implemented": False,
        },
    }
    write_json(output_metadata, metadata, force=args.force)

    print(f"Phase-2 mode        : {mode}")
    print(f"Behavior implemented: {implemented}")
    print(f"Base monitor        : {base_monitor}")
    print(f"Generated monitor   : {output_monitor}")
    print(f"Trace output        : {trace_output}")
    print(f"Mode metadata       : {output_metadata}")
    return 0


def validate_result_compile(run_dir: Path, case_name: str) -> dict[str, Any]:
    result = load_json(run_dir / "result.json", f"{case_name} result")
    if result.get("phase") != "compile":
        raise Phase2Error(f"{case_name}: phase is not compile")
    if result.get("run_purpose") != "COMPILE_CHECK":
        raise Phase2Error(f"{case_name}: run purpose is not COMPILE_CHECK")
    if result.get("status") != "COMPILE_PASS":
        raise Phase2Error(
            f"{case_name}: compile status is not COMPILE_PASS: "
            f"{result.get('status')}"
        )

    raw = result.get("raw_facts")
    if not isinstance(raw, dict):
        raise Phase2Error(f"{case_name}: raw_facts missing")
    tool = raw.get("tool")
    execution = raw.get("execution")
    workload = raw.get("workload")
    if not isinstance(tool, dict):
        raise Phase2Error(f"{case_name}: raw_facts.tool missing")
    if not isinstance(execution, dict):
        raise Phase2Error(f"{case_name}: raw_facts.execution missing")
    if not isinstance(workload, dict):
        raise Phase2Error(f"{case_name}: raw_facts.workload missing")

    if tool.get("status") != "OK":
        raise Phase2Error(f"{case_name}: tool status is not OK")
    if tool.get("infrastructure_error_count") != 0:
        raise Phase2Error(f"{case_name}: infrastructure errors were recorded")
    if execution.get("completion") != "COMPILE_ONLY":
        raise Phase2Error(f"{case_name}: completion is not COMPILE_ONLY")
    if workload.get("outcome") != "NOT_RUN":
        raise Phase2Error(f"{case_name}: workload was unexpectedly run")
    return result


def command_validate_g1(args: argparse.Namespace) -> int:
    policy_path = args.policy.resolve()
    cases_path = args.cases.resolve()
    output = args.report.resolve()

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

    validated: list[dict[str, Any]] = []
    for item in cases:
        if not isinstance(item, dict):
            raise Phase2Error("Phase2-G1 case entry must be an object")

        name = str(item["name"])
        mode = str(item["mode"])
        run_kind = str(item["run_kind"])
        run_dir = Path(str(item["run_dir"])).resolve()
        trace = Path(str(item["trace"])).resolve()
        monitor = Path(str(item["monitor"])).resolve()
        metadata_path = Path(str(item["metadata"])).resolve()

        require_directory(run_dir, f"{name} run directory")
        require_file(monitor, f"{name} monitor")
        require_file(metadata_path, f"{name} mode metadata")

        if trace.exists():
            raise Phase2Error(
                f"{name}: compile-only case unexpectedly created trace: {trace}"
            )

        result = validate_result_compile(run_dir, name)
        metadata = load_json(metadata_path, f"{name} mode metadata")

        if metadata.get("mode") != mode:
            raise Phase2Error(f"{name}: metadata mode mismatch")
        if metadata.get("output_monitor_sha256") != sha256_file(monitor):
            raise Phase2Error(f"{name}: monitor SHA mismatch")
        if metadata.get("trace_output") != str(trace):
            raise Phase2Error(f"{name}: trace path mismatch")

        monitor_text = monitor.read_text(encoding="utf-8", errors="strict")
        sanitize_monitor(monitor_text, f"{name} monitor")
        if monitor_text.count(str(trace)) != 1:
            raise Phase2Error(
                f"{name}: monitor does not contain exact trace path once"
            )

        expected_implemented = mode == "NATIVE"
        if metadata.get("continuation_behavior_implemented") is not expected_implemented:
            raise Phase2Error(
                f"{name}: continuation implementation flag is incorrect"
            )

        validated.append(
            {
                "name": name,
                "mode": mode,
                "run_kind": run_kind,
                "run_directory": str(run_dir),
                "monitor_sha256": sha256_file(monitor),
                "result_status": result["status"],
                "trace_absent": True,
            }
        )

    rejection = cases_payload.get("non_continuable_rejection")
    if not isinstance(rejection, dict):
        raise Phase2Error("NON_CONTINUABLE rejection evidence is missing")
    if rejection.get("rejected") is not True:
        raise Phase2Error("NON_CONTINUABLE was not rejected")
    if rejection.get("exit_status") == 0:
        raise Phase2Error("NON_CONTINUABLE rejection returned zero")

    report = {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "generated_at_utc": utc_now(),
        "gate": "stage5_phase2_g1_mode_infrastructure",
        "status": "PASS",
        "policy": str(policy_path),
        "policy_sha256": sha256_file(policy_path),
        "validated_cases": validated,
        "non_continuable_rejected": True,
        "gate_claims": {
            "mode_infrastructure_available": True,
            "native_compile_and_elaboration_passed": True,
            "observe_safe_compile_and_elaboration_passed": True,
            "quarantine_compile_and_elaboration_passed": True,
            "non_continuable_is_not_execution_mode": True,
            "monitor_sanitation_passed": True,
            "compile_only_generated_no_trace": True,
            "diagnostic_continuation_runtime_implemented": False,
            "existing_assertion_behavior_modified": False,
        },
    }
    write_json(output, report, force=args.force)

    print("Phase2-G1 status                 : PASS")
    print(f"Validated compile cases          : {len(validated)}")
    print("NON_CONTINUABLE rejected         : PASS")
    print("Diagnostic runtime implemented   : NO")
    print(f"Phase2-G1 report                 : {output}")
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


def compare_trace_files(
    reference: Path,
    candidate: Path,
    label: str,
) -> dict[str, Any]:
    require_file(reference, f"{label} reference trace")
    require_file(candidate, f"{label} candidate trace")

    reference_text = normalized_trace_text(reference)
    candidate_text = normalized_trace_text(candidate)
    if reference_text != candidate_text:
        reference_lines = reference_text.splitlines()
        candidate_lines = candidate_text.splitlines()
        mismatch = None
        for index, (left, right) in enumerate(
            zip(reference_lines, candidate_lines),
            start=1,
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


def compare_split_directories(
    reference_dir: Path,
    candidate_dir: Path,
) -> list[dict[str, Any]]:
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
            f"reference={sorted(reference_files)}, "
            f"candidate={sorted(candidate_files)}"
        )

    return [
        compare_trace_files(
            reference_files[name],
            candidate_files[name],
            f"golden split {name}",
        )
        for name in sorted(reference_files)
    ]


def require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Phase2Error(f"{label} must be an object")
    return value


def normalized_detector_event(event: Mapping[str, Any]) -> dict[str, Any]:
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
            require_mapping(
                reference_raw.get("execution"), "reference execution"
            ).get("completion"),
            require_mapping(
                candidate_raw.get("execution"), "candidate execution"
            ).get("completion"),
        ),
        "workload.outcome": (
            require_mapping(
                reference_raw.get("workload"), "reference workload"
            ).get("outcome"),
            require_mapping(
                candidate_raw.get("workload"), "candidate workload"
            ).get("outcome"),
        ),
        "workload.architectural_outcome": (
            require_mapping(
                reference_raw.get("workload"), "reference workload"
            ).get("architectural_outcome"),
            require_mapping(
                candidate_raw.get("workload"), "candidate workload"
            ).get("architectural_outcome"),
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

    reference_events = reference_detector.get("events")
    candidate_events = candidate_detector.get("events")
    if not isinstance(reference_events, list) or not reference_events:
        raise Phase2Error("reference existing detector events are missing")
    if not isinstance(candidate_events, list) or not candidate_events:
        raise Phase2Error("candidate existing detector events are missing")
    if len(reference_events) != len(candidate_events):
        raise Phase2Error(
            "existing detector event count mismatch: "
            f"reference={len(reference_events)}, "
            f"candidate={len(candidate_events)}"
        )

    normalized_reference = [
        normalized_detector_event(require_mapping(item, "reference event"))
        for item in reference_events
    ]
    normalized_candidate = [
        normalized_detector_event(require_mapping(item, "candidate event"))
        for item in candidate_events
    ]
    if normalized_reference != normalized_candidate:
        raise Phase2Error(
            "existing detector event mismatch: "
            f"reference={normalized_reference}, "
            f"candidate={normalized_candidate}"
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
    if metadata.get("continuation_behavior_implemented") is not True:
        raise Phase2Error(f"{label}: native behavior is not implemented")
    if metadata.get("phase2_g2_runtime_supported") is not True:
        raise Phase2Error(f"{label}: native runtime is not supported")
    return metadata


def require_native_log_marker(log_path: Path, label: str) -> None:
    require_file(log_path, label)
    text = log_path.read_text(encoding="utf-8", errors="replace")
    matches = list(MODE_MARKER_RE.finditer(text))
    if len(matches) != 1:
        raise Phase2Error(
            f"{label}: expected one Phase-2 mode marker; found {len(matches)}"
        )
    match = matches[0]
    if match.group("mode") != "NATIVE":
        raise Phase2Error(f"{label}: runtime mode marker is not NATIVE")
    if match.group("implemented") != "1":
        raise Phase2Error(f"{label}: native implementation marker is not 1")


def command_validate_g2(args: argparse.Namespace) -> int:
    policy_path = args.policy.resolve()
    phase1_gate3_report_path = args.phase1_gate3_report.resolve()
    phase1_gate4_report_path = args.phase1_gate4_report.resolve()
    candidate_golden_run = args.candidate_golden_run.resolve()
    candidate_fault_run = args.candidate_fault_run.resolve()
    phase1_golden_split = args.phase1_golden_split.resolve()
    candidate_golden_split = args.candidate_golden_split.resolve()
    phase1_fault_trace = args.phase1_fault_trace.resolve()
    candidate_fault_trace = args.candidate_fault_trace.resolve()
    golden_metadata_path = args.golden_metadata.resolve()
    fault_metadata_path = args.fault_metadata.resolve()
    output = args.report.resolve()

    policy = load_json(policy_path, "Phase-2 policy")
    validate_policy(policy)

    phase1_gate3 = load_json(
        phase1_gate3_report_path,
        "Phase-1 Gate-3 report",
    )
    phase1_gate4 = load_json(
        phase1_gate4_report_path,
        "Phase-1 Gate-4 report",
    )
    candidate_golden_result = load_json(
        candidate_golden_run / "result.json",
        "candidate golden result",
    )
    candidate_fault_result = load_json(
        candidate_fault_run / "result.json",
        "candidate fault result",
    )

    validate_native_metadata(golden_metadata_path, "golden mode metadata")
    validate_native_metadata(fault_metadata_path, "fault mode metadata")

    require_native_log_marker(
        candidate_golden_run / "xrun.log",
        "candidate golden xrun.log",
    )
    require_native_log_marker(
        candidate_fault_run / "xrun.log",
        "candidate fault xrun.log",
    )

    if phase1_gate3.get("status") != "PASS":
        raise Phase2Error("Phase-1 Gate-3 report is not PASS")
    if candidate_golden_result.get("status") != "PASS":
        raise Phase2Error(
            "Phase-2 native golden result is not PASS: "
            f"{candidate_golden_result.get('status')}"
        )
    if candidate_golden_result.get("run_purpose") != "NATIVE_CHARACTERIZATION":
        raise Phase2Error(
            "Phase-2 native golden run purpose is incorrect"
        )

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

    if candidate_fault_result.get("run_purpose") != "NATIVE_CHARACTERIZATION":
        raise Phase2Error("Phase-2 native fault run purpose is incorrect")

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
            "phase2_native_infrastructure_preserves_golden_behavior": True,
            "phase2_native_infrastructure_preserves_fault_trace": True,
            "phase2_native_infrastructure_preserves_detector_identity": True,
            "phase2_native_infrastructure_preserves_detector_time": True,
            "phase2_native_infrastructure_preserves_detector_action": True,
            "phase2_native_infrastructure_preserves_completion": True,
            "phase2_native_infrastructure_preserves_workload_outcome": True,
            "phase2_native_infrastructure_preserves_architectural_outcome": True,
            "observe_safe_runtime_not_executed": True,
            "quarantine_runtime_not_executed": True,
            "vcd_generated": False,
            "final_fault_effect_oracle_assigned": False,
        },
    }
    write_json(output, report, force=args.force)

    print("Phase2-G2 status                    : PASS")
    print("Golden native equivalence           : PASS")
    print("Fault native trace equivalence       : PASS")
    print("Existing detector equivalence        : PASS")
    print("Completion/outcome equivalence       : PASS")
    print(f"Golden split traces compared         : {len(golden_trace_comparisons)}")
    print(f"Phase2-G2 report                     : {output}")
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
        help="generate one mode-aware run-local monitor",
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
        help="validate Phase2-G1 mode compile/elaboration",
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
