#!/usr/bin/env python3
"""Classify one Stage-5 Xcelium compile or runtime result.

Runtime classification is policy driven and fail closed. The earliest terminal
behavior controls the verdict; later Xcelium fatal lines are retained as raw
evidence but cannot overwrite an earlier assertion, timeout, or workload result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

PROGRAM_VERSION = "5.3.0"
SCHEMA_VERSION = "3.0"

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
    r"^\s*(?P<tool>xmvlog|xmelab|xmsim|xrun):\s*"
    r"\*(?P<severity>[EF]),(?P<mnemonic>[A-Za-z0-9_]+):?\s*"
    r"(?P<message>.*)$",
    re.IGNORECASE,
)
SOURCE_PREFIX_RE = re.compile(
    r"^\((?P<file>.+?),(?P<line>\d+)(?:\|\d+)?\):\s*(?P<body>.*)$"
)
TIME_PREFIX_RE = re.compile(
    r"^\(time\s+(?P<value>[0-9]+(?:\.[0-9]+)?)\s+"
    r"(?P<unit>[A-Za-z]+)\)\s*(?P<body>.*)$",
    re.IGNORECASE,
)
ASRTST_BODY_RE = re.compile(
    r"Assertion\s+(?P<name>\S+)\s+has\s+failed",
    re.IGNORECASE,
)
FATAL_TRAILER_RE = re.compile(
    r"Simulation terminated via \$fatal\((?P<code>[^)]*)\)\s+at\s+time\s+"
    r"(?P<value>[0-9]+(?:\.[0-9]+)?)\s+(?P<unit>[A-Za-z]+)",
    re.IGNORECASE,
)
HEX_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:0x)?[0-9a-fA-FxXzZ_]{4,}(?![A-Za-z0-9_])"
)
DECIMAL_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])\d+(?![A-Za-z0-9_])")


class VerdictError(RuntimeError):
    """Controlled input or policy error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VerdictError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise VerdictError(f"invalid {label} JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise VerdictError(f"{label} must contain one JSON object: {path}")
    return value


def default_policy_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "platform/cv32e40p/stage5_assertion_policy_v1.json"
    )


def require_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VerdictError(f"{label} must be an object")
    return value


def validate_policy(policy: Mapping[str, Any]) -> None:
    if policy.get("schema_version") != "1.0":
        raise VerdictError("assertion policy schema_version must be 1.0")
    if policy.get("run_purpose_to_mode") != RUN_PURPOSE_TO_MODE:
        raise VerdictError("assertion policy run_purpose_to_mode mismatch")

    signature_policy = require_dict(
        policy.get("signature_policy"), "signature_policy"
    )
    for key in ("unknown_runtime_behavior", "ambiguous_signature_behavior"):
        if signature_policy.get(key) != "FAIL_CLOSED":
            raise VerdictError(f"signature policy {key} must be FAIL_CLOSED")

    regexes = signature_policy.get("true_infrastructure_message_regexes")
    if not isinstance(regexes, list) or not regexes:
        raise VerdictError("true infrastructure regexes are missing")
    for pattern in regexes:
        if not isinstance(pattern, str) or not pattern:
            raise VerdictError("invalid true infrastructure regex")
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise VerdictError(f"invalid infrastructure regex: {exc}") from exc

    detectors = policy.get("detectors")
    if not isinstance(detectors, list) or not detectors:
        raise VerdictError("assertion policy detectors array is missing")
    for detector in detectors:
        if not isinstance(detector, dict):
            raise VerdictError("detector records must be objects")
        for key in ("detector_id", "origin", "source_suffix", "assertion_leaf_name"):
            if not isinstance(detector.get(key), str) or not detector[key]:
                raise VerdictError(f"detector {key} is missing")
        diagnostic_suffix = detector.get("diagnostic_source_suffix")
        if diagnostic_suffix is not None and (
            not isinstance(diagnostic_suffix, str) or not diagnostic_suffix
        ):
            raise VerdictError("detector diagnostic_source_suffix is invalid")
        signatures = detector.get("termination_signatures")
        if not isinstance(signatures, list) or not signatures:
            raise VerdictError(f"{detector['detector_id']} has no signatures")
        for signature in signatures:
            if not isinstance(signature, dict):
                raise VerdictError("termination signatures must be objects")
            required = ("signature_id", "phase", "tool", "mnemonic", "terminal_kind")
            if any(not signature.get(key) for key in required):
                raise VerdictError("termination signature is incomplete")
            if signature.get("phase") != "run":
                raise VerdictError("detector signatures must apply to run phase")
            severities = signature.get("severities")
            if not isinstance(severities, list) or not set(severities) <= {"E", "F"}:
                raise VerdictError("termination signature severities are invalid")
            for regex_key in (
                "normalized_message_regex",
                "normalized_preceding_line_regex",
                "normalized_following_line_regex",
            ):
                regex_value = signature.get(regex_key)
                if regex_value is None:
                    continue
                try:
                    re.compile(str(regex_value), re.IGNORECASE)
                except re.error as exc:
                    raise VerdictError(
                        f"invalid {regex_key}: {exc}"
                    ) from exc


def count_matches(pattern: re.Pattern[str], text: str) -> int:
    return sum(1 for _ in pattern.finditer(text))


def first_line(pattern: re.Pattern[str], lines: Sequence[str]) -> int | None:
    for line_number, line in enumerate(lines, start=1):
        if pattern.search(line):
            return line_number
    return None


def normalize_path(value: str) -> str:
    return value.replace("\\", "/")


def normalize_message(value: str) -> str:
    value = HEX_TOKEN_RE.sub("<HEX>", value.strip().lower())
    value = DECIMAL_TOKEN_RE.sub("<NUM>", value)
    return re.sub(r"\s+", " ", value).strip(" .")


def split_tool_message(message: str) -> dict[str, Any]:
    source_file: str | None = None
    source_line: int | None = None
    simulation_time: dict[str, str] | None = None
    body = message.strip()

    source = SOURCE_PREFIX_RE.match(body)
    if source:
        source_file = source.group("file")
        source_line = int(source.group("line"))
        body = source.group("body").strip()

    time = TIME_PREFIX_RE.match(body)
    if time:
        simulation_time = {
            "value": time.group("value"),
            "unit": time.group("unit").upper(),
        }
        body = time.group("body").strip()

    return {
        "source_file": source_file,
        "source_line": source_line,
        "simulation_time": simulation_time,
        "message_body": body,
        "normalized_message": normalize_message(body),
    }


def source_origin(source_file: str | None, assertion_name: str | None) -> str:
    source = normalize_path(source_file or "").lower()
    if "/verification/" in source or "/tb/" in source:
        return "PREEXISTING_TB_ASSERTION"
    if assertion_name and assertion_name.startswith("tb_top."):
        return "PREEXISTING_TB_ASSERTION"
    if source_file or assertion_name:
        return "PREEXISTING_DESIGN_ASSERTION"
    return "UNKNOWN_ORIGIN"


def parse_tool_events(lines: Sequence[str]) -> list[dict[str, Any]]:
    """Parse Xcelium diagnostics and retain nearby message context.

    Xcelium FATSEV records may place the user-facing message on the line
    immediately before the FATSEV header.  The header itself can contain only
    source and simulation time.  Keeping a small bounded context lets policy
    signatures resolve that format without hard-coding detector semantics in
    the parser.
    """

    events: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        match = TOOL_DIAGNOSTIC_RE.match(line)
        if not match:
            continue

        details = split_tool_message(match.group("message"))
        assertion_name: str | None = None
        assertion_leaf: str | None = None
        if match.group("mnemonic").upper() == "ASRTST":
            assertion = ASRTST_BODY_RE.search(details["message_body"])
            if assertion:
                assertion_name = assertion.group("name")
                assertion_leaf = assertion_name.rsplit(".", 1)[-1]

        before = [
            item.strip()
            for item in lines[max(0, index - 3):index]
            if item.strip()
        ]
        after = [
            item.strip()
            for item in lines[index + 1:min(len(lines), index + 4)]
            if item.strip()
        ]

        events.append(
            {
                "log_line": index + 1,
                "tool": match.group("tool").lower(),
                "severity": match.group("severity").upper(),
                "mnemonic": match.group("mnemonic").upper(),
                "raw_line": line,
                "raw_message": match.group("message"),
                **details,
                "preceding_lines": before,
                "following_lines": after,
                "normalized_preceding_lines": [
                    normalize_message(item) for item in before
                ],
                "normalized_following_lines": [
                    normalize_message(item) for item in after
                ],
                "assertion_name": assertion_name,
                "assertion_leaf_name": assertion_leaf,
                "source_origin": source_origin(
                    details["source_file"], assertion_name
                ),
            }
        )
    return events


def parse_fatal_trailers(lines: Sequence[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        match = FATAL_TRAILER_RE.search(line)
        if match:
            events.append(
                {
                    "log_line": line_number,
                    "terminal_kind": "USER_FATAL_TRAILER",
                    "fatal_exit_code": match.group("code").strip(),
                    "simulation_time": {
                        "value": match.group("value"),
                        "unit": match.group("unit").upper(),
                    },
                    "raw_line": line,
                }
            )
    return events


def parse_structured_events(
    path: Path | None,
    expected_mode: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    if path is None:
        return [], ["assertion event path not provided"]
    if not path.is_file() or path.stat().st_size == 0:
        return [], [f"assertion event file missing or empty: {path}"]

    events: list[dict[str, Any]] = []
    errors: list[str] = []
    header_count = 0
    for line_number, raw in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(),
        start=1,
    ):
        if not raw:
            continue
        fields = raw.split("\t")
        if fields[0] == "H":
            header_count += 1
            expected = ["H", "F2A_ASSERT_EVENTS", "1", expected_mode]
            if fields != expected:
                errors.append(f"line {line_number}: invalid header {fields!r}")
            continue
        if fields[0] != "A" or len(fields) != 11:
            errors.append(f"line {line_number}: malformed event {fields!r}")
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
        errors.append(f"expected one header; found {header_count}")
    for expected_index, event in enumerate(events):
        if event["event_index"] != expected_index:
            errors.append(
                "event indexes are not contiguous: "
                f"expected {expected_index}, found {event['event_index']}"
            )
    return events, errors


def source_matches(source_file: Any, suffixes: Sequence[str]) -> bool:
    if not isinstance(source_file, str):
        return False
    normalized = normalize_path(source_file).lower()
    return any(
        normalized.endswith(normalize_path(suffix).lower())
        for suffix in suffixes
    )


def detector_source_suffixes(detector: Mapping[str, Any]) -> list[str]:
    values = [str(detector["source_suffix"])]
    diagnostic = detector.get("diagnostic_source_suffix")
    if isinstance(diagnostic, str) and diagnostic:
        values.append(diagnostic)
    return values


def any_line_matches(
    lines: Any,
    pattern: Any,
) -> bool:
    if pattern is None:
        return True
    if not isinstance(lines, list):
        return False
    compiled = re.compile(str(pattern), re.IGNORECASE)
    return any(compiled.fullmatch(str(line)) for line in lines)


def signature_matches(
    event: Mapping[str, Any],
    detector: Mapping[str, Any],
    signature: Mapping[str, Any],
) -> bool:
    if str(event.get("tool", "")).lower() != str(signature["tool"]).lower():
        return False
    if str(event.get("mnemonic", "")).upper() != str(
        signature["mnemonic"]
    ).upper():
        return False
    if event.get("severity") not in signature.get("severities", []):
        return False
    if signature.get("require_source_suffix") and not source_matches(
        event.get("source_file"), detector_source_suffixes(detector)
    ):
        return False
    if signature.get("require_assertion_leaf_name") and event.get(
        "assertion_leaf_name"
    ) != detector.get("assertion_leaf_name"):
        return False
    message_regex = signature.get("normalized_message_regex")
    if message_regex and not re.fullmatch(
        str(message_regex),
        str(event.get("normalized_message", "")),
        re.IGNORECASE,
    ):
        return False
    if not any_line_matches(
        event.get("normalized_preceding_lines"),
        signature.get("normalized_preceding_line_regex"),
    ):
        return False
    if not any_line_matches(
        event.get("normalized_following_lines"),
        signature.get("normalized_following_line_regex"),
    ):
        return False
    return True


def resolve_detector_events(
    tool_events: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    resolved: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    detectors = policy["detectors"]

    for event in tool_events:
        matches: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        for detector in detectors:
            for signature in detector["termination_signatures"]:
                if signature_matches(event, detector, signature):
                    matches.append((detector, signature))

        if len(matches) == 1:
            detector, signature = matches[0]
            resolved.append(
                {
                    **dict(event),
                    "semantic_class": "REGISTERED_DETECTOR_TERMINATION",
                    "detector_id": detector["detector_id"],
                    "detector_origin": detector["origin"],
                    "detector_leaf_name": detector["assertion_leaf_name"],
                    "assertion_leaf_name": detector["assertion_leaf_name"],
                    "effect_hint": detector.get("effect_hint"),
                    "signature_id": signature["signature_id"],
                    "terminal_kind": signature["terminal_kind"],
                    "action": signature["terminal_kind"],
                    "source": "XCELIUM_TERMINATION_SIGNATURE",
                }
            )
        elif len(matches) > 1:
            ambiguous.append(
                {
                    **dict(event),
                    "semantic_class": "AMBIGUOUS_REGISTERED_TERMINATION",
                    "matches": [
                        {
                            "detector_id": detector["detector_id"],
                            "signature_id": signature["signature_id"],
                        }
                        for detector, signature in matches
                    ],
                }
            )
    return resolved, ambiguous


def infrastructure_patterns(
    policy: Mapping[str, Any],
) -> list[re.Pattern[str]]:
    values = policy["signature_policy"]["true_infrastructure_message_regexes"]
    return [re.compile(str(value), re.IGNORECASE) for value in values]


def tool_event_context_text(event: Mapping[str, Any]) -> str:
    """Return one bounded Xcelium diagnostic block as searchable text.

    Xcelium may put the semantic message before or after the FATSEV header.
    The parser already stores three non-empty lines on each side; classification
    must use that bounded block rather than only the header line.
    """

    values: list[str] = [
        str(event.get("raw_line", "")),
        str(event.get("raw_message", "")),
        str(event.get("message_body", "")),
    ]
    for key in ("preceding_lines", "following_lines"):
        items = event.get(key)
        if isinstance(items, list):
            values.extend(str(item) for item in items)
    return "\n".join(values)


def classify_unmatched_tool_events(
    tool_events: Sequence[Mapping[str, Any]],
    resolved: Sequence[Mapping[str, Any]],
    ambiguous: Sequence[Mapping[str, Any]],
    patterns: Sequence[re.Pattern[str]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    handled_lines = {
        int(event["log_line"]) for event in [*resolved, *ambiguous]
    }
    infrastructure: list[dict[str, Any]] = []
    timeouts: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []

    for original in tool_events:
        event = dict(original)
        if int(event["log_line"]) in handled_lines:
            continue

        text = tool_event_context_text(event)

        # A watchdog is emitted as a FATSEV header followed by the semantic
        # message. Resolve the whole block as TIMEOUT before the generic
        # unmatched-FATSEV fail-closed path. This uses no fixed line number,
        # cycle count, fault ID, campaign path, or absolute source path.
        if event.get("tool") == "xmsim" and TIMEOUT_RE.search(text):
            event["semantic_class"] = "WATCHDOG_TIMEOUT"
            event["terminal_kind"] = "TIMEOUT"
            event["source"] = "XCELIUM_WATCHDOG_TERMINATION"
            timeouts.append(event)
            continue

        is_infrastructure = event.get("tool") in {"xmvlog", "xmelab", "xrun"}
        is_infrastructure |= any(pattern.search(text) for pattern in patterns)
        if is_infrastructure:
            event["semantic_class"] = "XCELIUM_INFRASTRUCTURE_FAILURE"
            infrastructure.append(event)
        else:
            event["semantic_class"] = "UNKNOWN_RUNTIME_TERMINATION"
            unknown.append(event)
    return infrastructure, timeouts, unknown


def event_line(events: Sequence[Mapping[str, Any]]) -> int | None:
    return min((int(event["log_line"]) for event in events), default=None)


def evidence_at(
    events: Sequence[Mapping[str, Any]],
    line_number: int | None,
) -> dict[str, Any] | None:
    return next(
        (
            dict(event)
            for event in events
            if line_number is not None and int(event["log_line"]) == line_number
        ),
        None,
    )


def candidate(
    line_number: int | None,
    kind: str,
    priority: int,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    if line_number is None:
        return None
    return {
        "log_line": line_number,
        "kind": kind,
        "priority": priority,
        "evidence": dict(evidence) if evidence else None,
    }


def choose_terminal(
    values: Iterable[dict[str, Any] | None],
) -> dict[str, Any] | None:
    candidates = [value for value in values if value is not None]
    return min(
        candidates,
        key=lambda value: (value["log_line"], value["priority"]),
        default=None,
    )


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
    policy: Mapping[str, Any],
    policy_path: Path,
) -> Verdict:
    if phase not in {"compile", "run"}:
        raise VerdictError(f"invalid phase: {phase}")
    if run_kind not in {"golden", "fault"}:
        raise VerdictError(f"invalid run kind: {run_kind}")
    if run_purpose not in RUN_PURPOSE_TO_MODE:
        raise VerdictError(f"invalid run purpose: {run_purpose}")
    if phase == "compile" and run_purpose != "COMPILE_CHECK":
        raise VerdictError("compile phase requires COMPILE_CHECK")
    if run_kind == "golden" and run_purpose not in {
        "COMPILE_CHECK",
        "NATIVE_CHARACTERIZATION",
    }:
        raise VerdictError("golden runs cannot use diagnostic modes")

    validate_policy(policy)
    mode = RUN_PURPOSE_TO_MODE[run_purpose]
    lines = log_text.splitlines() if log_text is not None else []
    structured_events, event_errors = ([], [])
    if phase == "run":
        structured_events, event_errors = parse_structured_events(
            event_path, mode
        )

    tool_events = parse_tool_events(lines)
    fatal_trailers = parse_fatal_trailers(lines)
    detector_events, ambiguous_events = resolve_detector_events(
        tool_events, policy
    )

    if phase == "compile":
        infrastructure_events = [
            {**event, "semantic_class": "COMPILE_OR_ELABORATION_FAILURE"}
            for event in tool_events
        ]
        timeout_events: list[dict[str, Any]] = []
        unknown_events: list[dict[str, Any]] = []
    else:
        (
            infrastructure_events,
            timeout_events,
            unknown_events,
        ) = classify_unmatched_tool_events(
            tool_events,
            detector_events,
            ambiguous_events,
            infrastructure_patterns(policy),
        )

    asrtst_events = [
        event for event in detector_events if event.get("mnemonic") == "ASRTST"
    ]
    markers = Markers(
        log_exists=log_text is not None,
        event_file_exists=bool(event_path and event_path.is_file()),
        exact_signature_count=count_matches(EXACT_SIGNATURE_RE, log_text or ""),
        any_crc_pass_count=count_matches(ANY_CRC_PASS_RE, log_text or ""),
        exit_success_count=count_matches(EXIT_SUCCESS_RE, log_text or ""),
        timeout_count=count_matches(TIMEOUT_RE, log_text or ""),
        output_failure_count=count_matches(OUTPUT_FAILURE_RE, log_text or ""),
        runner_error_count=sum(
            1 for line in lines if RUNNER_ERROR_RE.search(line)
        ),
        structured_detector_event_count=len(structured_events),
        xcelium_assertion_event_count=len(asrtst_events),
        infrastructure_error_count=len(infrastructure_events),
    )

    status = "UNKNOWN"
    reason = "no_recognized_terminal_outcome"
    exit_code = 3
    tool_status = "OK"
    valid_execution = True
    completion = "UNKNOWN"
    workload_outcome = "UNKNOWN"
    architectural_outcome = "UNKNOWN"
    selected_terminal: dict[str, Any] | None = None

    if phase == "compile":
        completion = "COMPILE_ONLY"
        workload_outcome = architectural_outcome = "NOT_RUN"
        if (
            not markers.log_exists
            or markers.runner_error_count
            or markers.infrastructure_error_count
            or xrun_exit_status != 0
        ):
            status, reason, exit_code = (
                "COMPILE_ERROR",
                "compile_or_elaboration_failed",
                4,
            )
            tool_status, valid_execution = "ERROR", False
        else:
            status, reason, exit_code = (
                "COMPILE_PASS",
                "compile_and_elaboration_completed_without_error",
                0,
            )
    elif not markers.log_exists:
        status, reason, exit_code = "ERROR", "xrun_log_missing", 4
        tool_status, valid_execution = "ERROR", False
    elif event_errors and not markers.runner_error_count:
        status, reason, exit_code = "ERROR", "assertion_event_file_invalid", 4
        tool_status, valid_execution = "ERROR", False
    else:
        detector_line = event_line(detector_events)
        ambiguous_line = event_line(ambiguous_events)
        infrastructure_line = event_line(infrastructure_events)
        timeout_event_line = event_line(timeout_events)
        timeout_marker_line = first_line(TIMEOUT_RE, lines)
        timeout_line = min(
            (
                line
                for line in (timeout_event_line, timeout_marker_line)
                if line is not None
            ),
            default=None,
        )
        timeout_evidence = evidence_at(timeout_events, timeout_event_line)
        unknown_line = event_line(unknown_events)
        exact_line = first_line(EXACT_SIGNATURE_RE, lines)
        exit_line = first_line(EXIT_SUCCESS_RE, lines)
        wrong_crc_line = (
            first_line(ANY_CRC_PASS_RE, lines) if exact_line is None else None
        )
        success_line = (
            exit_line
            if exact_line is not None
            and exit_line is not None
            and xrun_exit_status == 0
            else None
        )
        missing_signature_line = (
            exit_line
            if exit_line is not None
            and xrun_exit_status == 0
            and exact_line is None
            and wrong_crc_line is None
            else None
        )

        selected_terminal = choose_terminal(
            [
                candidate(first_line(RUNNER_ERROR_RE, lines), "RUNNER_ERROR", 0),
                candidate(
                    ambiguous_line,
                    "AMBIGUOUS_REGISTERED_TERMINATION",
                    1,
                    evidence_at(ambiguous_events, ambiguous_line),
                ),
                candidate(
                    infrastructure_line,
                    "INFRASTRUCTURE_FAILURE",
                    2,
                    evidence_at(infrastructure_events, infrastructure_line),
                ),
                candidate(
                    detector_line,
                    "REGISTERED_DETECTOR_TERMINATION",
                    3,
                    evidence_at(detector_events, detector_line),
                ),
                candidate(
                    timeout_line,
                    "TIMEOUT",
                    4,
                    timeout_evidence,
                ),
                candidate(
                    first_line(OUTPUT_FAILURE_RE, lines),
                    "WORKLOAD_FAILURE",
                    5,
                ),
                candidate(success_line, "WORKLOAD_SUCCESS", 6),
                candidate(wrong_crc_line, "WRONG_SIGNATURE", 7),
                candidate(
                    missing_signature_line,
                    "MISSING_REQUIRED_SIGNATURE",
                    8,
                    {
                        "exit_success_observed": True,
                        "exact_crc32_signature_observed": False,
                        "required_output": "CRC32 golden signature",
                    },
                ),
                candidate(
                    unknown_line,
                    "UNKNOWN_RUNTIME_TERMINATION",
                    9,
                    evidence_at(unknown_events, unknown_line),
                ),
            ]
        )
        terminal_kind = selected_terminal["kind"] if selected_terminal else None
        counterfactual = mode != "native" and bool(structured_events)

        if terminal_kind == "RUNNER_ERROR":
            status, reason, exit_code = (
                "ERROR",
                "stage5_runner_invariant_error",
                4,
            )
            tool_status, valid_execution = "ERROR", False
        elif terminal_kind == "AMBIGUOUS_REGISTERED_TERMINATION":
            status, reason, exit_code = (
                "ERROR",
                "ambiguous_registered_termination_signature",
                4,
            )
            tool_status, valid_execution = "ERROR", False
        elif terminal_kind == "INFRASTRUCTURE_FAILURE":
            status, reason, exit_code = (
                "ERROR",
                "xcelium_infrastructure_error_marker",
                4,
            )
            tool_status, valid_execution = "ERROR", False
        elif terminal_kind == "REGISTERED_DETECTOR_TERMINATION":
            completion = "TERMINATED_BY_EXISTING_ASSERTION"
            workload_outcome = "NOT_REACHED"
            architectural_outcome = "CENSORED"
            if mode == "native" and run_kind == "fault":
                status = "EXISTING_ASSERTION_DETECTED"
                reason = (
                    "native_fault_execution_terminated_by_registered_detector"
                )
                exit_code = 2
            elif run_kind == "golden":
                status, reason, exit_code = (
                    "GOLDEN_INVALID",
                    "golden_registered_detector_termination",
                    4,
                )
                valid_execution = False
            else:
                status, reason, exit_code = (
                    "ERROR",
                    "diagnostic_mode_did_not_suppress_registered_termination",
                    4,
                )
                valid_execution = False
        elif terminal_kind == "TIMEOUT":
            completion = "TIMED_OUT"
            workload_outcome = "NOT_REACHED"
            architectural_outcome = (
                "COUNTERFACTUAL_CENSORED" if counterfactual else "CENSORED"
            )
            if run_kind == "golden":
                status, reason, exit_code = (
                    "GOLDEN_INVALID",
                    "golden_execution_timed_out",
                    4,
                )
                valid_execution = False
            elif mode == "native":
                status, reason, exit_code = (
                    "TIMEOUT",
                    "maximum_cycle_limit_reached",
                    2,
                )
            else:
                status, reason, exit_code = (
                    "DIAGNOSTIC_TIMEOUT",
                    "diagnostic_execution_reached_maximum_cycle_limit",
                    2,
                )
        elif terminal_kind in {"WORKLOAD_FAILURE", "WRONG_SIGNATURE"}:
            completion = "COMPLETED"
            workload_outcome = "FAIL"
            architectural_outcome = (
                "COUNTERFACTUAL_FAIL" if counterfactual else "OBSERVED_FAIL"
            )
            if run_kind == "golden":
                status, reason, exit_code = (
                    "GOLDEN_INVALID",
                    "golden_workload_result_failed",
                    4,
                )
                valid_execution = False
            elif mode == "native":
                status, reason, exit_code = (
                    "OUTPUT_MISMATCH",
                    "native_workload_failure",
                    2,
                )
            else:
                status, reason, exit_code = (
                    "DIAGNOSTIC_OUTPUT_MISMATCH",
                    "diagnostic_workload_failure",
                    2,
                )
        elif terminal_kind == "WORKLOAD_SUCCESS":
            completion = "COMPLETED"
            workload_outcome = "PASS"
            architectural_outcome = (
                "COUNTERFACTUAL_PASS" if counterfactual else "OBSERVED_PASS"
            )
            if run_kind == "golden":
                status = "PASS"
            elif mode == "native":
                status = "OUTPUT_MATCH"
            else:
                status = "DIAGNOSTIC_OUTPUT_MATCH"
            reason = (
                "diagnostic_exact_crc32_signature_and_exit_success"
                if mode != "native"
                else "exact_crc32_signature_and_exit_success"
            )
            exit_code = 0
        elif terminal_kind == "MISSING_REQUIRED_SIGNATURE":
            completion = "COMPLETED"
            workload_outcome = "FAIL"
            architectural_outcome = (
                "COUNTERFACTUAL_FAIL" if counterfactual else "OBSERVED_FAIL"
            )
            if run_kind == "golden":
                status, reason, exit_code = (
                    "GOLDEN_INVALID",
                    (
                        "golden_required_crc32_signature_"
                        "missing_on_successful_exit"
                    ),
                    4,
                )
                valid_execution = False
            elif mode == "native":
                status, reason, exit_code = (
                    "OUTPUT_MISMATCH",
                    (
                        "required_crc32_signature_"
                        "missing_on_successful_exit"
                    ),
                    2,
                )
            else:
                status, reason, exit_code = (
                    "DIAGNOSTIC_OUTPUT_MISMATCH",
                    (
                        "diagnostic_required_crc32_signature_"
                        "missing_on_successful_exit"
                    ),
                    2,
                )
        elif terminal_kind == "UNKNOWN_RUNTIME_TERMINATION":
            status, reason, exit_code = (
                "ERROR",
                "unknown_runtime_termination_signature",
                4,
            )
            tool_status, valid_execution = "ERROR", False
        elif xrun_exit_status != 0:
            status, reason, exit_code = (
                "ERROR",
                "xrun_nonzero_without_recognized_terminal_event",
                4,
            )
            tool_status, valid_execution = "ERROR", False
        elif markers.exit_success_count:
            status, reason, exit_code = (
                "UNKNOWN",
                "exit_success_without_exact_crc32_signature",
                3,
            )
            valid_execution = False
        else:
            valid_execution = False

    selected_line = selected_terminal["log_line"] if selected_terminal else None
    trailing_tool_events = [
        event
        for event in tool_events
        if selected_line is not None and event["log_line"] > selected_line
    ]
    trailing_fatal_events = [
        event
        for event in fatal_trailers
        if selected_line is not None and event["log_line"] > selected_line
    ]

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
            "unknown_runtime_events": unknown_events,
            "ambiguous_registered_events": ambiguous_events,
            "assertion_event_file_errors": event_errors,
        },
        "execution": {
            "run_purpose": run_purpose,
            "assertion_mode": mode,
            "valid_experiment_execution": valid_execution,
            "completion": completion,
            "native_execution": mode == "native",
            "diagnostic_execution": mode != "native",
            "continuation_after_detector_event": (
                bool(structured_events)
                and completion != "TERMINATED_BY_EXISTING_ASSERTION"
            ),
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
            "triggered": bool(structured_events or detector_events),
            "structured_event_count": len(structured_events),
            "xcelium_assertion_event_count": len(asrtst_events),
            "events": structured_events,
            "xcelium_events": detector_events,
        },
        "signature_resolution": {
            "policy_path": str(policy_path),
            "policy_sha256": sha256_file(policy_path),
            "policy_version": policy.get("policy_version"),
            "signature_policy_version": policy["signature_policy"].get(
                "policy_version"
            ),
            "selected_terminal": selected_terminal,
            "registered_detector_events": detector_events,
            "ambiguous_registered_events": ambiguous_events,
            "watchdog_timeout_events": timeout_events,
            "unknown_runtime_events": unknown_events,
            "trailing_tool_events_after_selected_terminal": trailing_tool_events,
            "trailing_fatal_events_after_selected_terminal": trailing_fatal_events,
        },
        "intervention": intervention,
    }

    return Verdict(
        schema_version=SCHEMA_VERSION,
        verdict_engine_version=PROGRAM_VERSION,
        policy_version=str(policy.get("policy_version")),
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
            "earliest_terminal_event_controls_classification": True,
            "trailing_fatal_cannot_override_selected_terminal": True,
            "unknown_and_ambiguous_terminations_fail_closed": True,
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
    parser.add_argument(
        "--run-purpose", choices=sorted(RUN_PURPOSE_TO_MODE), required=True
    )
    parser.add_argument("--xrun-status", type=int, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--assert-events", type=Path)
    parser.add_argument("--policy", type=Path, default=default_policy_path())
    parser.add_argument("--result-json", type=Path, required=True)
    parser.add_argument("--result-text", type=Path, required=True)
    parser.add_argument("--result-env", type=Path, required=True)
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {PROGRAM_VERSION}"
    )
    args = parser.parse_args(argv)

    log_path = args.log.resolve()
    log_text = (
        log_path.read_text(encoding="utf-8", errors="replace")
        if log_path.is_file()
        else None
    )
    event_path = args.assert_events.resolve() if args.assert_events else None
    policy_path = args.policy.resolve()

    try:
        policy = load_json(policy_path, "assertion policy")
        verdict = compute_verdict(
            phase=args.phase,
            run_kind=args.run_kind,
            run_purpose=args.run_purpose,
            xrun_exit_status=args.xrun_status,
            log_text=log_text,
            event_path=event_path,
            policy=policy,
            policy_path=policy_path,
        )
    except VerdictError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 4

    payload = asdict(verdict)
    atomic_write(args.result_json, json.dumps(payload, indent=2) + "\n")
    atomic_write(args.result_text, verdict.status + "\n")

    raw = verdict.raw_facts
    terminal = raw["signature_resolution"].get("selected_terminal") or {}
    evidence = terminal.get("evidence") or {}
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
                "architectural_outcome="
                f"{raw['workload']['architectural_outcome']}",
                "existing_detector_count="
                f"{raw['existing_detector_baseline']['structured_event_count']}",
                f"terminal_kind={terminal.get('kind', '')}",
                f"terminal_log_line={terminal.get('log_line', '')}",
                f"terminal_signature_id={evidence.get('signature_id', '')}",
                f"terminal_detector_id={evidence.get('detector_id', '')}",
                "",
            ]
        ),
    )
    print(verdict.status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
