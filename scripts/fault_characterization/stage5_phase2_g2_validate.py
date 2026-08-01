#!/usr/bin/env python3
"""Validate the minimal Stage-5 Phase2-G2 Native sanity check.

G2 deliberately checks only the facts needed before diagnostic execution:

* the Phase2 golden Native run still passes with the expected CRC signature;
* the Phase2 fault Native run still terminates at the original
  ``out_of_bounds_write`` assertion;
* both runs use the original ``mm_ram.sv`` Native source profile;
* compact traces are produced, no VCD is produced, and no diagnostic mode runs.

G2 does not compare complete traces, classify continuation capability, execute
Observe/Quarantine, or assign a final diagnostic oracle.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


class G2ValidationError(RuntimeError):
    """Controlled G2 validation failure."""


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise G2ValidationError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise G2ValidationError(f"invalid {label} JSON {path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise G2ValidationError(f"{label} must contain one JSON object: {path}")

    return payload


def require_file(path: Path, label: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise G2ValidationError(f"{label} missing or empty: {path}")


def require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise G2ValidationError(f"{label} must be an object")
    return value


def require_value(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise G2ValidationError(
            f"{label} mismatch: expected={expected!r}, actual={actual!r}"
        )


def detector_events(detector: Mapping[str, Any], label: str) -> list[dict[str, Any]]:
    for key in ("events", "xcelium_events"):
        value = detector.get(key)
        if isinstance(value, list) and value:
            result: list[dict[str, Any]] = []
            for index, item in enumerate(value):
                result.append(require_mapping(item, f"{label}.{key}[{index}]"))
            return result

    raise G2ValidationError(f"{label} has no detector events")


def event_summary(event: Mapping[str, Any]) -> dict[str, Any]:
    source_file = event.get("source_file")
    source_name = None
    if isinstance(source_file, str) and source_file:
        source_name = Path(source_file).name

    return {
        "detector_origin": event.get("detector_origin"),
        "assertion_leaf_name": event.get("assertion_leaf_name"),
        "source_file_name": source_name,
        "action": event.get("action"),
        "simulation_time": event.get("simulation_time"),
    }


def validate_native_profile(
    run_dir: Path,
    metadata_path: Path,
    label: str,
) -> dict[str, Any]:
    manifest = load_json(run_dir / "manifest.json", f"{label} manifest")
    metadata = load_json(metadata_path, f"{label} mode metadata")

    require_value(metadata.get("mode"), "NATIVE", f"{label} metadata mode")
    require_value(
        metadata.get("expected_mm_ram_profile"),
        "native",
        f"{label} expected mm_ram profile",
    )

    require_value(
        manifest.get("mm_ram_profile"),
        "native",
        f"{label} manifest mm_ram profile",
    )
    require_value(
        manifest.get("selected_mm_ram_source"),
        manifest.get("original_mm_ram_source"),
        f"{label} selected Native mm_ram source",
    )

    if manifest.get("prepared_mm_ram_source"):
        raise G2ValidationError(
            f"{label} unexpectedly generated a diagnostic mm_ram overlay"
        )

    original_source = Path(str(manifest.get("original_mm_ram_source", "")))
    if original_source.name != "mm_ram.sv":
        raise G2ValidationError(
            f"{label} original source is not mm_ram.sv: {original_source}"
        )

    if (run_dir / "mm_ram_preparation.json").exists():
        raise G2ValidationError(
            f"{label} unexpectedly contains a diagnostic preparation report"
        )
    if (run_dir / "mm_ram_ownership.json").exists():
        raise G2ValidationError(
            f"{label} unexpectedly contains a diagnostic ownership report"
        )

    return {
        "mm_ram_profile": "native",
        "original_mm_ram_source": str(original_source),
        "diagnostic_overlay_absent": True,
    }


def validate_phase1_reference(
    gate3: Mapping[str, Any],
    gate4: Mapping[str, Any],
    fault_id: str,
) -> dict[str, Any]:
    require_value(gate3.get("status"), "PASS", "Phase1 Gate3 status")
    require_value(gate4.get("status"), "PASS", "Phase1 Gate4 status")
    require_value(gate4.get("fault_id"), fault_id, "Phase1 Gate4 fault ID")
    require_value(
        gate4.get("native_observation_status"),
        "EXISTING_ASSERTION_DETECTED",
        "Phase1 native fault status",
    )

    raw = require_mapping(
        gate4.get("raw_execution_facts"),
        "Phase1 Gate4 raw_execution_facts",
    )
    execution = require_mapping(raw.get("execution"), "Phase1 execution")
    workload = require_mapping(raw.get("workload"), "Phase1 workload")
    detector = require_mapping(
        raw.get("existing_detector_baseline"),
        "Phase1 existing detector baseline",
    )

    require_value(
        execution.get("completion"),
        "TERMINATED_BY_EXISTING_ASSERTION",
        "Phase1 completion",
    )
    require_value(workload.get("outcome"), "NOT_REACHED", "Phase1 workload outcome")
    require_value(
        workload.get("architectural_outcome"),
        "CENSORED",
        "Phase1 architectural outcome",
    )

    event = event_summary(detector_events(detector, "Phase1 detector")[0])
    require_value(
        event.get("detector_origin"),
        "PREEXISTING_TB_ASSERTION",
        "Phase1 detector origin",
    )
    require_value(
        event.get("assertion_leaf_name"),
        "out_of_bounds_write",
        "Phase1 detector name",
    )
    require_value(event.get("action"), "FATAL_TERMINATION", "Phase1 detector action")

    return {
        "fault_status": "EXISTING_ASSERTION_DETECTED",
        "completion": "TERMINATED_BY_EXISTING_ASSERTION",
        "workload_outcome": "NOT_REACHED",
        "architectural_outcome": "CENSORED",
        "detector": event,
    }


def validate_golden_result(result: Mapping[str, Any]) -> dict[str, Any]:
    require_value(result.get("phase"), "run", "golden phase")
    require_value(result.get("run_kind"), "golden", "golden run kind")
    require_value(
        result.get("run_purpose"),
        "NATIVE_CHARACTERIZATION",
        "golden run purpose",
    )
    require_value(result.get("assertion_mode"), "native", "golden assertion mode")
    require_value(result.get("status"), "PASS", "golden result")

    raw = require_mapping(result.get("raw_facts"), "golden raw_facts")
    tool = require_mapping(raw.get("tool"), "golden tool facts")
    require_value(tool.get("status"), "OK", "golden tool status")

    markers = require_mapping(result.get("markers"), "golden markers")
    exact_signature_count = int(markers.get("exact_signature_count", 0))
    exit_success_count = int(markers.get("exit_success_count", 0))

    if exact_signature_count < 1:
        raise G2ValidationError("golden run did not contain the expected CRC signature")
    if exit_success_count < 1:
        raise G2ValidationError("golden run did not reach EXIT SUCCESS")

    return {
        "status": "PASS",
        "tool_status": "OK",
        "expected_crc_signature_present": True,
        "exit_success_present": True,
    }


def validate_fault_result(
    result: Mapping[str, Any],
    phase1_reference: Mapping[str, Any],
) -> dict[str, Any]:
    require_value(result.get("phase"), "run", "fault phase")
    require_value(result.get("run_kind"), "fault", "fault run kind")
    require_value(
        result.get("run_purpose"),
        "NATIVE_CHARACTERIZATION",
        "fault run purpose",
    )
    require_value(result.get("assertion_mode"), "native", "fault assertion mode")
    require_value(
        result.get("status"),
        phase1_reference["fault_status"],
        "fault result",
    )

    raw = require_mapping(result.get("raw_facts"), "fault raw_facts")
    tool = require_mapping(raw.get("tool"), "fault tool facts")
    execution = require_mapping(raw.get("execution"), "fault execution facts")
    workload = require_mapping(raw.get("workload"), "fault workload facts")
    detector = require_mapping(
        raw.get("existing_detector_baseline"),
        "fault existing detector baseline",
    )

    require_value(tool.get("status"), "OK", "fault tool status")
    require_value(
        execution.get("completion"),
        phase1_reference["completion"],
        "fault completion",
    )
    require_value(
        workload.get("outcome"),
        phase1_reference["workload_outcome"],
        "fault workload outcome",
    )
    require_value(
        workload.get("architectural_outcome"),
        phase1_reference["architectural_outcome"],
        "fault architectural outcome",
    )

    event = event_summary(detector_events(detector, "fault detector")[0])
    reference_event = require_mapping(
        phase1_reference.get("detector"),
        "Phase1 detector summary",
    )

    for key in ("detector_origin", "assertion_leaf_name", "action"):
        require_value(event.get(key), reference_event.get(key), f"fault detector {key}")

    require_value(
        event.get("source_file_name"),
        "mm_ram.sv",
        "fault detector source file",
    )

    return {
        "status": result.get("status"),
        "tool_status": "OK",
        "completion": execution.get("completion"),
        "workload_outcome": workload.get("outcome"),
        "architectural_outcome": workload.get("architectural_outcome"),
        "detector": event,
        "matches_phase1_key_facts": True,
    }


def validate_no_vcd(run_dir: Path, label: str) -> None:
    vcd_files = list(run_dir.rglob("*.vcd"))
    if vcd_files:
        raise G2ValidationError(f"{label} unexpectedly generated VCD: {vcd_files[0]}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--g1-report", type=Path, required=True)
    parser.add_argument("--phase1-gate3-report", type=Path, required=True)
    parser.add_argument("--phase1-gate4-report", type=Path, required=True)
    parser.add_argument("--golden-run", type=Path, required=True)
    parser.add_argument("--fault-run", type=Path, required=True)
    parser.add_argument("--golden-trace", type=Path, required=True)
    parser.add_argument("--fault-trace", type=Path, required=True)
    parser.add_argument("--golden-metadata", type=Path, required=True)
    parser.add_argument("--fault-metadata", type=Path, required=True)
    parser.add_argument("--fault-id", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        g1_report = load_json(args.g1_report.resolve(), "Phase2-G1 report")
        require_value(g1_report.get("status"), "PASS", "Phase2-G1 status")
        g1_claims = require_mapping(g1_report.get("gate_claims"), "Phase2-G1 claims")
        require_value(
            g1_claims.get("native_profile_uses_original_mm_ram"),
            True,
            "G1 Native original mm_ram claim",
        )
        require_value(
            g1_claims.get("native_existing_assertion_unmodified"),
            True,
            "G1 Native assertion-unmodified claim",
        )
        require_value(
            g1_claims.get("diagnostic_continuation_runtime_executed"),
            False,
            "G1 diagnostic runtime execution claim",
        )

        gate3 = load_json(args.phase1_gate3_report.resolve(), "Phase1 Gate3 report")
        gate4 = load_json(args.phase1_gate4_report.resolve(), "Phase1 Gate4 report")
        phase1_reference = validate_phase1_reference(gate3, gate4, args.fault_id)

        golden_run = args.golden_run.resolve()
        fault_run = args.fault_run.resolve()
        golden_result = load_json(golden_run / "result.json", "golden result")
        fault_result = load_json(fault_run / "result.json", "fault result")

        golden_profile = validate_native_profile(
            golden_run,
            args.golden_metadata.resolve(),
            "golden Native run",
        )
        fault_profile = validate_native_profile(
            fault_run,
            args.fault_metadata.resolve(),
            "fault Native run",
        )

        golden_summary = validate_golden_result(golden_result)
        fault_summary = validate_fault_result(fault_result, phase1_reference)

        golden_trace = args.golden_trace.resolve()
        fault_trace = args.fault_trace.resolve()
        require_file(golden_trace, "golden compact trace")
        require_file(fault_trace, "fault compact trace")

        validate_no_vcd(golden_run, "golden Native run")
        validate_no_vcd(fault_run, "fault Native run")

        output = args.report.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "1.0",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "gate": "stage5_phase2_g2_minimal_native_sanity",
            "status": "PASS",
            "fault_id": args.fault_id,
            "purpose": (
                "Confirm that the Phase2 framework preserves the minimal "
                "golden and fault Native baseline before diagnostic execution."
            ),
            "golden": golden_summary,
            "fault": fault_summary,
            "native_profiles": {
                "golden": golden_profile,
                "fault": fault_profile,
            },
            "traces": {
                "golden": str(golden_trace),
                "fault": str(fault_trace),
                "generated_and_nonempty": True,
                "full_trace_equivalence_not_required_by_g2": True,
            },
            "gate_claims": {
                "golden_native_passed": True,
                "golden_expected_crc_signature_present": True,
                "fault_native_reproduced_phase1_key_facts": True,
                "fault_existing_detector_is_out_of_bounds_write": True,
                "native_runs_used_original_mm_ram": True,
                "diagnostic_overlay_not_used": True,
                "observe_runtime_not_executed": True,
                "quarantine_runtime_not_executed": True,
                "vcd_generated": False,
                "final_diagnostic_oracle_assigned": False,
            },
        }
        output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    except G2ValidationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"ERROR: unexpected {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    print("Phase2-G2 minimal Native sanity: PASS")
    print("Golden Native                    : PASS + expected CRC")
    print("Fault Native                     : EXISTING_ASSERTION_DETECTED")
    print("Existing detector                : out_of_bounds_write")
    print("Native completion                : TERMINATED_BY_EXISTING_ASSERTION")
    print("Native architectural outcome     : CENSORED")
    print("Native source profile            : original mm_ram.sv")
    print("Observe/Quarantine runtime       : NOT EXECUTED")
    print(f"Validation report                : {args.report.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
