#!/usr/bin/env python3
"""Validate one Stage-5 native/observe/quarantine fault execution."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

VALID_BY_PURPOSE = {
    "NATIVE_CHARACTERIZATION": {
        "OUTPUT_MATCH",
        "OUTPUT_MISMATCH",
        "TIMEOUT",
        "EXISTING_ASSERTION_DETECTED",
    },
    "DIAGNOSTIC_OBSERVE": {
        "DIAGNOSTIC_OUTPUT_MATCH",
        "DIAGNOSTIC_OUTPUT_MISMATCH",
        "DIAGNOSTIC_TIMEOUT",
    },
    "DIAGNOSTIC_QUARANTINE": {
        "DIAGNOSTIC_OUTPUT_MATCH",
        "DIAGNOSTIC_OUTPUT_MISMATCH",
        "DIAGNOSTIC_TIMEOUT",
    },
}
MODE_BY_PURPOSE = {
    "NATIVE_CHARACTERIZATION": "native",
    "DIAGNOSTIC_OBSERVE": "observe",
    "DIAGNOSTIC_QUARANTINE": "diagnostic_quarantine",
}


class ValidationError(RuntimeError):
    pass


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be an object")
    return value


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def import_common(path: Path):
    spec = importlib.util.spec_from_file_location("f2a_phase23_common", path.resolve())
    if spec is None or spec.loader is None:
        raise ValidationError(f"cannot import common validator: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_trace(path: Path, fault_id: str) -> dict[str, int]:
    header = samples = activity = 0
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line_number, raw in enumerate(stream, start=1):
            fields = raw.rstrip("\n").split("\t")
            if not fields or fields == [""]:
                continue
            if fields[0] == "H":
                header += 1
                if fields != ["H", "FAULT", fault_id]:
                    raise ValidationError(f"invalid trace header at line {line_number}: {fields}")
            elif fields[0] == "F" and len(fields) == 8:
                samples += 1
                if fields[1] != fault_id:
                    raise ValidationError(f"wrong fault ID at line {line_number}")
            elif fields[0] in {"FA", "FS"}:
                activity += 1
                if len(fields) < 2 or fields[1] != fault_id:
                    raise ValidationError(f"wrong activity fault ID at line {line_number}")
            else:
                raise ValidationError(f"malformed trace at line {line_number}: {fields}")
    if header != 1 or samples == 0:
        raise ValidationError(f"trace requires one header and samples; header={header}, samples={samples}")
    return {"header_count": header, "sample_count": samples, "activity_count": activity}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--common", type=Path, required=True)
    parser.add_argument("--compile-report", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--fault-id", required=True)
    parser.add_argument("--purpose", choices=sorted(VALID_BY_PURPOSE), required=True)
    parser.add_argument("--require-detector-event", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        common = import_common(args.common)
        run_dir = args.run_dir.resolve()
        result = common.read_result(run_dir)
        expected_mode = MODE_BY_PURPOSE[args.purpose]
        if result.get("phase") != "run" or result.get("run_kind") != "fault":
            raise ValidationError("result is not a fault run")
        if result.get("run_purpose") != args.purpose:
            raise ValidationError("run purpose mismatch")
        if result.get("assertion_mode") != expected_mode:
            raise ValidationError("assertion mode mismatch")
        status = str(result.get("status"))
        if status not in VALID_BY_PURPOSE[args.purpose]:
            raise ValidationError(f"invalid status for {args.purpose}: {status}")

        raw = result.get("raw_facts")
        if not isinstance(raw, dict):
            raise ValidationError("raw_facts missing")
        tool = raw.get("tool", {})
        execution = raw.get("execution", {})
        workload = raw.get("workload", {})
        detector = raw.get("existing_detector_baseline", {})
        intervention = raw.get("intervention", {})
        if tool.get("status") != "OK" or tool.get("infrastructure_error_count") != 0:
            raise ValidationError("tool execution is not valid")
        if execution.get("valid_experiment_execution") is not True:
            raise ValidationError("execution not marked valid")
        if intervention.get("assertion_mode") != expected_mode:
            raise ValidationError("intervention mode mismatch")
        if args.purpose == "NATIVE_CHARACTERIZATION":
            if intervention.get("termination_suppressed") is not False:
                raise ValidationError("native mode suppressed termination")
            if intervention.get("transaction_quarantine") is not False:
                raise ValidationError("native mode quarantined a transaction")
        elif args.purpose == "DIAGNOSTIC_OBSERVE":
            if intervention.get("termination_suppressed") is not True:
                raise ValidationError("observe mode did not suppress termination")
            if intervention.get("transaction_quarantine") is not False:
                raise ValidationError("observe mode quarantined a transaction")
        else:
            if intervention.get("termination_suppressed") is not True:
                raise ValidationError("quarantine mode did not suppress termination")
            if intervention.get("transaction_quarantine") is not True:
                raise ValidationError("quarantine mode did not enable quarantine")

        events = detector.get("events")
        if not isinstance(events, list):
            raise ValidationError("structured detector events missing")
        if args.require_detector_event and not events:
            raise ValidationError("required detector event was not observed")
        for event in events:
            if event.get("detector_origin") != "PREEXISTING_TB_ASSERTION":
                raise ValidationError("unexpected detector origin")
            if event.get("assertion_leaf_name") != "out_of_bounds_write":
                raise ValidationError("unexpected detector name")
            if event.get("detector_reported_effect_hint") != "ILLEGAL_MEMORY_WRITE":
                raise ValidationError("unexpected detector effect hint")
        expected_actions = {
            "NATIVE_CHARACTERIZATION": {"FATAL_TERMINATION"},
            "DIAGNOSTIC_OBSERVE": {"RECORD_ONLY"},
            "DIAGNOSTIC_QUARANTINE": {"RECORD_AND_QUARANTINE"},
        }[args.purpose]
        if events and {event.get("action") for event in events} != expected_actions:
            raise ValidationError("detector action does not match mode")

        trace = args.trace.resolve()
        if not trace.is_file() or trace.stat().st_size == 0:
            raise ValidationError(f"trace missing or empty: {trace}")
        trace_counts = parse_trace(trace, args.fault_id)
        event_file = run_dir / "assertion_events.tsv"
        if not event_file.is_file() or event_file.stat().st_size == 0:
            raise ValidationError("assertion event file missing")
        common.validate_no_vcd(run_dir)
        retention = common.read_retention(run_dir)
        compile_report = common.load_json(args.compile_report.resolve(), "Phase-2 compile report")
        if compile_report.get("status") != "PASS":
            raise ValidationError("compile report is not PASS")
        compile_key = {
            "NATIVE_CHARACTERIZATION": "native",
            "DIAGNOSTIC_OBSERVE": "observe",
            "DIAGNOSTIC_QUARANTINE": "diagnostic_quarantine",
        }[args.purpose]
        compiled_hashes = compile_report["runs"][compile_key]["input_hashes"]
        current_hashes = common.collect_run_input_hashes(run_dir)
        if current_hashes != compiled_hashes:
            raise ValidationError(
                f"{args.purpose} execution inputs differ from exact compiled inputs"
            )
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1

    report = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "kind": "stage5_diagnostic_execution_validation",
        "status": "PASS",
        "fault_id": args.fault_id,
        "run_purpose": args.purpose,
        "assertion_mode": expected_mode,
        "runner_status": status,
        "run_directory": str(run_dir),
        "trace": str(trace),
        "trace_sha256": sha256_file(trace),
        "assertion_events": str(event_file),
        "assertion_events_sha256": sha256_file(event_file),
        "trace_counts": trace_counts,
        "input_hashes": current_hashes,
        "compiled_input_hashes_match": True,
        "detector_event_count": len(events),
        "execution_completion": execution.get("completion"),
        "workload_outcome": workload.get("outcome"),
        "architectural_outcome": workload.get("architectural_outcome"),
        "intervention": intervention,
        "retention": retention,
        "guardrails": {
            "diagnostic_result_is_counterfactual_after_first_event": expected_mode != "native",
            "diagnostic_result_does_not_replace_native_outcome": True,
            "final_multidimensional_oracle_not_assigned_here": True,
        },
    }
    output = args.report.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Purpose              : {args.purpose}")
    print(f"Assertion mode       : {expected_mode}")
    print(f"Runner status        : {status}")
    print(f"Detector events      : {len(events)}")
    print(f"Validation report    : {output}")
    print("Diagnostic validation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
