#!/usr/bin/env python3
"""Validate Stage-5 Gate 3 golden functional run and split trace cache."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

SELECTION_RE = re.compile(r"^TS\d{6}$")


class Gate3Error(RuntimeError):
    pass


def load_common(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("f2a_gate_common", path.resolve())
    if spec is None or spec.loader is None:
        raise Gate3Error(f"cannot import validation common module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--common", type=Path, required=True)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--gate2-report", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--raw-trace", type=Path, required=True)
    parser.add_argument("--split-dir", type=Path, required=True)
    parser.add_argument("--split-manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        common = load_common(args.common)
        campaign = common.load_json(args.campaign.resolve(), "mini campaign")
        run_dir = args.run_dir.resolve()
        result = common.read_result(run_dir)
        if result.get("phase") != "run" or result.get("run_kind") != "golden":
            raise Gate3Error("Gate 3 result is not a golden run-phase result")
        if result.get("status") != "PASS":
            raise Gate3Error(f"golden functional result is not PASS: {result.get('status')}")
        if result.get("xrun_exit_status") != 0:
            raise Gate3Error("golden functional xrun exit status was non-zero")
        markers = result.get("markers", {})
        if markers.get("exact_signature_count", 0) < 1:
            raise Gate3Error("exact frozen CRC32 signature was not observed")
        if markers.get("exit_success_count", 0) < 1:
            raise Gate3Error("EXIT SUCCESS was not observed")
        if markers.get("runner_error_count", 0) != 0:
            raise Gate3Error("runner invariant error was recorded")
        if markers.get("infrastructure_error_count", 0) != 0:
            raise Gate3Error("Xcelium infrastructure error was recorded")

        retention = common.read_retention(run_dir)
        if retention.get("work_directory_retained") is not False:
            raise Gate3Error("successful golden work directory was retained")
        common.require_absent(run_dir / "work", "successful golden work directory")
        common.require_absent(
            run_dir / "reproduction_bundle.tar.gz",
            "golden-pass reproduction bundle",
        )
        common.validate_no_vcd(run_dir)
        gate2 = common.load_json(args.gate2_report.resolve(), "Gate-2 report")
        if gate2.get("status") != "PASS":
            raise Gate3Error("Gate-2 report is not PASS")
        current_input_hashes = common.collect_run_input_hashes(run_dir)
        if current_input_hashes != gate2.get("golden_input_hashes"):
            raise Gate3Error(
                "Gate-3 golden execution inputs differ from Gate-2 compiled inputs"
            )

        # The driver splits atomically with --delete-source.  The raw trace must
        # therefore be gone only after the split manifest/cache are valid.
        common.require_absent(args.raw_trace.resolve(), "deleted raw golden trace")
        split_manifest = common.load_json(
            args.split_manifest.resolve(), "golden split manifest"
        )
        if split_manifest.get("shared_header_count") != 1:
            raise Gate3Error("golden raw trace did not contain exactly one shared header")

        selected_sites = campaign.get("selected_sites")
        if not isinstance(selected_sites, list) or not selected_sites:
            raise Gate3Error("mini campaign has no selected sites")
        expected_ids = sorted(str(item["selection_id"]) for item in selected_sites)
        if any(not SELECTION_RE.fullmatch(item) for item in expected_ids):
            raise Gate3Error("mini campaign contains invalid selection IDs")
        if split_manifest.get("selection_trace_count") != len(expected_ids):
            raise Gate3Error(
                "split trace count mismatch: "
                f"expected={len(expected_ids)}, "
                f"actual={split_manifest.get('selection_trace_count')}"
            )

        actual_ids: list[str] = []
        row_counts: dict[str, int] = {}
        for selection_id in expected_ids:
            path = args.split_dir.resolve() / f"{selection_id}.trace.tsv.gz"
            common.require_file(path, f"split trace {selection_id}")
            count = 0
            for line_number, fields in common.iter_trace_rows(path):
                if fields[0] not in {"G", "GA", "GS"}:
                    raise Gate3Error(
                        f"invalid golden split row type at {path}:{line_number}: {fields[0]}"
                    )
                if len(fields) < 2 or fields[1] != selection_id:
                    raise Gate3Error(
                        f"selection ID mismatch at {path}:{line_number}: {fields}"
                    )
                count += 1
            if count == 0:
                raise Gate3Error(f"split trace contains no records: {path}")
            actual_ids.append(selection_id)
            row_counts[selection_id] = count
        if actual_ids != expected_ids:
            raise Gate3Error("split trace ID set mismatch")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    report = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "gate": "stage5_gate3_golden_functional",
        "status": "PASS",
        "run_directory": str(args.run_dir.resolve()),
        "result": result,
        "input_hashes": current_input_hashes,
        "compiled_input_hashes_match": True,
        "raw_trace_deleted_after_atomic_split": True,
        "split_manifest": str(args.split_manifest.resolve()),
        "split_selection_ids": expected_ids,
        "split_row_counts": row_counts,
        "vcd_files_generated": 0,
    }
    output = args.report.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("Gate 3 strict golden verdict       : PASS")
    print(f"Gate 3 split site traces           : {len(expected_ids)}")
    print(f"Gate 3 report                      : {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
