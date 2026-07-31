#!/usr/bin/env python3
"""Validate Stage-5 Gate 4 native single-fault execution.

Gate 4 validates experiment integrity and raw execution facts.  It does not
assign a final fault-effect oracle class.  In particular, a pre-existing
assertion that terminates a fault run is accepted as a valid existing-detector
baseline observation while the workload/architectural outcome remains censored.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

FAULT_RE = re.compile(r"^TF\d{6}_SA[01]$")
VALID_STATUSES = {
    "OUTPUT_MATCH",
    "OUTPUT_MISMATCH",
    "TIMEOUT",
    "EXISTING_ASSERTION_DETECTED",
}
ALLOWED_EXISTING_DETECTOR_ORIGINS = {
    "PREEXISTING_TB_ASSERTION",
    "PREEXISTING_DESIGN_ASSERTION",
    "PREEXISTING_ASSERTION_UNKNOWN_ORIGIN",
}


class Gate4Error(RuntimeError):
    pass


def load_common(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("f2a_gate_common", path.resolve())
    if spec is None or spec.loader is None:
        raise Gate4Error(f"cannot import validation common module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Gate4Error(f"{label} must be a JSON object")
    return value


def validate_layered_result(result: dict[str, Any], status: str) -> dict[str, Any]:
    if result.get("schema_version") != "2.0":
        raise Gate4Error(
            f"Gate 4 requires result schema 2.0; got {result.get('schema_version')!r}"
        )
    if result.get("verdict_engine_version") != "3.0.0":
        raise Gate4Error(
            "Gate 4 requires verdict engine 3.0.0; got "
            f"{result.get('verdict_engine_version')!r}"
        )
    if result.get("run_purpose") != "NATIVE_CHARACTERIZATION":
        raise Gate4Error(
            "Gate 4 requires NATIVE_CHARACTERIZATION; got "
            f"{result.get('run_purpose')!r}"
        )

    raw = require_mapping(result.get("raw_facts"), "raw_facts")
    tool = require_mapping(raw.get("tool"), "raw_facts.tool")
    execution = require_mapping(raw.get("execution"), "raw_facts.execution")
    workload = require_mapping(raw.get("workload"), "raw_facts.workload")
    detector = require_mapping(
        raw.get("existing_detector_baseline"),
        "raw_facts.existing_detector_baseline",
    )

    if tool.get("status") != "OK":
        raise Gate4Error(f"fault execution tool status is not OK: {tool.get('status')}")
    if tool.get("runner_error_count") != 0:
        raise Gate4Error("runner invariant error was recorded")
    if tool.get("infrastructure_error_count") != 0:
        raise Gate4Error("Xcelium infrastructure error was recorded")
    if execution.get("valid_experiment_execution") is not True:
        raise Gate4Error("fault run is not marked as a valid experiment execution")
    if execution.get("native_execution") is not True:
        raise Gate4Error("Gate 4 result is not marked as native execution")
    if execution.get("post_terminal_execution_observed") is not False:
        raise Gate4Error("native Gate 4 must not claim post-terminal execution")

    events = detector.get("events")
    if not isinstance(events, list):
        raise Gate4Error("existing detector events must be an array")

    if status == "OUTPUT_MATCH":
        if execution.get("completion") != "COMPLETED":
            raise Gate4Error("OUTPUT_MATCH completion is not COMPLETED")
        if workload.get("outcome") != "PASS":
            raise Gate4Error("OUTPUT_MATCH workload outcome is not PASS")
        if workload.get("architectural_outcome") != "OBSERVED_PASS":
            raise Gate4Error("OUTPUT_MATCH architectural outcome is not OBSERVED_PASS")
        if result.get("xrun_exit_status") != 0:
            raise Gate4Error("OUTPUT_MATCH has non-zero xrun status")
    elif status == "OUTPUT_MISMATCH":
        if execution.get("completion") != "COMPLETED":
            raise Gate4Error("OUTPUT_MISMATCH completion is not COMPLETED")
        if workload.get("outcome") != "FAIL":
            raise Gate4Error("OUTPUT_MISMATCH workload outcome is not FAIL")
        if workload.get("architectural_outcome") != "OBSERVED_FAIL":
            raise Gate4Error(
                "OUTPUT_MISMATCH architectural outcome is not OBSERVED_FAIL"
            )
    elif status == "TIMEOUT":
        if execution.get("completion") != "TIMED_OUT":
            raise Gate4Error("TIMEOUT completion is not TIMED_OUT")
        if workload.get("outcome") != "NOT_REACHED":
            raise Gate4Error("TIMEOUT workload outcome is not NOT_REACHED")
        if workload.get("architectural_outcome") != "CENSORED":
            raise Gate4Error("TIMEOUT architectural outcome is not CENSORED")
    elif status == "EXISTING_ASSERTION_DETECTED":
        if execution.get("completion") != "TERMINATED_BY_EXISTING_ASSERTION":
            raise Gate4Error(
                "existing assertion result lacks "
                "TERMINATED_BY_EXISTING_ASSERTION completion"
            )
        if workload.get("outcome") != "NOT_REACHED":
            raise Gate4Error(
                "assertion-terminated workload outcome must be NOT_REACHED"
            )
        if workload.get("architectural_outcome") != "CENSORED":
            raise Gate4Error(
                "assertion-terminated architectural outcome must be CENSORED"
            )
        if detector.get("triggered") is not True:
            raise Gate4Error("existing detector baseline is not marked triggered")
        if detector.get("event_count") != len(events) or len(events) < 1:
            raise Gate4Error("existing detector event count is inconsistent or empty")
        for index, event in enumerate(events):
            if not isinstance(event, dict):
                raise Gate4Error(f"detector event {index} is not an object")
            origin = event.get("detector_origin")
            if origin not in ALLOWED_EXISTING_DETECTOR_ORIGINS:
                raise Gate4Error(
                    f"detector event {index} has unsupported origin: {origin!r}"
                )
            if event.get("mnemonic") != "ASRTST":
                raise Gate4Error(
                    f"detector event {index} is not an ASRTST event"
                )
            if event.get("action") != "FATAL_TERMINATION":
                raise Gate4Error(
                    f"detector event {index} did not record fatal termination"
                )
    return raw


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--common", type=Path, required=True)
    parser.add_argument("--selection-record", type=Path, required=True)
    parser.add_argument("--gate2-report", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        common = load_common(args.common)
        selection = common.load_json(
            args.selection_record.resolve(), "smoke-fault selection"
        )
        fault_id = str(selection.get("fault_id", ""))
        if not FAULT_RE.fullmatch(fault_id):
            raise Gate4Error(f"invalid selected fault ID: {fault_id!r}")
        run_dir = args.run_dir.resolve()
        result = common.read_result(run_dir)
        if result.get("phase") != "run" or result.get("run_kind") != "fault":
            raise Gate4Error("Gate 4 result is not a fault run-phase result")
        status = str(result.get("status"))
        if status not in VALID_STATUSES:
            raise Gate4Error(
                "single-fault run is not a valid native scientific observation: "
                f"status={status}"
            )
        raw_facts = validate_layered_result(result, status)
        markers = result.get("markers", {})

        fault_copy = common.load_json(run_dir / "fault.json", "run fault spec")
        if fault_copy.get("fault_id") != fault_id:
            raise Gate4Error("run fault.json does not match selected fault")
        if fault_copy.get("fault_spec_digest_sha256") != selection.get(
            "fault_spec_digest_sha256"
        ):
            raise Gate4Error("run fault digest does not match selection record")

        trace = args.trace.resolve()
        common.require_file(trace, "single-fault compact trace")
        header_count = 0
        sample_count = 0
        activity_count = 0
        wrong_ids: list[str] = []
        for line_number, fields in common.iter_trace_rows(trace):
            if fields[0] == "H":
                header_count += 1
                if len(fields) != 3 or fields[1] != "FAULT" or fields[2] != fault_id:
                    raise Gate4Error(
                        f"invalid fault header at {trace}:{line_number}: {fields}"
                    )
                continue
            if fields[0] == "F" and len(fields) == 8:
                sample_count += 1
            elif fields[0] in {"FA", "FS"}:
                activity_count += 1
            else:
                raise Gate4Error(
                    f"malformed fault trace row at {trace}:{line_number}: {fields}"
                )
            if len(fields) < 2 or fields[1] != fault_id:
                wrong_ids.append(str(fields[1] if len(fields) > 1 else "<missing>"))
        if header_count != 1:
            raise Gate4Error(
                f"fault trace must contain exactly one header; got {header_count}"
            )
        if sample_count == 0:
            raise Gate4Error("fault trace contains no cycle samples")
        if wrong_ids:
            raise Gate4Error(f"fault trace contains wrong fault IDs: {wrong_ids[:5]}")

        retention = common.read_retention(run_dir)
        work_exists = (run_dir / "work").is_dir()
        if status in {"OUTPUT_MATCH", "OUTPUT_MISMATCH"}:
            if retention.get("work_directory_retained") is not False or work_exists:
                raise Gate4Error(
                    f"completed run retained work unexpectedly: {status}"
                )
        elif status in {"TIMEOUT", "EXISTING_ASSERTION_DETECTED"}:
            if retention.get("work_directory_retained") is not True or not work_exists:
                raise Gate4Error(
                    f"censored native run did not retain work: {status}"
                )

        bundle_present = (run_dir / "reproduction_bundle.tar.gz").is_file()
        if status == "OUTPUT_MATCH":
            if bundle_present:
                raise Gate4Error("OUTPUT_MATCH should not create a reproduction bundle")
        else:
            common.validate_bundle(run_dir, status)
        common.validate_no_vcd(run_dir)
        gate2 = common.load_json(args.gate2_report.resolve(), "Gate-2 report")
        if gate2.get("status") != "PASS":
            raise Gate4Error("Gate-2 report is not PASS")
        current_input_hashes = common.collect_run_input_hashes(run_dir)
        if current_input_hashes != gate2.get("fault_input_hashes"):
            raise Gate4Error(
                "Gate-4 fault execution inputs differ from Gate-2 compiled inputs"
            )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    detector_baseline = raw_facts["existing_detector_baseline"]
    report = {
        "schema_version": "2.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "gate": "stage5_gate4_native_single_fault",
        "status": "PASS",
        "fault_id": fault_id,
        "native_observation_status": status,
        "run_directory": str(run_dir),
        "trace": str(trace),
        "trace_sha256": common.sha256_file(trace),
        "input_hashes": current_input_hashes,
        "compiled_input_hashes_match": True,
        "trace_header_count": header_count,
        "trace_sample_count": sample_count,
        "trace_activity_count": activity_count,
        "work_directory_retained": work_exists,
        "reproduction_bundle_present": bundle_present,
        "vcd_files_generated": 0,
        "raw_execution_facts": raw_facts,
        "existing_detector_baseline": detector_baseline,
        "oracle_guardrail": {
            "final_fault_effect_class_assigned": False,
            "architectural_outcome_censored": (
                raw_facts["workload"]["architectural_outcome"] == "CENSORED"
            ),
            "existing_detector_trigger_is_not_final_oracle": True,
        },
    }
    output = args.report.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Gate 4 fault                         : {fault_id}")
    print(f"Gate 4 native observation            : {status}")
    print(f"Gate 4 execution completion          : {raw_facts['execution']['completion']}")
    print(f"Gate 4 workload outcome              : {raw_facts['workload']['outcome']}")
    print(
        "Gate 4 architectural outcome         : "
        f"{raw_facts['workload']['architectural_outcome']}"
    )
    print(
        "Gate 4 existing detector events      : "
        f"{detector_baseline['event_count']}"
    )
    print("Gate 4 infrastructure validity       : PASS")
    print(f"Gate 4 report                        : {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
