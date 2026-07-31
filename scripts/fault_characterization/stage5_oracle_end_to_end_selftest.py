#!/usr/bin/env python3
"""Synthetic end-to-end tests for Stage-5 v2 trace parsing and oracle output."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def make_spec(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "program_version": "1.0.7",
        "generated_at_utc": "2026-01-01T00:00:00+00:00",
        "stage": "stage_05_fault_materialization",
        "fault_id": "TF000001_SA0",
        "selection_id": "TS000001",
        "selection_rank": 1,
        "site_id": "RS000001",
        "site_key": "m|n",
        "design": "cv32e40p",
        "workload": "crc32",
        "fault_class": "control_path",
        "injection_kind": "net_stuck_at",
        "polarity": "SA0",
        "stuck_at": 0,
        "source_stage4": {},
        "mapped_netlist": {},
        "site": {
            "module": "m",
            "source_net": "src",
            "source_key": "src",
            "source_kind": "combinational_output",
            "state_site": False,
        },
        "receiver_signals": [
            {
                "receiver_index": 0,
                "receiver_id": "R000",
                "expression": "recv",
                "source_key": "recv",
                "role": "direct_receiver_output",
                "metadata": {},
            }
        ],
        "modification": {},
        "artifacts": {},
    }
    payload["fault_spec_digest_sha256"] = canonical_digest(
        {key: value for key, value in payload.items() if key != "generated_at_utc"}
    )
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def write_result(path: Path, status: str) -> None:
    exact = status == "OUTPUT_MATCH"
    payload = {
        "schema_version": "1.0",
        "verdict_engine_version": "2.0.0",
        "generated_at_utc": "2026-01-01T00:00:00+00:00",
        "phase": "run",
        "run_kind": "fault",
        "xrun_exit_status": 0 if status == "OUTPUT_MATCH" else 2,
        "status": status,
        "reason": "synthetic",
        "recommended_exit_code": 0 if status == "OUTPUT_MATCH" else 2,
        "markers": {
            "log_exists": True,
            "exact_signature_count": 1 if exact else 0,
            "any_crc_pass_count": 1 if exact else 0,
            "exit_success_count": 1 if exact else 0,
            "timeout_count": 1 if status == "TIMEOUT" else 0,
            "output_failure_count": 1 if status == "OUTPUT_MISMATCH" else 0,
            "runner_error_count": 0,
            "infrastructure_error_count": 0,
        },
        "strict_success_requirements": {},
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def run_case(
    *,
    root: Path,
    analyzer: Path,
    validator: Path,
    semantics: Path,
    policy: Path,
    label: str,
    fault_trace_text: str,
    status: str,
    expected_class: str,
) -> None:
    case = root / label.replace(" ", "_")
    case.mkdir()
    spec_path = case / "fault.json"
    make_spec(spec_path)
    golden = case / "TS000001.trace.tsv.gz"
    import gzip

    with gzip.open(golden, "wt", encoding="utf-8") as handle:
        handle.write("G\tTS000001\t1\t10\ttop.u.gmon\t0\t0\n")
        handle.write("GA\tTS000001\t1\t10\ttop.u.gmon\tSRC\t0\n")
        handle.write("G\tTS000001\t2\t20\ttop.u.gmon\t1\t0\n")
        handle.write("GA\tTS000001\t2\t20\ttop.u.gmon\tSRC\t1\n")
    fault_trace = case / "fault.trace.tsv"
    fault_trace.write_text(fault_trace_text, encoding="utf-8")
    result = case / "result.json"
    write_result(result, status)
    log = case / "xrun.log"
    log.write_text("synthetic\n", encoding="utf-8")
    oracle = case / "oracle.json"
    report = case / "oracle.txt"
    sva = case / "oracle.sva"
    validation = case / "validation.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(analyzer),
            "--fault-json",
            str(spec_path),
            "--golden-trace",
            str(golden),
            "--fault-trace",
            str(fault_trace),
            "--result-json",
            str(result),
            "--xrun-log",
            str(log),
            "--semantics",
            str(semantics),
            "--policy",
            str(policy),
            "--oracle-output",
            str(oracle),
            "--report-output",
            str(report),
            "--sva-output",
            str(sva),
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"analyzer failed for {label}:\n{completed.stdout}\n{completed.stderr}"
        )
    payload = json.loads(oracle.read_text(encoding="utf-8"))
    actual = payload["semantic_classification"]["primary_class"]
    if actual != expected_class:
        raise RuntimeError(
            f"{label}: expected={expected_class}, actual={actual}"
        )
    validated = subprocess.run(
        [
            sys.executable,
            str(validator),
            "--oracle",
            str(oracle),
            "--fault-json",
            str(spec_path),
            "--analyzer",
            str(analyzer),
            "--semantics",
            str(semantics),
            "--policy",
            str(policy),
            "--report",
            str(validation),
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if validated.returncode != 0:
        raise RuntimeError(
            f"validator failed for {label}:\n{validated.stdout}\n{validated.stderr}"
        )
    if label == "receiver divergence":
        original_text = oracle.read_text(encoding="utf-8")
        tampered = json.loads(original_text)
        tampered["raw_facts"]["activation"]["activated"] = False
        oracle.write_text(json.dumps(tampered, indent=2) + "\n", encoding="utf-8")
        rejected = subprocess.run(
            [
                sys.executable,
                str(validator),
                "--oracle", str(oracle),
                "--fault-json", str(spec_path),
                "--analyzer", str(analyzer),
                "--semantics", str(semantics),
                "--policy", str(policy),
                "--report", str(validation),
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        oracle.write_text(original_text, encoding="utf-8")
        if rejected.returncode == 0:
            raise RuntimeError("validator accepted tampered raw facts")
        print("End-to-end tampered raw facts rejection: PASS")
    print(f"End-to-end {label:30s}: PASS ({actual})")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analyzer", type=Path, required=True)
    parser.add_argument("--validator", type=Path, required=True)
    parser.add_argument("--semantics", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        with tempfile.TemporaryDirectory(prefix="f2a_oracle_e2e_") as temporary:
            root = Path(temporary)
            run_case(
                root=root,
                analyzer=args.analyzer.resolve(),
                validator=args.validator.resolve(),
                semantics=args.semantics.resolve(),
                policy=args.policy.resolve(),
                label="receiver divergence",
                fault_trace_text=(
                    "H\tFAULT\tTF000001_SA0\n"
                    "F\tTF000001_SA0\t1\t10\ttop.u.fmon\t0\t0\t0\n"
                    "FA\tTF000001_SA0\t1\t10\ttop.u.fmon\tPRE\t0\n"
                    "FA\tTF000001_SA0\t1\t10\ttop.u.fmon\tOBS\t0\n"
                    "F\tTF000001_SA0\t2\t20\ttop.u.fmon\t1\t0\t1\n"
                    "FA\tTF000001_SA0\t2\t20\ttop.u.fmon\tPRE\t1\n"
                ),
                status="OUTPUT_MATCH",
                expected_class="LOCAL_PROPAGATION_MASKED_AT_OUTPUT",
            )
            run_case(
                root=root,
                analyzer=args.analyzer.resolve(),
                validator=args.validator.resolve(),
                semantics=args.semantics.resolve(),
                policy=args.policy.resolve(),
                label="injection error",
                fault_trace_text=(
                    "H\tFAULT\tTF000001_SA0\n"
                    "F\tTF000001_SA0\t1\t10\ttop.u.fmon\t0\t1\t0\n"
                    "FA\tTF000001_SA0\t1\t10\ttop.u.fmon\tPRE\t0\n"
                    "FA\tTF000001_SA0\t1\t10\ttop.u.fmon\tOBS\t1\n"
                    "F\tTF000001_SA0\t2\t20\ttop.u.fmon\t1\t1\t0\n"
                ),
                status="OUTPUT_MATCH",
                expected_class="INJECTION_ERROR",
            )
            run_case(
                root=root,
                analyzer=args.analyzer.resolve(),
                validator=args.validator.resolve(),
                semantics=args.semantics.resolve(),
                policy=args.policy.resolve(),
                label="detected output corruption",
                fault_trace_text=(
                    "H\tFAULT\tTF000001_SA0\n"
                    "F\tTF000001_SA0\t1\t10\ttop.u.fmon\t0\t0\t0\n"
                    "FA\tTF000001_SA0\t1\t10\ttop.u.fmon\tOBS\t0\n"
                    "F\tTF000001_SA0\t2\t20\ttop.u.fmon\t1\t0\t1\n"
                ),
                status="OUTPUT_MISMATCH",
                expected_class="DETECTED_OUTPUT_CORRUPTION",
            )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("Stage-5 oracle end-to-end self-test     : PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
