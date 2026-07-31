#!/usr/bin/env python3
"""Stage-5 layered verdict engine for native and diagnostic execution.

The engine separates tool validity, execution completion, workload outcome,
pre-existing detector evidence, and diagnostic intervention.  Diagnostic modes
never redefine the natural architectural outcome: once termination is
suppressed or a transaction is quarantined, all later behavior is marked
counterfactual.
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

PROGRAM_VERSION = "4.0.0"
SCHEMA_VERSION = "3.0"
POLICY_VERSION = "stage5_native_diagnostic_execution_v1"

RUN_PURPOSE_TO_MODE = {
    "COMPILE_CHECK": "native",
    "NATIVE_CHARACTERIZATION": "native",
    "DIAGNOSTIC_OBSERVE": "observe",
    "DIAGNOSTIC_QUARANTINE": "diagnostic_quarantine",
}

EXACT_SIGNATURE_RE = re.compile(
    r"CRC32\s+PASS:\s*vector=(?:0x)?cbf43926\s+"
    r"signature=(?:0x)?2d6352b3\s+last=(?:0x)?5650ac83\b",
    re.IGNORECASE,
)
ANY_CRC_PASS_RE = re.compile(r"CRC32\s+PASS:", re.IGNORECASE)
EXIT_SUCCESS_RE = re.compile(r"\bEXIT\s+SUCCESS\b", re.IGNORECASE)
TIMEOUT_RE = re.compile(
    r"Simulation aborted due to maximum cycle limit|maximum cycle limit|"
    r"\bMAXCYCLES\b.*(?:reached|exceeded)",
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
    r"(?P<time_value>[0-9]+(?:\.[0-9]+)?)\s+(?P<time_unit>[A-Za-z]+)",
    re.IGNORECASE,
)
GENERIC_INFRA_RE = re.compile(
    r"\b(?:segmentation fault|core dumped|internal error)\b|^FATAL:\s",
    re.IGNORECASE,
)


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


def classify_origin(source_file: str, assertion_name: str) -> str:
    source = source_file.replace("\\", "/").lower()
    if "/verification/" in source or "/tb/" in source or assertion_name.startswith("tb_top."):
        return "PREEXISTING_TB_ASSERTION"
    return "PREEXISTING_DESIGN_ASSERTION"


def parse_structured_events(path: Path | None, expected_mode: str) -> tuple[list[dict[str, Any]], list[str]]:
    if path is None:
        return [], ["assertion event path not provided"]
    if not path.is_file() or path.stat().st_size == 0:
        return [], [f"assertion event file missing or empty: {path}"]
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    header_count = 0
    for line_number, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        if not raw:
            continue
        fields = raw.split("\t")
        if fields[0] == "H":
            header_count += 1
            if fields != ["H", "F2A_ASSERT_EVENTS", "1", expected_mode]:
                errors.append(f"line {line_number}: invalid assertion-event header {fields!r}")
            continue
        if fields[0] != "A" or len(fields) != 11:
            errors.append(f"line {line_number}: malformed assertion event {fields!r}")
            continue
        try:
            event_index = int(fields[1])
            cycle = int(fields[2])
        except ValueError as exc:
            errors.append(f"line {line_number}: {exc}")
            continue
        events.append(
            {
                "event_index": event_index,
                "event_file_line": line_number,
                "cycle": cycle,
                "simulation_time": fields[3],
                "detector_origin": fields[4],
                "assertion_leaf_name": fields[5],
                "detector_reported_effect_hint": fields[6],
                "address": fields[7].lower(),
                "write_data": fields[8].lower(),
                "byte_enable": fields[9].lower(),
                "action": fields[10],
                "source": "STRUCTURED_STAGE5_EVENT",
            }
        )
    if header_count != 1:
        errors.append(f"assertion event file must contain one header; found {header_count}")
    for expected_index, event in enumerate(events):
        if event["event_index"] != expected_index:
            errors.append(
                f"assertion event indexes must be contiguous: expected {expected_index}, "
                f"found {event['event_index']}"
            )
    return events, errors


def parse_asrtst_events(lines: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        detail = ASRTST_DETAIL_RE.match(line)
        if detail is None:
            continue
        assertion_name = detail.group("assertion_name")
        source_file = detail.group("source_file")
        event: dict[str, Any] = {
            "log_line": index + 1,
            "severity": detail.group("severity").upper(),
            "mnemonic": "ASRTST",
            "detector_origin": classify_origin(source_file, assertion_name),
            "assertion_name": assertion_name,
            "assertion_leaf_name": assertion_name.rsplit(".", 1)[-1],
            "source_file": source_file,
            "source_line": int(detail.group("source_line")),
            "simulation_time": {
                "value": detail.group("time_value"),
                "unit": detail.group("time_unit").upper(),
            },
            "action": "ASSERTION_FAILURE",
            "source": "XCELIUM_ASRTST",
        }
        for next_index in range(index + 1, min(len(lines), index + 8)):
            termination = FATAL_TERMINATION_RE.search(lines[next_index])
            if termination is not None:
                event["action"] = "FATAL_TERMINATION"
                event["fatal_exit_code"] = termination.group("code").strip()
                event["termination_log_line"] = next_index + 1
                break
        events.append(event)
    return events


def parse_infrastructure_events(lines: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    timeout_lines = {i for i, line in enumerate(lines) if TIMEOUT_RE.search(line)}
    for index, line in enumerate(lines):
        diagnostic = TOOL_DIAGNOSTIC_RE.match(line)
        if diagnostic is not None:
            tool = diagnostic.group("tool").lower()
            mnemonic = diagnostic.group("mnemonic").upper()
            if tool == "xmsim" and mnemonic == "ASRTST":
                continue
            # A simulator fatal immediately adjacent to the explicit max-cycle
            # message is the expected watchdog terminal event, not tool failure.
            if any(abs(index - timeout_index) <= 3 for timeout_index in timeout_lines):
                continue
            events.append(
                {
                    "log_line": index + 1,
                    "tool": tool,
                    "severity": diagnostic.group("severity").upper(),
                    "mnemonic": mnemonic,
                    "message": line,
                }
            )
        elif GENERIC_INFRA_RE.search(line):
            events.append(
                {
                    "log_line": index + 1,
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
    event_file_exists: bool
    exact_signature_count: int
    any_crc_pass_count: int
    exit_success_count: int
    timeout_count: int
    output_failure_count: int
    runner_error_count: int
    structured_detector_event_count: int
    xcelium_assertion_event_count: int
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
    assertion_mode: str
    xrun_exit_status: int
    status: str
    reason: str
    recommended_exit_code: int
    raw_facts: dict[str, Any]
    markers: Markers
    interpretation_contract: dict[str, bool]


def compute_verdict(
    *,
    phase: str,
    run_kind: str,
    run_purpose: str,
    xrun_exit_status: int,
    log_text: str | None,
    event_path: Path | None,
) -> Verdict:
    if phase not in {"compile", "run"}:
        raise VerdictError(f"invalid phase: {phase}")
    if run_kind not in {"golden", "fault"}:
        raise VerdictError(f"invalid run kind: {run_kind}")
    if run_purpose not in RUN_PURPOSE_TO_MODE:
        raise VerdictError(f"invalid run purpose: {run_purpose}")
    if phase == "compile" and run_purpose != "COMPILE_CHECK":
        raise VerdictError("compile phase requires COMPILE_CHECK")
    if run_kind == "golden" and run_purpose not in {"COMPILE_CHECK", "NATIVE_CHARACTERIZATION"}:
        raise VerdictError("golden runs cannot use diagnostic assertion modes")

    mode = RUN_PURPOSE_TO_MODE[run_purpose]
    lines = log_text.splitlines() if log_text is not None else []
    structured_events: list[dict[str, Any]] = []
    event_errors: list[str] = []
    if phase == "run":
        structured_events, event_errors = parse_structured_events(event_path, mode)
    asrtst_events = parse_asrtst_events(lines)
    infrastructure_events = parse_infrastructure_events(lines)

    markers = Markers(
        log_exists=log_text is not None,
        event_file_exists=bool(event_path and event_path.is_file()),
        exact_signature_count=count_matches(EXACT_SIGNATURE_RE, log_text or ""),
        any_crc_pass_count=count_matches(ANY_CRC_PASS_RE, log_text or ""),
        exit_success_count=count_matches(EXIT_SUCCESS_RE, log_text or ""),
        timeout_count=count_matches(TIMEOUT_RE, log_text or ""),
        output_failure_count=count_matches(OUTPUT_FAILURE_RE, log_text or ""),
        runner_error_count=sum(1 for line in lines if RUNNER_ERROR_RE.search(line)),
        structured_detector_event_count=len(structured_events),
        xcelium_assertion_event_count=len(asrtst_events),
        infrastructure_error_count=len(infrastructure_events),
    )

    tool_status = "OK"
    valid_execution = True
    completion = "UNKNOWN"
    workload_outcome = "UNKNOWN"
    architectural_outcome = "UNKNOWN"
    status = "UNKNOWN"
    reason = "no_recognized_terminal_outcome"
    exit_code = 3

    if phase == "compile":
        completion = "COMPILE_ONLY"
        workload_outcome = "NOT_RUN"
        architectural_outcome = "NOT_RUN"
        if not markers.log_exists or markers.runner_error_count or markers.infrastructure_error_count or xrun_exit_status != 0:
            status = "COMPILE_ERROR"
            reason = "compile_or_elaboration_failed"
            exit_code = 4
            tool_status = "ERROR"
            valid_execution = False
        else:
            status = "COMPILE_PASS"
            reason = "compile_and_elaboration_completed_without_error"
            exit_code = 0
    elif not markers.log_exists:
        status, reason, exit_code = "ERROR", "xrun_log_missing", 4
        tool_status, valid_execution = "ERROR", False
    elif event_errors:
        status, reason, exit_code = "ERROR", "assertion_event_file_invalid", 4
        tool_status, valid_execution = "ERROR", False
    elif markers.runner_error_count:
        status, reason, exit_code = "ERROR", "stage5_runner_invariant_error", 4
        tool_status, valid_execution = "ERROR", False
    elif markers.infrastructure_error_count:
        status, reason, exit_code = "ERROR", "xcelium_infrastructure_error_marker", 4
        tool_status, valid_execution = "ERROR", False
    else:
        timeout_line = first_match_line(TIMEOUT_RE, lines)
        failure_line = first_match_line(OUTPUT_FAILURE_RE, lines)
        exact_line = first_match_line(EXACT_SIGNATURE_RE, lines)
        exit_line = first_match_line(EXIT_SUCCESS_RE, lines)
        wrong_crc_line = first_match_line(ANY_CRC_PASS_RE, lines) if exact_line is None else None
        assertion_line = min(
            (int(event.get("log_line", 10**12)) for event in asrtst_events),
            default=None,
        )
        candidates: list[tuple[int, str]] = []
        if assertion_line is not None:
            candidates.append((assertion_line, "ASSERTION_FATAL"))
        if timeout_line is not None:
            candidates.append((timeout_line, "TIMEOUT"))
        if failure_line is not None:
            candidates.append((failure_line, "WORKLOAD_FAILURE"))
        if exact_line is not None and exit_line is not None and xrun_exit_status == 0:
            candidates.append((exit_line, "WORKLOAD_SUCCESS"))
        elif wrong_crc_line is not None:
            candidates.append((wrong_crc_line, "WRONG_SIGNATURE"))
        terminal = min(candidates)[1] if candidates else None

        counterfactual = mode != "native" and bool(structured_events)
        if terminal == "ASSERTION_FATAL":
            completion = "TERMINATED_BY_EXISTING_ASSERTION"
            workload_outcome = "NOT_REACHED"
            architectural_outcome = "CENSORED"
            if mode == "native" and run_kind == "fault":
                status = "EXISTING_ASSERTION_DETECTED"
                reason = "native_fault_execution_terminated_by_preexisting_assertion"
                exit_code = 2
            elif run_kind == "golden":
                status, reason, exit_code = "GOLDEN_INVALID", "golden_assertion_failure", 4
                valid_execution = False
            else:
                status, reason, exit_code = "ERROR", "diagnostic_mode_did_not_suppress_assertion_termination", 4
                valid_execution = False
        elif terminal == "TIMEOUT":
            completion = "TIMED_OUT"
            workload_outcome = "NOT_REACHED"
            architectural_outcome = "COUNTERFACTUAL_CENSORED" if counterfactual else "CENSORED"
            if run_kind == "golden":
                status, reason, exit_code = "GOLDEN_INVALID", "golden_execution_timed_out", 4
                valid_execution = False
            elif mode == "native":
                status, reason, exit_code = "TIMEOUT", "maximum_cycle_limit_reached", 2
            else:
                status, reason, exit_code = "DIAGNOSTIC_TIMEOUT", "diagnostic_execution_reached_maximum_cycle_limit", 2
        elif terminal in {"WORKLOAD_FAILURE", "WRONG_SIGNATURE"}:
            completion = "COMPLETED"
            workload_outcome = "FAIL"
            architectural_outcome = "COUNTERFACTUAL_FAIL" if counterfactual else "OBSERVED_FAIL"
            if run_kind == "golden":
                status, reason, exit_code = "GOLDEN_INVALID", "golden_workload_result_failed", 4
                valid_execution = False
            elif mode == "native":
                status, reason, exit_code = "OUTPUT_MISMATCH", "native_workload_failure", 2
            else:
                status, reason, exit_code = "DIAGNOSTIC_OUTPUT_MISMATCH", "diagnostic_workload_failure", 2
        elif terminal == "WORKLOAD_SUCCESS":
            completion = "COMPLETED"
            workload_outcome = "PASS"
            architectural_outcome = "COUNTERFACTUAL_PASS" if counterfactual else "OBSERVED_PASS"
            if run_kind == "golden":
                status, reason, exit_code = "PASS", "exact_crc32_signature_and_exit_success", 0
            elif mode == "native":
                status, reason, exit_code = "OUTPUT_MATCH", "exact_crc32_signature_and_exit_success", 0
            else:
                status, reason, exit_code = "DIAGNOSTIC_OUTPUT_MATCH", "diagnostic_exact_crc32_signature_and_exit_success", 0
        elif xrun_exit_status != 0:
            status, reason, exit_code = "ERROR", "xrun_nonzero_without_recognized_terminal_event", 4
            tool_status, valid_execution = "ERROR", False
        elif markers.exit_success_count:
            status, reason, exit_code = "UNKNOWN", "exit_success_without_exact_crc32_signature", 3
            valid_execution = False
        else:
            valid_execution = False

    intervention = {
        "assertion_mode": mode,
        "termination_suppressed": mode != "native",
        "transaction_quarantine": mode == "diagnostic_quarantine",
        "counterfactual_after_first_detector_event": mode != "native",
        "intervention_applied": bool(structured_events) and mode != "native",
    }
    raw_facts = {
        "tool": {
            "status": tool_status,
            "xrun_exit_status": xrun_exit_status,
            "runner_error_count": markers.runner_error_count,
            "infrastructure_error_count": markers.infrastructure_error_count,
            "infrastructure_events": infrastructure_events,
            "assertion_event_file_errors": event_errors,
        },
        "execution": {
            "run_purpose": run_purpose,
            "assertion_mode": mode,
            "valid_experiment_execution": valid_execution,
            "completion": completion,
            "native_execution": mode == "native",
            "diagnostic_execution": mode != "native",
            "continuation_after_detector_event": bool(structured_events) and completion != "TERMINATED_BY_EXISTING_ASSERTION",
        },
        "workload": {
            "outcome": workload_outcome,
            "architectural_outcome": architectural_outcome,
            "exact_signature_observed": markers.exact_signature_count > 0,
            "exit_success_observed": markers.exit_success_count > 0,
            "explicit_failure_observed": markers.output_failure_count > 0,
            "timeout_observed": markers.timeout_count > 0,
        },
        "existing_detector_baseline": {
            "triggered": bool(structured_events or asrtst_events),
            "structured_event_count": len(structured_events),
            "xcelium_assertion_event_count": len(asrtst_events),
            "events": structured_events,
            "xcelium_events": asrtst_events,
        },
        "intervention": intervention,
    }

    return Verdict(
        schema_version=SCHEMA_VERSION,
        verdict_engine_version=PROGRAM_VERSION,
        policy_version=POLICY_VERSION,
        generated_at_utc=utc_now(),
        phase=phase,
        run_kind=run_kind,
        run_purpose=run_purpose,
        assertion_mode=mode,
        xrun_exit_status=xrun_exit_status,
        status=status,
        reason=reason,
        recommended_exit_code=exit_code,
        raw_facts=raw_facts,
        markers=markers,
        interpretation_contract={
            "native_result_defines_natural_completion": True,
            "observe_and_quarantine_results_are_counterfactual_after_first_event": True,
            "existing_detector_event_is_raw_evidence_not_final_oracle": True,
            "quarantine_outcome_cannot_replace_native_architectural_outcome": True,
            "ai_generated_assertions_are_out_of_scope": True,
        },
    )


def atomic_write(path: Path, text: str) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("compile", "run"), required=True)
    parser.add_argument("--run-kind", choices=("golden", "fault"), required=True)
    parser.add_argument("--run-purpose", choices=sorted(RUN_PURPOSE_TO_MODE), required=True)
    parser.add_argument("--xrun-status", type=int, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--assert-events", type=Path)
    parser.add_argument("--result-json", type=Path, required=True)
    parser.add_argument("--result-text", type=Path, required=True)
    parser.add_argument("--result-env", type=Path, required=True)
    parser.add_argument("--version", action="version", version=f"%(prog)s {PROGRAM_VERSION}")
    args = parser.parse_args(argv)

    log_path = args.log.resolve()
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else None
    event_path = args.assert_events.resolve() if args.assert_events else None
    try:
        verdict = compute_verdict(
            phase=args.phase,
            run_kind=args.run_kind,
            run_purpose=args.run_purpose,
            xrun_exit_status=args.xrun_status,
            log_text=log_text,
            event_path=event_path,
        )
    except VerdictError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 4

    payload = asdict(verdict)
    atomic_write(args.result_json, json.dumps(payload, indent=2) + "\n")
    atomic_write(args.result_text, verdict.status + "\n")
    raw = verdict.raw_facts
    atomic_write(
        args.result_env,
        "\n".join(
            [
                f"phase={verdict.phase}",
                f"run_kind={verdict.run_kind}",
                f"run_purpose={verdict.run_purpose}",
                f"assertion_mode={verdict.assertion_mode}",
                f"xrun_exit_status={verdict.xrun_exit_status}",
                f"result={verdict.status}",
                f"reason={verdict.reason}",
                f"recommended_exit_code={verdict.recommended_exit_code}",
                f"tool_status={raw['tool']['status']}",
                f"execution_completion={raw['execution']['completion']}",
                f"workload_outcome={raw['workload']['outcome']}",
                f"architectural_outcome={raw['workload']['architectural_outcome']}",
                f"existing_detector_count={raw['existing_detector_baseline']['structured_event_count']}",
                "",
            ]
        ),
    )
    print(verdict.status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
