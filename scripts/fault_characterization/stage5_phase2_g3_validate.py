#!/usr/bin/env python3
"""Validate the minimal Stage-5 Phase2-G3 OBSERVE experiment.

G3 checks only the facts needed to characterize OBSERVE for the smoke fault:

* the frozen G2 Native sanity report is PASS;
* the run uses DIAGNOSTIC_OBSERVE with the diagnostic mm_ram profile;
* the original out_of_bounds_write assertion is absent from the overlay;
* one procedural first-event detector record is produced with RECORD_ONLY;
* termination is suppressed and transaction quarantine is disabled;
* the final run result is DIAGNOSTIC_OUTPUT_MATCH,
  DIAGNOSTIC_OUTPUT_MISMATCH, or DIAGNOSTIC_TIMEOUT.

G3 does not run DIAGNOSTIC_QUARANTINE and does not assign a final diagnostic
oracle or continuation capability.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ALLOWED_RESULTS = {
    "DIAGNOSTIC_OUTPUT_MATCH": "OUTPUT_MATCH",
    "DIAGNOSTIC_OUTPUT_MISMATCH": "OUTPUT_MISMATCH",
    "DIAGNOSTIC_TIMEOUT": "TIMEOUT",
}


class G3ValidationError(RuntimeError):
    """Controlled G3 validation failure."""


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise G3ValidationError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise G3ValidationError(f"invalid {label} JSON {path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise G3ValidationError(f"{label} must contain one JSON object: {path}")

    return payload


def require_file(path: Path, label: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise G3ValidationError(f"{label} missing or empty: {path}")


def require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise G3ValidationError(f"{label} must be an object")
    return value


def require_value(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise G3ValidationError(
            f"{label} mismatch: expected={expected!r}, actual={actual!r}"
        )


def require_one_structured_event(detector: Mapping[str, Any]) -> dict[str, Any]:
    events = detector.get("events")
    if not isinstance(events, list):
        raise G3ValidationError("structured detector events are missing")
    if len(events) != 1:
        raise G3ValidationError(
            f"OBSERVE must record exactly one first detector event; found {len(events)}"
        )
    event = events[0]
    if not isinstance(event, dict):
        raise G3ValidationError("structured detector event must be an object")
    return event


def validate_g2(g2_report: Mapping[str, Any], fault_id: str) -> None:
    require_value(g2_report.get("status"), "PASS", "Phase2-G2 status")
    require_value(g2_report.get("fault_id"), fault_id, "Phase2-G2 fault ID")

    claims = require_mapping(g2_report.get("gate_claims"), "Phase2-G2 claims")
    required_true = (
        "golden_native_passed",
        "golden_expected_crc_signature_present",
        "fault_native_reproduced_phase1_key_facts",
        "fault_existing_detector_is_out_of_bounds_write",
        "native_runs_used_original_mm_ram",
        "diagnostic_overlay_not_used",
        "observe_runtime_not_executed",
        "quarantine_runtime_not_executed",
    )
    for key in required_true:
        require_value(claims.get(key), True, f"Phase2-G2 claim {key}")

    require_value(
        claims.get("final_diagnostic_oracle_assigned"),
        False,
        "Phase2-G2 final diagnostic oracle claim",
    )


def validate_metadata(metadata: Mapping[str, Any]) -> None:
    require_value(metadata.get("mode"), "OBSERVE", "mode metadata")
    require_value(
        metadata.get("plusarg_mode"),
        "observe",
        "mode metadata plusarg",
    )
    require_value(
        metadata.get("expected_mm_ram_profile"),
        "diagnostic",
        "mode metadata mm_ram profile",
    )
    require_value(
        metadata.get("run_purpose_when_executed"),
        "DIAGNOSTIC_OBSERVE",
        "mode metadata run purpose",
    )


def validate_manifest(manifest: Mapping[str, Any], fault_id: str) -> dict[str, str]:
    require_value(manifest.get("phase"), "run", "run manifest phase")
    require_value(manifest.get("run_kind"), "fault", "run manifest kind")
    require_value(
        manifest.get("run_purpose"),
        "DIAGNOSTIC_OBSERVE",
        "run manifest purpose",
    )
    require_value(manifest.get("mm_ram_profile"), "diagnostic", "run profile")
    require_value(manifest.get("assertion_mode"), "observe", "run assertion mode")
    require_value(manifest.get("fault_id"), fault_id, "run manifest fault ID")

    original_source = str(manifest.get("original_mm_ram_source", ""))
    prepared_source = str(manifest.get("prepared_mm_ram_source", ""))
    selected_source = str(manifest.get("selected_mm_ram_source", ""))

    if Path(original_source).name != "mm_ram.sv":
        raise G3ValidationError(
            f"original mm_ram source is unexpected: {original_source!r}"
        )
    if Path(prepared_source).name != "mm_ram.stage5.sv":
        raise G3ValidationError(
            f"diagnostic overlay path is unexpected: {prepared_source!r}"
        )
    require_value(
        selected_source,
        prepared_source,
        "selected diagnostic mm_ram source",
    )
    if selected_source == original_source:
        raise G3ValidationError("OBSERVE selected the original mm_ram instead of overlay")

    return {
        "original_mm_ram_source": original_source,
        "prepared_mm_ram_source": prepared_source,
        "selected_mm_ram_source": selected_source,
    }


def validate_overlay_reports(
    preparation: Mapping[str, Any],
    ownership: Mapping[str, Any],
) -> dict[str, Any]:
    require_value(
        preparation.get("kind"),
        "stage5_diagnostic_mm_ram_preparation",
        "preparation report kind",
    )
    require_value(
        preparation.get("original_assertion_block_removed"),
        True,
        "original assertion removal",
    )
    require_value(
        preparation.get("diagnostic_detector_implementation"),
        "PROCEDURAL_FIRST_EVENT",
        "diagnostic detector implementation",
    )
    require_value(
        preparation.get("first_event_policy"),
        "FIRST_VIOLATION_ONLY",
        "diagnostic first-event policy",
    )
    require_value(
        preparation.get("external_source_modified"),
        False,
        "external source modification",
    )

    require_value(ownership.get("status"), "PASS", "ownership report status")
    require_value(
        ownership.get("original_assertion_block_count"),
        0,
        "diagnostic original assertion block count",
    )
    require_value(
        ownership.get("procedural_detector_count"),
        1,
        "diagnostic procedural detector count",
    )
    require_value(
        ownership.get("diagnostic_detector_implementation"),
        "PROCEDURAL_FIRST_EVENT",
        "ownership detector implementation",
    )
    require_value(
        ownership.get("first_event_policy"),
        "FIRST_VIOLATION_ONLY",
        "ownership first-event policy",
    )

    return {
        "original_assertion_removed": True,
        "diagnostic_detector_implementation": "PROCEDURAL_FIRST_EVENT",
        "first_event_policy": "FIRST_VIOLATION_ONLY",
        "procedural_detector_count": 1,
        "external_source_modified": False,
    }


def validate_result(result: Mapping[str, Any]) -> dict[str, Any]:
    require_value(result.get("phase"), "run", "result phase")
    require_value(result.get("run_kind"), "fault", "result run kind")
    require_value(
        result.get("run_purpose"),
        "DIAGNOSTIC_OBSERVE",
        "result run purpose",
    )
    require_value(result.get("assertion_mode"), "observe", "result mode")

    runner_status = str(result.get("status", ""))
    if runner_status not in ALLOWED_RESULTS:
        raise G3ValidationError(
            "invalid OBSERVE result: "
            f"{runner_status!r}; allowed={sorted(ALLOWED_RESULTS)}"
        )

    raw = require_mapping(result.get("raw_facts"), "result raw_facts")
    tool = require_mapping(raw.get("tool"), "tool facts")
    execution = require_mapping(raw.get("execution"), "execution facts")
    workload = require_mapping(raw.get("workload"), "workload facts")
    detector = require_mapping(
        raw.get("existing_detector_baseline"),
        "existing detector facts",
    )
    intervention = require_mapping(raw.get("intervention"), "intervention facts")

    require_value(tool.get("status"), "OK", "tool status")
    require_value(tool.get("infrastructure_error_count"), 0, "infrastructure errors")
    require_value(
        execution.get("valid_experiment_execution"),
        True,
        "valid experiment execution",
    )
    require_value(execution.get("diagnostic_execution"), True, "diagnostic execution")

    require_value(
        detector.get("structured_event_count"),
        1,
        "structured detector event count",
    )
    require_value(
        detector.get("xcelium_assertion_event_count"),
        0,
        "Xcelium assertion event count in diagnostic overlay",
    )

    event = require_one_structured_event(detector)
    require_value(
        event.get("detector_origin"),
        "PREEXISTING_TB_ASSERTION",
        "detector origin",
    )
    require_value(
        event.get("assertion_leaf_name"),
        "out_of_bounds_write",
        "detector name",
    )
    require_value(
        event.get("detector_reported_effect_hint"),
        "ILLEGAL_MEMORY_WRITE",
        "detector effect hint",
    )
    require_value(event.get("action"), "RECORD_ONLY", "OBSERVE detector action")

    require_value(intervention.get("assertion_mode"), "observe", "intervention mode")
    require_value(
        intervention.get("termination_suppressed"),
        True,
        "OBSERVE termination suppression",
    )
    require_value(
        intervention.get("transaction_quarantine"),
        False,
        "OBSERVE transaction quarantine",
    )
    require_value(
        intervention.get("counterfactual_after_first_detector_event"),
        True,
        "counterfactual marker",
    )
    require_value(
        intervention.get("intervention_applied"),
        True,
        "OBSERVE intervention applied",
    )

    return {
        "runner_status": runner_status,
        "observe_outcome": ALLOWED_RESULTS[runner_status],
        "execution_completion": execution.get("completion"),
        "workload_outcome": workload.get("outcome"),
        "architectural_outcome": workload.get("architectural_outcome"),
        "detector_event": event,
        "termination_suppressed": True,
        "transaction_quarantine": False,
        "counterfactual_after_first_event": True,
    }


def validate_no_vcd(root: Path) -> None:
    vcd_files = list(root.rglob("*.vcd"))
    if vcd_files:
        raise G3ValidationError(f"G3 unexpectedly generated VCD: {vcd_files[0]}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--g2-report", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--fault-id", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        g2_report = load_json(args.g2_report.resolve(), "Phase2-G2 report")
        validate_g2(g2_report, args.fault_id)

        run_dir = args.run_dir.resolve()
        result = load_json(run_dir / "result.json", "OBSERVE result")
        manifest = load_json(run_dir / "manifest.json", "OBSERVE manifest")
        metadata = load_json(args.metadata.resolve(), "OBSERVE mode metadata")
        preparation = load_json(
            run_dir / "mm_ram_preparation.json",
            "diagnostic preparation report",
        )
        ownership = load_json(
            run_dir / "mm_ram_ownership.json",
            "diagnostic ownership report",
        )

        validate_metadata(metadata)
        profile_summary = validate_manifest(manifest, args.fault_id)
        overlay_summary = validate_overlay_reports(preparation, ownership)
        observe_summary = validate_result(result)

        trace = args.trace.resolve()
        event_file = run_dir / "assertion_events.tsv"
        require_file(trace, "OBSERVE compact trace")
        require_file(event_file, "OBSERVE assertion event file")
        validate_no_vcd(run_dir)

        output = args.report.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "1.0",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "gate": "stage5_phase2_g3_minimal_observe",
            "status": "PASS",
            "fault_id": args.fault_id,
            "purpose": (
                "Record the existing out_of_bounds_write detector event and "
                "observe what happens when its fatal action is removed without "
                "enabling transaction quarantine."
            ),
            "observe": observe_summary,
            "diagnostic_profile": profile_summary,
            "diagnostic_overlay": overlay_summary,
            "artifacts": {
                "run_directory": str(run_dir),
                "trace": str(trace),
                "assertion_events": str(event_file),
                "mode_metadata": str(args.metadata.resolve()),
                "vcd_generated": False,
            },
            "gate_claims": {
                "g2_native_sanity_passed": True,
                "diagnostic_observe_executed": True,
                "diagnostic_profile_used": True,
                "original_target_assertion_removed": True,
                "procedural_first_event_detector_recorded_once": True,
                "existing_detector_is_out_of_bounds_write": True,
                "detector_action_is_record_only": True,
                "termination_suppressed": True,
                "transaction_quarantine_enabled": False,
                "observe_result_is_valid_scientific_outcome": True,
                "diagnostic_quarantine_runtime_executed": False,
                "vcd_generated": False,
                "final_diagnostic_oracle_assigned": False,
                "continuation_capability_assigned": False,
            },
        }
        output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    except G3ValidationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"ERROR: unexpected {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    print("Phase2-G3 minimal OBSERVE experiment: PASS")
    print(f"Fault ID                         : {args.fault_id}")
    print("Detector                         : out_of_bounds_write")
    print("Detector action                  : RECORD_ONLY")
    print("Termination suppressed           : YES")
    print("Transaction quarantine           : NO")
    print(f"OBSERVE runner status             : {observe_summary['runner_status']}")
    print(f"OBSERVE outcome                   : {observe_summary['observe_outcome']}")
    print(f"Execution completion              : {observe_summary['execution_completion']}")
    print(f"Architectural outcome             : {observe_summary['architectural_outcome']}")
    print("DIAGNOSTIC_QUARANTINE runtime     : NOT EXECUTED")
    print("Final diagnostic oracle           : NOT ASSIGNED")
    print(f"Validation report                 : {args.report.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
