#!/usr/bin/env python3
"""Audit Stage-5 Phase2-G1 compile runs.

This audit intentionally does not require command.txt to contain a fixed
absolute path to mm_ram.sv. Source paths may be represented differently by
the runner.

Instead, it proves that Phase2-G1 did not request or use the diagnostic
mm_ram overlay and did not execute OBSERVE or QUARANTINE behavior.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


class AuditError(RuntimeError):
    """Controlled Phase2-G1 audit failure."""


def require_file(path: Path, label: str) -> Path:
    path = path.expanduser().resolve()

    if not path.is_file() or path.stat().st_size == 0:
        raise AuditError(f"{label} not found or empty: {path}")

    return path


def load_json(path: Path, label: str) -> dict[str, Any]:
    path = require_file(path, label)

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AuditError(f"invalid {label}: {path}: {exc}") from exc

    if not isinstance(value, dict):
        raise AuditError(f"{label} must be a JSON object: {path}")

    return value


def read_optional(path: Path) -> str:
    if not path.is_file():
        return ""

    return path.read_text(
        encoding="utf-8",
        errors="replace",
    )


def audit_compile_run(run_dir: Path, expected_kind: str) -> dict[str, Any]:
    run_dir = run_dir.expanduser().resolve()

    if not run_dir.is_dir():
        raise AuditError(f"run directory not found: {run_dir}")

    result = load_json(run_dir / "result.json", f"{expected_kind} result")

    if result.get("phase") != "compile":
        raise AuditError(f"{expected_kind}: phase is not compile")

    if result.get("run_kind") != expected_kind:
        raise AuditError(
            f"{expected_kind}: run_kind mismatch: "
            f"{result.get('run_kind')!r}"
        )

    if result.get("run_purpose") != "COMPILE_CHECK":
        raise AuditError(
            f"{expected_kind}: run_purpose is not COMPILE_CHECK"
        )

    if result.get("status") != "COMPILE_PASS":
        raise AuditError(
            f"{expected_kind}: result is not COMPILE_PASS: "
            f"{result.get('status')!r}"
        )

    if result.get("xrun_exit_status") != 0:
        raise AuditError(
            f"{expected_kind}: xrun_exit_status is not zero"
        )

    evidence_files = [
        run_dir / "command.txt",
        run_dir / "wrapper_command.txt",
        run_dir / "manifest.txt",
        run_dir / "manifest.json",
    ]

    evidence_text = "\n".join(
        read_optional(path)
        for path in evidence_files
    )

    forbidden = {
        "mm_ram diagnostic overlay":
            "mm_ram.stage5.sv",

        "overlay enable variable":
            "STAGE5_USE_ASSERTION_OVERLAY=1",

        "observe runtime plusarg":
            "+f2a_assert_mode=observe",

        "quarantine runtime plusarg":
            "+f2a_assert_mode=diagnostic_quarantine",

        "observe runtime environment":
            "STAGE5_ASSERTION_MODE=observe",

        "quarantine runtime environment":
            "STAGE5_ASSERTION_MODE=diagnostic_quarantine",
    }

    violations: list[str] = []

    for label, marker in forbidden.items():
        if marker in evidence_text:
            violations.append(f"{label}: {marker}")

    if violations:
        raise AuditError(
            f"{expected_kind}: forbidden Phase2-G1 behavior detected: "
            + "; ".join(violations)
        )

    command = require_file(
        run_dir / "command.txt",
        f"{expected_kind} command",
    ).read_text(
        encoding="utf-8",
        errors="replace",
    )

    if "-elaborate" not in command:
        raise AuditError(
            f"{expected_kind}: command did not use -elaborate"
        )

    xrun_log = require_file(
        run_dir / "xrun.log",
        f"{expected_kind} xrun log",
    ).read_text(
        encoding="utf-8",
        errors="replace",
    )

    forbidden_errors = (
        "MULAXX",
        "ELBERR",
        "Multiple drivers to always_ff",
        "xmvlog: *E",
        "xmelab: *E",
        "xmsim: *E",
        "xrun: *E",
    )

    found_errors = [
        marker
        for marker in forbidden_errors
        if marker in xrun_log
    ]

    if found_errors:
        raise AuditError(
            f"{expected_kind}: elaboration errors found: "
            + ", ".join(found_errors)
        )

    if "EXIT SUCCESS" in xrun_log:
        raise AuditError(
            f"{expected_kind}: compile-only run entered simulation"
        )

    return {
        "run_kind": expected_kind,
        "run_directory": str(run_dir),
        "status": result["status"],
        "xrun_exit_status": result["xrun_exit_status"],
        "compile_only": True,
        "used_elaborate": True,
        "diagnostic_overlay_requested": False,
        "observe_runtime_executed": False,
        "quarantine_runtime_executed": False,
        "elaboration_error_count": 0,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--golden-run",
        type=Path,
        required=True,
    )

    parser.add_argument(
        "--fault-run",
        type=Path,
        required=True,
    )

    parser.add_argument(
        "--report",
        type=Path,
        required=True,
    )

    args = parser.parse_args(argv)

    try:
        golden = audit_compile_run(
            args.golden_run,
            "golden",
        )

        fault = audit_compile_run(
            args.fault_run,
            "fault",
        )

        report = {
            "schema_version": "1.0",
            "generated_at_utc": datetime.now(
                timezone.utc
            ).isoformat(),
            "gate": "stage5_phase2_g1_run_audit",
            "status": "PASS",
            "golden": golden,
            "fault": fault,
            "claims": {
                "golden_compile_elaboration_passed": True,
                "fault_compile_elaboration_passed": True,
                "fixed_mm_ram_path_not_required": True,
                "diagnostic_overlay_requested": False,
                "observe_runtime_executed": False,
                "quarantine_runtime_executed": False,
                "simulation_entered": False,
                "elaboration_errors": 0,
            },
        }

        output = args.report.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)

        output.write_text(
            json.dumps(report, indent=2) + "\n",
            encoding="utf-8",
        )

    except AuditError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("Phase2-G1 run audit: PASS")
    print(f"Audit report: {output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
