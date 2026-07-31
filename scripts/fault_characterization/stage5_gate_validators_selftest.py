#!/usr/bin/env python3
"""Synthetic positive/negative tests for Stage-5 Gate 2/3/4 validators."""

from __future__ import annotations

import argparse
import gzip
import json
import subprocess
import sys
import tarfile
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Any, Sequence


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def result_payload(phase: str, run_kind: str, status: str) -> dict[str, Any]:
    exact = status in {"PASS", "OUTPUT_MATCH"}
    compile_phase = phase == "compile"
    if compile_phase:
        completion = "COMPILE_ONLY"
        workload_outcome = "NOT_RUN"
        architectural_outcome = "NOT_RUN"
        tool_status = "OK" if status == "COMPILE_PASS" else "ERROR"
        valid = status == "COMPILE_PASS"
    elif status in {"PASS", "OUTPUT_MATCH"}:
        completion = "COMPLETED"
        workload_outcome = "PASS"
        architectural_outcome = "OBSERVED_PASS"
        tool_status = "OK"
        valid = True
    elif status == "OUTPUT_MISMATCH":
        completion = "COMPLETED"
        workload_outcome = "FAIL"
        architectural_outcome = "OBSERVED_FAIL"
        tool_status = "OK"
        valid = True
    elif status == "TIMEOUT":
        completion = "TIMED_OUT"
        workload_outcome = "NOT_REACHED"
        architectural_outcome = "CENSORED"
        tool_status = "OK"
        valid = True
    elif status == "EXISTING_ASSERTION_DETECTED":
        completion = "TERMINATED_BY_EXISTING_ASSERTION"
        workload_outcome = "NOT_REACHED"
        architectural_outcome = "CENSORED"
        tool_status = "OK"
        valid = True
    else:
        completion = "UNKNOWN"
        workload_outcome = "UNKNOWN"
        architectural_outcome = "UNKNOWN"
        tool_status = "ERROR" if status == "ERROR" else "OK"
        valid = False

    detector_events = []
    if status == "EXISTING_ASSERTION_DETECTED":
        detector_events = [
            {
                "event_index": 0,
                "log_line": 1,
                "tool": "xmsim",
                "severity": "F",
                "mnemonic": "ASRTST",
                "detector_origin": "PREEXISTING_TB_ASSERTION",
                "assertion_name": "tb_top.wrapper_i.ram_i.out_of_bounds_write",
                "assertion_leaf_name": "out_of_bounds_write",
                "source_file": "/tmp/verification/shared/tb/mm_ram.sv",
                "source_line": 367,
                "simulation_time": {"value": "780", "unit": "NS"},
                "action": "FATAL_TERMINATION",
                "termination_log_line": 2,
                "fatal_exit_code": "1",
                "fatal_source_statement": "synthetic",
                "raw_message": "synthetic ASRTST",
            }
        ]

    xrun_status = 0 if status in {"COMPILE_PASS", "PASS", "OUTPUT_MATCH"} else 2
    recommended = 0 if status in {"COMPILE_PASS", "PASS", "OUTPUT_MATCH"} else 2
    if status in {"COMPILE_ERROR", "ERROR", "UNKNOWN"}:
        recommended = 4 if status != "UNKNOWN" else 3
    return {
        "schema_version": "2.0",
        "verdict_engine_version": "3.0.0",
        "policy_version": "native_execution_raw_facts_v1",
        "generated_at_utc": "2026-01-01T00:00:00+00:00",
        "phase": phase,
        "run_kind": run_kind,
        "run_purpose": "COMPILE_CHECK" if compile_phase else "NATIVE_CHARACTERIZATION",
        "xrun_exit_status": xrun_status,
        "status": status,
        "reason": "synthetic",
        "recommended_exit_code": recommended,
        "raw_facts": {
            "tool": {
                "status": tool_status,
                "xrun_exit_status": xrun_status,
                "runner_error_count": 0,
                "infrastructure_error_count": 0,
                "infrastructure_events": [],
            },
            "execution": {
                "run_purpose": "COMPILE_CHECK" if compile_phase else "NATIVE_CHARACTERIZATION",
                "valid_experiment_execution": valid,
                "completion": completion,
                "terminal_event_type": (
                    "PREEXISTING_ASSERTION"
                    if status == "EXISTING_ASSERTION_DETECTED"
                    else None
                ),
                "terminal_log_line": 1 if status == "EXISTING_ASSERTION_DETECTED" else None,
                "native_execution": not compile_phase,
                "post_terminal_execution_observed": False,
            },
            "workload": {
                "outcome": workload_outcome,
                "architectural_outcome": architectural_outcome,
                "exact_signature_observed": exact,
                "any_crc_pass_observed": exact,
                "exit_success_observed": exact,
                "explicit_failure_observed": status == "OUTPUT_MISMATCH",
                "timeout_observed": status == "TIMEOUT",
            },
            "existing_detector_baseline": {
                "triggered": bool(detector_events),
                "event_count": len(detector_events),
                "events": detector_events,
            },
        },
        "markers": {
            "log_exists": True,
            "exact_signature_count": 1 if exact else 0,
            "any_crc_pass_count": 1 if exact else 0,
            "exit_success_count": 1 if exact else 0,
            "timeout_count": 1 if status == "TIMEOUT" else 0,
            "output_failure_count": 1 if status == "OUTPUT_MISMATCH" else 0,
            "runner_error_count": 0,
            "existing_assertion_event_count": len(detector_events),
            "infrastructure_error_count": 0,
        },
        "interpretation_contract": {},
        "strict_success_requirements": {},
    }


def make_run(root: Path, phase: str, run_kind: str, status: str) -> Path:
    root.mkdir(parents=True)
    write_json(root / "result.json", result_payload(phase, run_kind, status))
    (root / "result.txt").write_text(status + "\n", encoding="utf-8")
    retained = status in {"COMPILE_ERROR", "ERROR", "UNKNOWN", "TIMEOUT", "EXISTING_ASSERTION_DETECTED"}
    write_json(
        root / "retention.json",
        {
            "schema_version": "1.0",
            "status": status,
            "work_directory_retained": retained,
            "retention_reason": "synthetic",
            "reproduction_bundle_created": False,
        },
    )
    (root / "command.txt").write_text(
        "xrun -elaborate\n" if phase == "compile" else "xrun\n",
        encoding="utf-8",
    )
    if phase == "compile":
        log_text = "compile clean\n"
    elif status == "EXISTING_ASSERTION_DETECTED":
        log_text = (
            "xmsim: *F,ASRTST: (/tmp/verification/shared/tb/mm_ram.sv,367): "
            "(time 780 NS) Assertion "
            "tb_top.wrapper_i.ram_i.out_of_bounds_write has failed\n"
            "Simulation terminated via $fatal(1) at time 780 NS + 11\n"
        )
    else:
        log_text = (
            "CRC32 PASS: vector=cbf43926 signature=2d6352b3 last=5650ac83\n"
            "EXIT SUCCESS\n"
        )
    (root / "xrun.log").write_text(log_text, encoding="utf-8")
    raw_hash = "b" * 64 if run_kind == "golden" else "c" * 64
    monitor_hash = "d" * 64 if run_kind == "golden" else "e" * 64
    (root / "firmware.sha256").write_text(
        f"{'a' * 64}  /tmp/crc32.hex\n{'f' * 64}  /tmp/crc32.elf\n",
        encoding="utf-8",
    )
    (root / "netlist_sources.sha256").write_text(
        f"{raw_hash}  /tmp/raw.v\n{'1' * 64}  /tmp/cell.v\n",
        encoding="utf-8",
    )
    (root / "simulation_netlist.sha256").write_text(
        f"{'2' * 64}  /tmp/prepared.v\n", encoding="utf-8"
    )
    (root / "stage5_monitor.sha256").write_text(
        f"{monitor_hash}  /tmp/monitor.sv\n", encoding="utf-8"
    )
    if retained:
        (root / "work").mkdir()
    return root


def run_tool(command: list[str], expected_success: bool, label: str) -> None:
    completed = subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    actual_success = completed.returncode == 0
    if actual_success != expected_success:
        raise RuntimeError(
            f"{label}: expected_success={expected_success}, rc={completed.returncode}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    print(f"Gate validator {label:28s}: PASS")


def make_minimal_bundle(run_dir: Path, status: str) -> None:
    manifest = {
        "schema_version": "1.0",
        "kind": "stage5_reproduction_bundle",
        "status": status,
    }
    write_json(run_dir / "reproduction_bundle_manifest.json", manifest)
    with tarfile.open(run_dir / "reproduction_bundle.tar.gz", "w:gz") as archive:
        for name, data in {
            "README_REPRODUCE.txt": b"synthetic\n",
            "reproduction_bundle_manifest.json": json.dumps(manifest).encode(),
            "run/xrun.log": (run_dir / "xrun.log").read_bytes(),
            "run/result.json": (run_dir / "result.json").read_bytes(),
            "run/command.txt": (run_dir / "command.txt").read_bytes(),
            "run/stage5_monitor.sv": b"module m; endmodule\n",
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, BytesIO(data))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--common", type=Path, required=True)
    parser.add_argument("--gate2", type=Path, required=True)
    parser.add_argument("--gate3", type=Path, required=True)
    parser.add_argument("--gate4", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        with tempfile.TemporaryDirectory(prefix="f2a_gate_validators_") as temporary:
            root = Path(temporary)

            # Gate 2 positive and one negative contract test.
            g2_golden = make_run(root / "g2_golden", "compile", "golden", "COMPILE_PASS")
            g2_fault = make_run(root / "g2_fault", "compile", "fault", "COMPILE_PASS")
            g2_report = root / "g2_report.json"
            gate2_cmd = [
                sys.executable,
                str(args.gate2.resolve()),
                "--common", str(args.common.resolve()),
                "--golden-run", str(g2_golden),
                "--fault-run", str(g2_fault),
                "--golden-trace", str(root / "no_golden_trace.tsv"),
                "--fault-trace", str(root / "no_fault_trace.tsv"),
                "--report", str(g2_report),
            ]
            run_tool(gate2_cmd, True, "Gate2 positive")
            (g2_fault / "command.txt").write_text("xrun -compile\n", encoding="utf-8")
            run_tool(gate2_cmd, False, "Gate2 rejects no elaborate")

            # Gate 3 positive and strict-result negative test.
            g3_run = make_run(root / "g3_run", "run", "golden", "PASS")
            campaign = root / "campaign.json"
            selection_ids = [f"TS{index:06d}" for index in range(1, 5)]
            write_json(campaign, {"selected_sites": [{"selection_id": item} for item in selection_ids]})
            split_dir = root / "split"
            split_dir.mkdir()
            row_counts = {}
            for selection_id in selection_ids:
                path = split_dir / f"{selection_id}.trace.tsv.gz"
                with gzip.open(path, "wt", encoding="utf-8") as handle:
                    handle.write(f"G\t{selection_id}\t1\t10\ttop.u.gmon\t0\t0\n")
                row_counts[selection_id] = 1
            split_manifest = root / "split_manifest.json"
            write_json(
                split_manifest,
                {
                    "shared_header_count": 1,
                    "selection_trace_count": 4,
                    "row_counts": row_counts,
                },
            )
            g3_report = root / "g3_report.json"
            gate3_cmd = [
                sys.executable,
                str(args.gate3.resolve()),
                "--common", str(args.common.resolve()),
                "--campaign", str(campaign),
                "--gate2-report", str(g2_report),
                "--run-dir", str(g3_run),
                "--raw-trace", str(root / "deleted_raw.tsv"),
                "--split-dir", str(split_dir),
                "--split-manifest", str(split_manifest),
                "--report", str(g3_report),
            ]
            run_tool(gate3_cmd, True, "Gate3 positive")
            original_monitor_sha = (g3_run / "stage5_monitor.sha256").read_text(encoding="utf-8")
            (g3_run / "stage5_monitor.sha256").write_text(
                f"{'9' * 64}  /tmp/monitor.sv\n", encoding="utf-8"
            )
            run_tool(gate3_cmd, False, "Gate3 rejects input drift")
            (g3_run / "stage5_monitor.sha256").write_text(
                original_monitor_sha, encoding="utf-8"
            )
            bad_result = result_payload("run", "golden", "UNKNOWN")
            write_json(g3_run / "result.json", bad_result)
            (g3_run / "result.txt").write_text("UNKNOWN\n", encoding="utf-8")
            run_tool(gate3_cmd, False, "Gate3 rejects UNKNOWN")

            # Gate 4 OUTPUT_MATCH positive and ERROR negative test.
            g4_run = make_run(root / "g4_run", "run", "fault", "OUTPUT_MATCH")
            fault_digest = "a" * 64
            write_json(
                g4_run / "fault.json",
                {
                    "fault_id": "TF000001_SA0",
                    "fault_spec_digest_sha256": fault_digest,
                },
            )
            selection = root / "selection.json"
            write_json(
                selection,
                {
                    "fault_id": "TF000001_SA0",
                    "fault_spec_digest_sha256": fault_digest,
                },
            )
            fault_trace = root / "fault.trace.tsv"
            fault_trace.write_text(
                "H\tFAULT\tTF000001_SA0\n"
                "F\tTF000001_SA0\t1\t10\ttop.u.fmon\t0\t0\t0\n",
                encoding="utf-8",
            )
            g4_report = root / "g4_report.json"
            gate4_cmd = [
                sys.executable,
                str(args.gate4.resolve()),
                "--common", str(args.common.resolve()),
                "--selection-record", str(selection),
                "--gate2-report", str(g2_report),
                "--run-dir", str(g4_run),
                "--trace", str(fault_trace),
                "--report", str(g4_report),
            ]
            run_tool(gate4_cmd, True, "Gate4 OUTPUT_MATCH")
            original_netlist_sha = (g4_run / "netlist_sources.sha256").read_text(encoding="utf-8")
            (g4_run / "netlist_sources.sha256").write_text(
                f"{'8' * 64}  /tmp/raw.v\n{'1' * 64}  /tmp/cell.v\n",
                encoding="utf-8",
            )
            run_tool(gate4_cmd, False, "Gate4 rejects input drift")
            (g4_run / "netlist_sources.sha256").write_text(
                original_netlist_sha, encoding="utf-8"
            )
            # Existing assertion is valid native detector evidence, not a tool error.
            write_json(
                g4_run / "result.json",
                result_payload("run", "fault", "EXISTING_ASSERTION_DETECTED"),
            )
            (g4_run / "result.txt").write_text(
                "EXISTING_ASSERTION_DETECTED\n", encoding="utf-8"
            )
            write_json(
                g4_run / "retention.json",
                {
                    "schema_version": "1.0",
                    "status": "EXISTING_ASSERTION_DETECTED",
                    "work_directory_retained": True,
                    "retention_reason": "synthetic",
                    "reproduction_bundle_created": True,
                },
            )
            (g4_run / "work").mkdir(exist_ok=True)
            make_minimal_bundle(g4_run, "EXISTING_ASSERTION_DETECTED")
            run_tool(gate4_cmd, True, "Gate4 existing assertion")

            write_json(g4_run / "result.json", result_payload("run", "fault", "ERROR"))
            (g4_run / "result.txt").write_text("ERROR\n", encoding="utf-8")
            make_minimal_bundle(g4_run, "ERROR")
            run_tool(gate4_cmd, False, "Gate4 rejects ERROR")

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("Stage-5 gate validator self-test       : PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
