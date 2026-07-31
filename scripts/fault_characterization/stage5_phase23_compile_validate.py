#!/usr/bin/env python3
"""Validate the Phase-2 golden plus three exact-monitor compile/elaboration runs."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


class ValidationError(RuntimeError):
    pass


def import_common(path: Path):
    spec = importlib.util.spec_from_file_location("f2a_phase23_compile_common", path.resolve())
    if spec is None or spec.loader is None:
        raise ValidationError(f"cannot import common validator: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def validate_run(common: Any, run_dir: Path, expected_kind: str) -> dict[str, Any]:
    result = common.read_result(run_dir)
    if result.get("phase") != "compile" or result.get("run_kind") != expected_kind:
        raise ValidationError(f"compile phase/kind mismatch: {run_dir}")
    if result.get("run_purpose") != "COMPILE_CHECK":
        raise ValidationError(f"compile purpose mismatch: {run_dir}")
    if result.get("status") != "COMPILE_PASS" or result.get("xrun_exit_status") != 0:
        raise ValidationError(f"compile/elaboration failed: {run_dir}: {result.get('status')}")
    markers = result.get("markers", {})
    if markers.get("infrastructure_error_count") != 0 or markers.get("runner_error_count") != 0:
        raise ValidationError(f"compile run contains error markers: {run_dir}")
    command = (run_dir / "command.txt").read_text(encoding="utf-8")
    if "-elaborate" not in command:
        raise ValidationError(f"compile run did not use -elaborate: {run_dir}")
    common.require_absent(run_dir / "assertion_events.tsv", "compile assertion-event file")
    retention = common.read_retention(run_dir)
    if retention.get("work_directory_retained") is not False:
        raise ValidationError(f"successful compile retained work: {run_dir}")
    common.require_absent(run_dir / "work", "successful compile work")
    common.validate_no_vcd(run_dir)
    return {
        "run_directory": str(run_dir.resolve()),
        "result": result,
        "input_hashes": common.collect_run_input_hashes(run_dir),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--common", type=Path, required=True)
    parser.add_argument("--golden-run", type=Path, required=True)
    parser.add_argument("--native-run", type=Path, required=True)
    parser.add_argument("--observe-run", type=Path, required=True)
    parser.add_argument("--quarantine-run", type=Path, required=True)
    parser.add_argument("--golden-trace", type=Path, required=True)
    parser.add_argument("--native-trace", type=Path, required=True)
    parser.add_argument("--observe-trace", type=Path, required=True)
    parser.add_argument("--quarantine-trace", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        common = import_common(args.common)
        runs = {
            "golden": validate_run(common, args.golden_run.resolve(), "golden"),
            "native": validate_run(common, args.native_run.resolve(), "fault"),
            "observe": validate_run(common, args.observe_run.resolve(), "fault"),
            "diagnostic_quarantine": validate_run(
                common, args.quarantine_run.resolve(), "fault"
            ),
        }
        for trace in (
            args.golden_trace,
            args.native_trace,
            args.observe_trace,
            args.quarantine_trace,
        ):
            common.require_absent(trace.resolve(), "compile-only trace")
        adapter_hashes = {
            json.dumps(item["input_hashes"]["assertion_adapter"], sort_keys=True)
            for item in runs.values()
        }
        if len(adapter_hashes) != 1:
            raise ValidationError("assertion-adapter hashes differ across compile runs")
        firmware_hashes = {
            json.dumps(item["input_hashes"]["firmware"], sort_keys=True)
            for item in runs.values()
        }
        if len(firmware_hashes) != 1:
            raise ValidationError("firmware hashes differ across compile runs")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    report = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "kind": "stage5_phase23_compile_validation",
        "status": "PASS",
        "runs": runs,
        "contracts": {
            "golden_and_all_three_exact_monitors_compiled": True,
            "compile_plus_elaboration_only": True,
            "no_simulation_trace_created": True,
            "same_assertion_adapter_across_runs": True,
        },
    }
    output = args.report.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("Golden compile/elaboration             : PASS")
    print("Native monitor compile/elaboration     : PASS")
    print("Observe monitor compile/elaboration    : PASS")
    print("Quarantine monitor compile/elaboration : PASS")
    print(f"Compile report                         : {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
