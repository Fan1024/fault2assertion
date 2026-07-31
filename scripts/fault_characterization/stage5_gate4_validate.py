#!/usr/bin/env python3
"""Validate Stage-5 Gate 4 single-fault functional execution."""

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
VALID_STATUSES = {"OUTPUT_MATCH", "OUTPUT_MISMATCH", "TIMEOUT"}


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
                f"single-fault run is an infrastructure failure: status={status}"
            )
        markers = result.get("markers", {})
        if markers.get("runner_error_count", 0) != 0:
            raise Gate4Error("runner invariant error was recorded")
        if markers.get("infrastructure_error_count", 0) != 0:
            raise Gate4Error("Xcelium infrastructure error was recorded")
        if status == "OUTPUT_MATCH":
            if result.get("xrun_exit_status") != 0:
                raise Gate4Error("OUTPUT_MATCH has non-zero xrun status")
            if markers.get("exact_signature_count", 0) < 1:
                raise Gate4Error("OUTPUT_MATCH lacks exact frozen signature")
            if markers.get("exit_success_count", 0) < 1:
                raise Gate4Error("OUTPUT_MATCH lacks EXIT SUCCESS")

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
                    f"scientifically valid completed run retained work unexpectedly: {status}"
                )
        elif status == "TIMEOUT":
            if retention.get("work_directory_retained") is not True or not work_exists:
                raise Gate4Error("timeout did not retain its work directory")

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

    report = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "gate": "stage5_gate4_single_fault",
        "status": "PASS",
        "fault_id": fault_id,
        "scientific_result": status,
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
    }
    output = args.report.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Gate 4 fault                         : {fault_id}")
    print(f"Gate 4 scientific result             : {status}")
    print("Gate 4 infrastructure validity       : PASS")
    print(f"Gate 4 report                        : {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
