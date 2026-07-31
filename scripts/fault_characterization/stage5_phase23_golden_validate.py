#!/usr/bin/env python3
"""Validate Phase-2 golden regression under the mode-aware testbench."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


class ValidationError(RuntimeError):
    pass


def import_common(path: Path):
    spec = importlib.util.spec_from_file_location("f2a_phase23_golden_common", path.resolve())
    if spec is None or spec.loader is None:
        raise ValidationError(f"cannot import common validator: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def event_header_only(path: Path) -> bool:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    return lines == ["H\tF2A_ASSERT_EVENTS\t1\tnative"]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--common", type=Path, required=True)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--compile-report", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--raw-trace", type=Path, required=True)
    parser.add_argument("--split-dir", type=Path, required=True)
    parser.add_argument("--split-manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        common = import_common(args.common)
        campaign = common.load_json(args.campaign.resolve(), "campaign")
        compile_report = common.load_json(args.compile_report.resolve(), "compile report")
        if compile_report.get("status") != "PASS":
            raise ValidationError("compile report is not PASS")
        run_dir = args.run_dir.resolve()
        result = common.read_result(run_dir)
        if result.get("phase") != "run" or result.get("run_kind") != "golden":
            raise ValidationError("golden result phase/kind mismatch")
        if result.get("run_purpose") != "NATIVE_CHARACTERIZATION":
            raise ValidationError("golden run purpose mismatch")
        if result.get("assertion_mode") != "native":
            raise ValidationError("golden assertion mode mismatch")
        if result.get("status") != "PASS" or result.get("xrun_exit_status") != 0:
            raise ValidationError(f"strict golden result failed: {result.get('status')}")
        raw = result.get("raw_facts", {})
        if raw.get("tool", {}).get("status") != "OK":
            raise ValidationError("golden tool status is not OK")
        if raw.get("existing_detector_baseline", {}).get("triggered") is not False:
            raise ValidationError("golden run triggered a pre-existing detector")
        event_path = run_dir / "assertion_events.tsv"
        if not event_path.is_file() or not event_header_only(event_path):
            raise ValidationError("golden assertion-event file is not header-only native")
        current_hashes = common.collect_run_input_hashes(run_dir)
        compiled_hashes = compile_report["runs"]["golden"]["input_hashes"]
        if current_hashes != compiled_hashes:
            raise ValidationError("golden execution inputs differ from compiled inputs")
        retention = common.read_retention(run_dir)
        if retention.get("work_directory_retained") is not False:
            raise ValidationError("golden PASS retained work")
        common.require_absent(run_dir / "work", "golden PASS work")
        common.require_absent(args.raw_trace.resolve(), "deleted raw golden trace")
        common.validate_no_vcd(run_dir)
        manifest = common.load_json(args.split_manifest.resolve(), "split manifest")
        selected = campaign.get("selected_sites")
        if not isinstance(selected, list) or not selected:
            raise ValidationError("campaign has no selected sites")
        ids = sorted(str(item["selection_id"]) for item in selected)
        if manifest.get("shared_header_count") != 1:
            raise ValidationError("golden trace shared header count mismatch")
        if manifest.get("selection_trace_count") != len(ids):
            raise ValidationError("golden split trace count mismatch")
        row_counts: dict[str, int] = {}
        for selection_id in ids:
            path = args.split_dir.resolve() / f"{selection_id}.trace.tsv.gz"
            common.require_file(path, f"golden split trace {selection_id}")
            count = 0
            for line_number, fields in common.iter_trace_rows(path):
                if fields[0] not in {"G", "GA", "GS"} or fields[1] != selection_id:
                    raise ValidationError(
                        f"invalid split row at {path}:{line_number}: {fields}"
                    )
                count += 1
            if count == 0:
                raise ValidationError(f"empty golden split trace: {path}")
            row_counts[selection_id] = count
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    report = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "kind": "stage5_phase23_golden_validation",
        "status": "PASS",
        "run_directory": str(run_dir),
        "result": result,
        "input_hashes": current_hashes,
        "assertion_event_count": 0,
        "raw_trace_deleted_after_atomic_split": True,
        "split_selection_ids": ids,
        "split_row_counts": row_counts,
        "vcd_files_generated": 0,
    }
    output = args.report.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("Strict golden workload result     : PASS")
    print("Golden assertion event count      : 0")
    print(f"Golden split site traces          : {len(ids)}")
    print(f"Golden validation report          : {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
