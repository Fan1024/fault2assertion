#!/usr/bin/env python3
"""Strict, testable verdict engine for Fault2Assertion Stage-5 Xcelium runs.

The runner delegates all PASS/FAIL interpretation to this file.  A functional
PASS is accepted only when both the exact CRC32 signature and EXIT SUCCESS are
present and Xcelium returned zero.  Merely seeing EXIT SUCCESS is never enough.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

PROGRAM_VERSION = "2.0.0"

EXACT_SIGNATURE_RE = re.compile(
    r"CRC32\s+PASS:\s*"
    r"vector=(?:0x)?cbf43926\s+"
    r"signature=(?:0x)?2d6352b3\s+"
    r"last=(?:0x)?5650ac83\b",
    re.IGNORECASE,
)
ANY_CRC_PASS_RE = re.compile(r"CRC32\s+PASS:", re.IGNORECASE)
EXIT_SUCCESS_RE = re.compile(r"\bEXIT\s+SUCCESS\b", re.IGNORECASE)
TIMEOUT_RE = re.compile(
    r"Simulation aborted due to maximum cycle limit|"
    r"maximum cycle limit|\bMAXCYCLES\b.*(?:reached|exceeded)",
    re.IGNORECASE,
)
OUTPUT_FAILURE_RE = re.compile(
    r"CRC32\s+FAIL|\bEXIT\s+FAILURE\b|TEST\(S\)\s+FAILED",
    re.IGNORECASE,
)
RUNNER_ERROR_RE = re.compile(r"^F2A_RUNNER_ERROR:", re.IGNORECASE | re.MULTILINE)
INFRA_ERROR_RE = re.compile(
    r"^(?:xmvlog|xmelab|xmsim|xrun):\s*\*[EF],|"
    r"\b(?:segmentation fault|core dumped|internal error)\b|"
    r"^FATAL:\s",
    re.IGNORECASE | re.MULTILINE,
)

SUCCESS_STATUSES = {"COMPILE_PASS", "PASS", "OUTPUT_MATCH"}
VALID_SCIENTIFIC_FAULT_STATUSES = {"OUTPUT_MATCH", "OUTPUT_MISMATCH", "TIMEOUT"}


class VerdictError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Markers:
    log_exists: bool
    exact_signature_count: int
    any_crc_pass_count: int
    exit_success_count: int
    timeout_count: int
    output_failure_count: int
    runner_error_count: int
    infrastructure_error_count: int


@dataclass(frozen=True)
class Verdict:
    schema_version: str
    verdict_engine_version: str
    generated_at_utc: str
    phase: str
    run_kind: str
    xrun_exit_status: int
    status: str
    reason: str
    recommended_exit_code: int
    markers: Markers
    strict_success_requirements: dict[str, object]


def count_matches(pattern: re.Pattern[str], text: str) -> int:
    return sum(1 for _ in pattern.finditer(text))


def inspect_markers(log_text: str | None) -> Markers:
    if log_text is None:
        return Markers(False, 0, 0, 0, 0, 0, 0, 0)
    return Markers(
        log_exists=True,
        exact_signature_count=count_matches(EXACT_SIGNATURE_RE, log_text),
        any_crc_pass_count=count_matches(ANY_CRC_PASS_RE, log_text),
        exit_success_count=count_matches(EXIT_SUCCESS_RE, log_text),
        timeout_count=count_matches(TIMEOUT_RE, log_text),
        output_failure_count=count_matches(OUTPUT_FAILURE_RE, log_text),
        runner_error_count=count_matches(RUNNER_ERROR_RE, log_text),
        infrastructure_error_count=count_matches(INFRA_ERROR_RE, log_text),
    )


def compute_verdict(
    *,
    phase: str,
    run_kind: str,
    xrun_exit_status: int,
    log_text: str | None,
) -> Verdict:
    if phase not in {"compile", "run"}:
        raise VerdictError(f"phase must be compile or run; got {phase!r}")
    if run_kind not in {"golden", "fault"}:
        raise VerdictError(f"run_kind must be golden or fault; got {run_kind!r}")
    if xrun_exit_status < 0:
        raise VerdictError("xrun exit status must be non-negative")

    markers = inspect_markers(log_text)
    status: str
    reason: str
    exit_code: int

    if phase == "compile":
        if not markers.log_exists:
            status, reason, exit_code = (
                "COMPILE_ERROR",
                "xrun_log_missing",
                4,
            )
        elif markers.runner_error_count:
            status, reason, exit_code = (
                "COMPILE_ERROR",
                "stage5_runner_invariant_error",
                4,
            )
        elif xrun_exit_status != 0:
            status, reason, exit_code = (
                "COMPILE_ERROR",
                "xrun_nonzero_during_compile_or_elaboration",
                4,
            )
        elif markers.infrastructure_error_count:
            status, reason, exit_code = (
                "COMPILE_ERROR",
                "xcelium_compile_or_elaboration_error_marker",
                4,
            )
        else:
            status, reason, exit_code = (
                "COMPILE_PASS",
                "compile_and_elaboration_completed_without_error",
                0,
            )
    else:
        # Explicit workload outcomes dominate a non-zero simulator return because
        # a testbench may intentionally terminate after reporting timeout/failure.
        if not markers.log_exists:
            status, reason, exit_code = "ERROR", "xrun_log_missing", 4
        elif markers.runner_error_count:
            status, reason, exit_code = "ERROR", "stage5_runner_invariant_error", 4
        elif markers.timeout_count:
            status, reason, exit_code = "TIMEOUT", "maximum_cycle_limit_reached", 2
        elif markers.output_failure_count:
            status, reason, exit_code = (
                "OUTPUT_MISMATCH",
                "explicit_workload_failure_marker",
                2,
            )
        elif markers.infrastructure_error_count:
            status, reason, exit_code = (
                "ERROR",
                "xcelium_infrastructure_error_marker",
                4,
            )
        elif (
            xrun_exit_status == 0
            and markers.exact_signature_count >= 1
            and markers.exit_success_count >= 1
        ):
            status = "PASS" if run_kind == "golden" else "OUTPUT_MATCH"
            reason = "exact_crc32_signature_and_exit_success"
            exit_code = 0
        elif markers.any_crc_pass_count >= 1 and markers.exact_signature_count == 0:
            status, reason, exit_code = (
                "OUTPUT_MISMATCH",
                "crc32_pass_marker_did_not_match_frozen_signature",
                2,
            )
        elif xrun_exit_status != 0:
            status, reason, exit_code = "ERROR", "xrun_nonzero_without_valid_outcome", 4
        elif markers.exit_success_count >= 1:
            status, reason, exit_code = (
                "UNKNOWN",
                "exit_success_without_exact_crc32_signature",
                3,
            )
        else:
            status, reason, exit_code = (
                "UNKNOWN",
                "no_strict_terminal_outcome_marker",
                3,
            )

    return Verdict(
        schema_version="1.0",
        verdict_engine_version=PROGRAM_VERSION,
        generated_at_utc=utc_now(),
        phase=phase,
        run_kind=run_kind,
        xrun_exit_status=xrun_exit_status,
        status=status,
        reason=reason,
        recommended_exit_code=exit_code,
        markers=markers,
        strict_success_requirements={
            "xrun_exit_status": 0,
            "required_exact_signature": (
                "CRC32 PASS: vector=cbf43926 "
                "signature=2d6352b3 last=5650ac83"
            ),
            "required_exit_marker": "EXIT SUCCESS",
            "exit_success_alone_is_success": False,
        },
    )


def atomic_write(path: Path, text: str) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("compile", "run"), required=True)
    parser.add_argument("--run-kind", choices=("golden", "fault"), required=True)
    parser.add_argument("--xrun-status", type=int, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--result-json", type=Path, required=True)
    parser.add_argument("--result-text", type=Path, required=True)
    parser.add_argument("--result-env", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_path = args.log.resolve()
    log_text = (
        log_path.read_text(encoding="utf-8", errors="replace")
        if log_path.is_file()
        else None
    )
    try:
        verdict = compute_verdict(
            phase=args.phase,
            run_kind=args.run_kind,
            xrun_exit_status=args.xrun_status,
            log_text=log_text,
        )
    except VerdictError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 4

    payload = asdict(verdict)
    atomic_write(
        args.result_json,
        json.dumps(payload, indent=2, sort_keys=False) + "\n",
    )
    atomic_write(args.result_text, verdict.status + "\n")
    atomic_write(
        args.result_env,
        "\n".join(
            [
                f"phase={verdict.phase}",
                f"run_kind={verdict.run_kind}",
                f"xrun_exit_status={verdict.xrun_exit_status}",
                f"result={verdict.status}",
                f"reason={verdict.reason}",
                f"recommended_exit_code={verdict.recommended_exit_code}",
                "",
            ]
        ),
    )
    print(verdict.status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
