#!/usr/bin/env python3
"""Layered Stage-5 native-execution result parser.

This module intentionally separates four questions that the old verdict engine
collapsed into one status:

1. Did the simulator/tool infrastructure execute correctly?
2. Why did the native execution stop?
3. Was the workload result observed, failed, or censored?
4. Did a pre-existing design/testbench detector fire?

A pre-existing SystemVerilog assertion failure in a *fault* run is therefore
recorded as a valid scientific observation, not as an Xcelium infrastructure
error.  It does not, by itself, assign a fault-effect oracle class.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

PROGRAM_VERSION = "3.0.0"
SCHEMA_VERSION = "2.0"
POLICY_VERSION = "native_execution_raw_facts_v1"

RUN_PURPOSES = {
    "COMPILE_CHECK",
    "NATIVE_CHARACTERIZATION",
    "DIAGNOSTIC_CONTINUATION",
    "ASSERTION_EVALUATION_PASSIVE",
    "ASSERTION_DEPLOYMENT_FAILFAST",
}

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
RUNNER_ERROR_RE = re.compile(r"^F2A_RUNNER_ERROR:", re.IGNORECASE)
TOOL_DIAGNOSTIC_RE = re.compile(
    r"^(?P<tool>xmvlog|xmelab|xmsim|xrun):\s*"
    r"\*(?P<severity>[EF]),(?P<mnemonic>[A-Za-z0-9_]+):?\s*(?P<message>.*)$",
    re.IGNORECASE,
)
ASRTST_DETAIL_RE = re.compile(
    r"^xmsim:\s*\*(?P<severity>[EF]),ASRTST:?\s*"
    r"\((?P<source_file>.+?),(?P<source_line>\d+)\):\s*"
    r"\(time\s+(?P<time_value>[0-9]+(?:\.[0-9]+)?)\s+"
    r"(?P<time_unit>[A-Za-z]+)\)\s*"
    r"Assertion\s+(?P<assertion_name>\S+)\s+has\s+failed",
    re.IGNORECASE,
)
FATAL_TERMINATION_RE = re.compile(
    r"Simulation terminated via \$fatal\((?P<code>[^)]*)\)\s+at\s+time\s+"
    r"(?P<time_value>[0-9]+(?:\.[0-9]+)?)\s+"
    r"(?P<time_unit>[A-Za-z]+)",
    re.IGNORECASE,
)
FATAL_SOURCE_RE = re.compile(
    r"^(?P<source_file>/.*?):(?P<source_line>\d+)\s+.*?\$fatal\((?P<body>.*)\);?\s*$"
)
GENERIC_INFRA_RE = re.compile(
    r"\b(?:segmentation fault|core dumped|internal error)\b|^FATAL:\s",
    re.IGNORECASE,
)

SUCCESS_STATUSES = {"COMPILE_PASS", "PASS", "OUTPUT_MATCH"}
VALID_FAULT_OBSERVATION_STATUSES = {
    "OUTPUT_MATCH",
    "OUTPUT_MISMATCH",
    "TIMEOUT",
    "EXISTING_ASSERTION_DETECTED",
}


class VerdictError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def count_matches(pattern: re.Pattern[str], text: str) -> int:
    return sum(1 for _ in pattern.finditer(text))


def first_match_line(pattern: re.Pattern[str], lines: list[str]) -> int | None:
    for index, line in enumerate(lines, start=1):
        if pattern.search(line):
            return index
    return None


def classify_detector_origin(source_file: str, assertion_name: str) -> str:
    source = source_file.replace("\\", "/").lower()
    assertion = assertion_name.lower()
    if (
        "/verification/" in source
        or "/tb/" in source
        or source.endswith("/tb_top.sv")
        or assertion.startswith("tb_top.")
    ):
        return "PREEXISTING_TB_ASSERTION"
    return "PREEXISTING_DESIGN_ASSERTION"


def parse_assertion_events(lines: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        diagnostic = TOOL_DIAGNOSTIC_RE.match(line)
        if diagnostic is None:
            continue
        if diagnostic.group("tool").lower() != "xmsim":
            continue
        if diagnostic.group("mnemonic").upper() != "ASRTST":
            continue

        detail = ASRTST_DETAIL_RE.match(line)
        event: dict[str, Any] = {
            "event_index": len(events),
            "log_line": index + 1,
            "tool": "xmsim",
            "severity": diagnostic.group("severity").upper(),
            "mnemonic": "ASRTST",
            "detector_origin": "PREEXISTING_ASSERTION_UNKNOWN_ORIGIN",
            "assertion_name": None,
            "assertion_leaf_name": None,
            "source_file": None,
            "source_line": None,
            "simulation_time": None,
            "action": "ASSERTION_FAILURE",
            "termination_log_line": None,
            "fatal_exit_code": None,
            "fatal_source_statement": None,
            "raw_message": line,
        }
        if detail is not None:
            assertion_name = detail.group("assertion_name")
            source_file = detail.group("source_file")
            event.update(
                {
                    "detector_origin": classify_detector_origin(
                        source_file, assertion_name
                    ),
                    "assertion_name": assertion_name,
                    "assertion_leaf_name": assertion_name.rsplit(".", 1)[-1],
                    "source_file": source_file,
                    "source_line": int(detail.group("source_line")),
                    "simulation_time": {
                        "value": detail.group("time_value"),
                        "unit": detail.group("time_unit").upper(),
                    },
                }
            )

        # Xcelium prints the $fatal termination immediately after ASRTST.  Keep
        # the search local so an unrelated later fatal cannot be attached.
        for next_index in range(index + 1, min(len(lines), index + 8)):
            termination = FATAL_TERMINATION_RE.search(lines[next_index])
            if termination is not None:
                event["action"] = "FATAL_TERMINATION"
                event["termination_log_line"] = next_index + 1
                event["fatal_exit_code"] = termination.group("code").strip()
                event["termination_time"] = {
                    "value": termination.group("time_value"),
                    "unit": termination.group("time_unit").upper(),
                }
            source_statement = FATAL_SOURCE_RE.match(lines[next_index])
            if source_statement is not None:
                event["fatal_source_statement"] = lines[next_index].strip()
        events.append(event)
    return events


def parse_infrastructure_events(lines: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        diagnostic = TOOL_DIAGNOSTIC_RE.match(line)
        if diagnostic is not None:
            tool = diagnostic.group("tool").lower()
            mnemonic = diagnostic.group("mnemonic").upper()
            # ASRTST is a design/testbench detector event.  It is not an
            # infrastructure failure when running a fault natively.
            if tool == "xmsim" and mnemonic == "ASRTST":
                continue
            events.append(
                {
                    "log_line": index,
                    "tool": tool,
                    "severity": diagnostic.group("severity").upper(),
                    "mnemonic": mnemonic,
                    "message": line,
                }
            )
            continue
        if GENERIC_INFRA_RE.search(line):
            events.append(
                {
                    "log_line": index,
                    "tool": "unknown",
                    "severity": "F",
                    "mnemonic": "GENERIC_INFRASTRUCTURE_FAILURE",
                    "message": line,
                }
            )
    return events


@dataclass(frozen=True)
class Markers:
    log_exists: bool
    exact_signature_count: int
    any_crc_pass_count: int
    exit_success_count: int
    timeout_count: int
    output_failure_count: int
    runner_error_count: int
    existing_assertion_event_count: int
    infrastructure_error_count: int


@dataclass(frozen=True)
class Verdict:
    schema_version: str
    verdict_engine_version: str
    policy_version: str
    generated_at_utc: str
    phase: str
    run_kind: str
    run_purpose: str
    xrun_exit_status: int
    status: str
    reason: str
    recommended_exit_code: int
    raw_facts: dict[str, Any]
    markers: Markers
    interpretation_contract: dict[str, bool]
    strict_success_requirements: dict[str, object]


def inspect_markers(log_text: str | None) -> tuple[Markers, dict[str, Any]]:
    if log_text is None:
        markers = Markers(False, 0, 0, 0, 0, 0, 0, 0, 0)
        return markers, {
            "lines": [],
            "assertion_events": [],
            "infrastructure_events": [],
        }
    lines = log_text.splitlines()
    assertion_events = parse_assertion_events(lines)
    infrastructure_events = parse_infrastructure_events(lines)
    markers = Markers(
        log_exists=True,
        exact_signature_count=count_matches(EXACT_SIGNATURE_RE, log_text),
        any_crc_pass_count=count_matches(ANY_CRC_PASS_RE, log_text),
        exit_success_count=count_matches(EXIT_SUCCESS_RE, log_text),
        timeout_count=count_matches(TIMEOUT_RE, log_text),
        output_failure_count=count_matches(OUTPUT_FAILURE_RE, log_text),
        runner_error_count=sum(1 for line in lines if RUNNER_ERROR_RE.search(line)),
        existing_assertion_event_count=len(assertion_events),
        infrastructure_error_count=len(infrastructure_events),
    )
    return markers, {
        "lines": lines,
        "assertion_events": assertion_events,
        "infrastructure_events": infrastructure_events,
    }


def build_raw_facts(
    *,
    run_purpose: str,
    xrun_exit_status: int,
    tool_status: str,
    valid_execution: bool,
    completion: str,
    terminal_event_type: str | None,
    terminal_log_line: int | None,
    workload_outcome: str,
    architectural_outcome: str,
    markers: Markers,
    parsed: dict[str, Any],
) -> dict[str, Any]:
    assertion_events = parsed["assertion_events"]
    infrastructure_events = parsed["infrastructure_events"]
    return {
        "tool": {
            "status": tool_status,
            "xrun_exit_status": xrun_exit_status,
            "runner_error_count": markers.runner_error_count,
            "infrastructure_error_count": markers.infrastructure_error_count,
            "infrastructure_events": infrastructure_events,
        },
        "execution": {
            "run_purpose": run_purpose,
            "valid_experiment_execution": valid_execution,
            "completion": completion,
            "terminal_event_type": terminal_event_type,
            "terminal_log_line": terminal_log_line,
            "native_execution": run_purpose == "NATIVE_CHARACTERIZATION",
            "post_terminal_execution_observed": False,
        },
        "workload": {
            "outcome": workload_outcome,
            "architectural_outcome": architectural_outcome,
            "exact_signature_observed": markers.exact_signature_count > 0,
            "any_crc_pass_observed": markers.any_crc_pass_count > 0,
            "exit_success_observed": markers.exit_success_count > 0,
            "explicit_failure_observed": markers.output_failure_count > 0,
            "timeout_observed": markers.timeout_count > 0,
        },
        "existing_detector_baseline": {
            "triggered": bool(assertion_events),
            "event_count": len(assertion_events),
            "events": assertion_events,
        },
    }


def compute_verdict(
    *,
    phase: str,
    run_kind: str,
    xrun_exit_status: int,
    log_text: str | None,
    run_purpose: str | None = None,
) -> Verdict:
    if phase not in {"compile", "run"}:
        raise VerdictError(f"phase must be compile or run; got {phase!r}")
    if run_kind not in {"golden", "fault"}:
        raise VerdictError(f"run_kind must be golden or fault; got {run_kind!r}")
    if xrun_exit_status < 0:
        raise VerdictError("xrun exit status must be non-negative")

    if run_purpose is None:
        run_purpose = "COMPILE_CHECK" if phase == "compile" else "NATIVE_CHARACTERIZATION"
    if run_purpose not in RUN_PURPOSES:
        raise VerdictError(f"unsupported run purpose: {run_purpose!r}")
    if phase == "compile" and run_purpose != "COMPILE_CHECK":
        raise VerdictError("compile phase requires COMPILE_CHECK run purpose")

    markers, parsed = inspect_markers(log_text)
    lines: list[str] = parsed["lines"]
    status: str
    reason: str
    exit_code: int
    tool_status = "OK"
    valid_execution = True
    completion = "UNKNOWN"
    terminal_event_type: str | None = None
    terminal_log_line: int | None = None
    workload_outcome = "UNKNOWN"
    architectural_outcome = "UNKNOWN"

    if phase == "compile":
        completion = "COMPILE_ONLY"
        workload_outcome = "NOT_RUN"
        architectural_outcome = "NOT_RUN"
        if not markers.log_exists:
            status, reason, exit_code = "COMPILE_ERROR", "xrun_log_missing", 4
            tool_status = "ERROR"
            valid_execution = False
        elif markers.runner_error_count:
            status, reason, exit_code = (
                "COMPILE_ERROR",
                "stage5_runner_invariant_error",
                4,
            )
            tool_status = "ERROR"
            valid_execution = False
        elif markers.infrastructure_error_count:
            status, reason, exit_code = (
                "COMPILE_ERROR",
                "xcelium_compile_or_elaboration_error_marker",
                4,
            )
            tool_status = "ERROR"
            valid_execution = False
        elif xrun_exit_status != 0:
            status, reason, exit_code = (
                "COMPILE_ERROR",
                "xrun_nonzero_during_compile_or_elaboration",
                4,
            )
            tool_status = "ERROR"
            valid_execution = False
        else:
            status, reason, exit_code = (
                "COMPILE_PASS",
                "compile_and_elaboration_completed_without_error",
                0,
            )
    elif not markers.log_exists:
        status, reason, exit_code = "ERROR", "xrun_log_missing", 4
        tool_status = "ERROR"
        valid_execution = False
    elif markers.runner_error_count:
        status, reason, exit_code = "ERROR", "stage5_runner_invariant_error", 4
        tool_status = "ERROR"
        valid_execution = False
    elif markers.infrastructure_error_count:
        status, reason, exit_code = "ERROR", "xcelium_infrastructure_error_marker", 4
        tool_status = "ERROR"
        valid_execution = False
    else:
        assertion_events: list[dict[str, Any]] = parsed["assertion_events"]
        assertion_line = (
            min(event["log_line"] for event in assertion_events)
            if assertion_events
            else None
        )
        timeout_line = first_match_line(TIMEOUT_RE, lines)
        output_failure_line = first_match_line(OUTPUT_FAILURE_RE, lines)
        exact_line = first_match_line(EXACT_SIGNATURE_RE, lines)
        exit_success_line = first_match_line(EXIT_SUCCESS_RE, lines)
        any_crc_line = first_match_line(ANY_CRC_PASS_RE, lines)

        terminal_candidates: list[tuple[int, str]] = []
        if assertion_line is not None:
            terminal_candidates.append((assertion_line, "PREEXISTING_ASSERTION"))
        if timeout_line is not None:
            terminal_candidates.append((timeout_line, "TIMEOUT"))
        if output_failure_line is not None:
            terminal_candidates.append((output_failure_line, "WORKLOAD_FAILURE"))
        if (
            exact_line is not None
            and exit_success_line is not None
            and xrun_exit_status == 0
        ):
            terminal_candidates.append((exit_success_line, "WORKLOAD_SUCCESS"))
        elif any_crc_line is not None and exact_line is None:
            terminal_candidates.append((any_crc_line, "WRONG_FROZEN_SIGNATURE"))

        if terminal_candidates:
            terminal_log_line, terminal_event_type = min(terminal_candidates)

        if terminal_event_type == "PREEXISTING_ASSERTION":
            completion = "TERMINATED_BY_EXISTING_ASSERTION"
            workload_outcome = "NOT_REACHED"
            architectural_outcome = "CENSORED"
            if run_kind == "fault":
                status = "EXISTING_ASSERTION_DETECTED"
                reason = "native_fault_execution_terminated_by_preexisting_assertion"
                exit_code = 2
            else:
                status = "GOLDEN_INVALID"
                reason = "golden_execution_terminated_by_preexisting_assertion"
                exit_code = 4
                valid_execution = False
        elif terminal_event_type == "TIMEOUT":
            completion = "TIMED_OUT"
            workload_outcome = "NOT_REACHED"
            architectural_outcome = "CENSORED"
            if run_kind == "fault":
                status, reason, exit_code = "TIMEOUT", "maximum_cycle_limit_reached", 2
            else:
                status, reason, exit_code = (
                    "GOLDEN_INVALID",
                    "golden_execution_timed_out",
                    4,
                )
                valid_execution = False
        elif terminal_event_type in {"WORKLOAD_FAILURE", "WRONG_FROZEN_SIGNATURE"}:
            completion = "COMPLETED"
            workload_outcome = "FAIL"
            architectural_outcome = "OBSERVED_FAIL"
            if run_kind == "fault":
                status = "OUTPUT_MISMATCH"
                reason = (
                    "explicit_workload_failure_marker"
                    if terminal_event_type == "WORKLOAD_FAILURE"
                    else "crc32_pass_marker_did_not_match_frozen_signature"
                )
                exit_code = 2
            else:
                status = "GOLDEN_INVALID"
                reason = "golden_workload_result_failed"
                exit_code = 4
                valid_execution = False
        elif terminal_event_type == "WORKLOAD_SUCCESS":
            completion = "COMPLETED"
            workload_outcome = "PASS"
            architectural_outcome = "OBSERVED_PASS"
            status = "PASS" if run_kind == "golden" else "OUTPUT_MATCH"
            reason = "exact_crc32_signature_and_exit_success"
            exit_code = 0
        elif xrun_exit_status != 0:
            status, reason, exit_code = "ERROR", "xrun_nonzero_without_recognized_terminal_event", 4
            tool_status = "ERROR"
            valid_execution = False
        elif markers.exit_success_count > 0:
            status, reason, exit_code = (
                "UNKNOWN",
                "exit_success_without_exact_crc32_signature",
                3,
            )
            valid_execution = False
        else:
            status, reason, exit_code = (
                "UNKNOWN",
                "no_recognized_terminal_outcome",
                3,
            )
            valid_execution = False

    raw_facts = build_raw_facts(
        run_purpose=run_purpose,
        xrun_exit_status=xrun_exit_status,
        tool_status=tool_status,
        valid_execution=valid_execution,
        completion=completion,
        terminal_event_type=terminal_event_type,
        terminal_log_line=terminal_log_line,
        workload_outcome=workload_outcome,
        architectural_outcome=architectural_outcome,
        markers=markers,
        parsed=parsed,
    )

    return Verdict(
        schema_version=SCHEMA_VERSION,
        verdict_engine_version=PROGRAM_VERSION,
        policy_version=POLICY_VERSION,
        generated_at_utc=utc_now(),
        phase=phase,
        run_kind=run_kind,
        run_purpose=run_purpose,
        xrun_exit_status=xrun_exit_status,
        status=status,
        reason=reason,
        recommended_exit_code=exit_code,
        raw_facts=raw_facts,
        markers=markers,
        interpretation_contract={
            "existing_assertion_event_is_raw_detection_evidence": True,
            "existing_assertion_event_is_not_fault_effect_oracle": True,
            "assertion_terminated_architectural_outcome_is_censored": True,
            "native_execution_does_not_suppress_existing_assertions": True,
            "ai_generated_assertions_do_not_participate_in_native_oracle_generation": True,
        },
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
    parser.add_argument("--run-purpose", choices=sorted(RUN_PURPOSES))
    parser.add_argument("--xrun-status", type=int, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--result-json", type=Path, required=True)
    parser.add_argument("--result-text", type=Path, required=True)
    parser.add_argument("--result-env", type=Path, required=True)
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {PROGRAM_VERSION}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
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
            run_purpose=args.run_purpose,
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
    raw = verdict.raw_facts
    atomic_write(
        args.result_env,
        "\n".join(
            [
                f"phase={verdict.phase}",
                f"run_kind={verdict.run_kind}",
                f"run_purpose={verdict.run_purpose}",
                f"xrun_exit_status={verdict.xrun_exit_status}",
                f"result={verdict.status}",
                f"reason={verdict.reason}",
                f"recommended_exit_code={verdict.recommended_exit_code}",
                f"tool_status={raw['tool']['status']}",
                f"execution_completion={raw['execution']['completion']}",
                f"workload_outcome={raw['workload']['outcome']}",
                f"architectural_outcome={raw['workload']['architectural_outcome']}",
                "existing_detector_count="
                f"{raw['existing_detector_baseline']['event_count']}",
                "",
            ]
        ),
    )
    print(verdict.status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
