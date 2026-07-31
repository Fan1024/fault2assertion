#!/usr/bin/env python3
"""Validate Stage-5 Gate 2 compile/elaboration-only golden and fault runs."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


class Gate2Error(RuntimeError):
    pass


def load_common(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("f2a_gate_common", path.resolve())
    if spec is None or spec.loader is None:
        raise Gate2Error(f"cannot import validation common module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def validate_run(common: Any, run_dir: Path, expected_kind: str) -> dict[str, Any]:
    if not run_dir.is_dir():
        raise Gate2Error(f"run directory not found: {run_dir}")
    result = common.read_result(run_dir)
    if result.get("phase") != "compile":
        raise Gate2Error(f"expected compile phase: {run_dir}")
    if result.get("run_kind") != expected_kind:
        raise Gate2Error(
            f"run-kind mismatch: expected={expected_kind}, actual={result.get('run_kind')}"
        )
    if result.get("status") != "COMPILE_PASS":
        raise Gate2Error(
            f"compile/elaboration did not pass: {run_dir}: {result.get('status')}"
        )
    if result.get("xrun_exit_status") != 0:
        raise Gate2Error(f"compile/elaboration xrun status was non-zero: {run_dir}")
    if result.get("markers", {}).get("infrastructure_error_count") != 0:
        raise Gate2Error(f"compile log contains infrastructure errors: {run_dir}")
    if result.get("markers", {}).get("runner_error_count") != 0:
        raise Gate2Error(f"compile log contains runner invariant errors: {run_dir}")

    command = (run_dir / "command.txt").read_text(encoding="utf-8")
    if "-elaborate" not in command:
        raise Gate2Error(f"compile gate did not use -elaborate: {run_dir}")
    if "EXIT SUCCESS" in (run_dir / "xrun.log").read_text(
        encoding="utf-8", errors="replace"
    ):
        raise Gate2Error(f"compile-only run unexpectedly entered simulation: {run_dir}")

    retention = common.read_retention(run_dir)
    if retention.get("work_directory_retained") is not False:
        raise Gate2Error(f"successful compile work directory was retained: {run_dir}")
    common.require_absent(run_dir / "work", "successful compile work directory")
    common.require_absent(
        run_dir / "reproduction_bundle.tar.gz",
        "compile-pass reproduction bundle",
    )
    common.require_absent(
        run_dir / "reproduction_bundle_manifest.json",
        "compile-pass bundle manifest",
    )
    common.validate_no_vcd(run_dir)
    return {
        "result": result,
        "input_hashes": common.collect_run_input_hashes(run_dir),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--common", type=Path, required=True)
    parser.add_argument("--golden-run", type=Path, required=True)
    parser.add_argument("--fault-run", type=Path, required=True)
    parser.add_argument("--golden-trace", type=Path, required=True)
    parser.add_argument("--fault-trace", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        common = load_common(args.common)
        golden_result = validate_run(common, args.golden_run.resolve(), "golden")
        fault_result = validate_run(common, args.fault_run.resolve(), "fault")
        common.require_absent(args.golden_trace.resolve(), "golden compile trace")
        common.require_absent(args.fault_trace.resolve(), "fault compile trace")
    except (Gate2Error, Exception) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    report = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "gate": "stage5_gate2_compile_elaboration_only",
        "status": "PASS",
        "golden_run": str(args.golden_run.resolve()),
        "fault_run": str(args.fault_run.resolve()),
        "golden_result": golden_result["result"],
        "fault_result": fault_result["result"],
        "golden_input_hashes": golden_result["input_hashes"],
        "fault_input_hashes": fault_result["input_hashes"],
        "simulation_entered": False,
        "trace_files_generated": 0,
        "vcd_files_generated": 0,
    }
    output = args.report.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("Gate 2 golden compile/elaboration : PASS")
    print("Gate 2 fault compile/elaboration  : PASS")
    print(f"Gate 2 report                     : {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
