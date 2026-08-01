#!/usr/bin/env python3
"""Limited-enumeration Stage-5 termination-signature classifier.

This tool does not infer arbitrary simulator behavior.  It enumerates a small,
reviewed whitelist of terminal signatures from the assertion policy, deduplicates
normalized signatures, and fails closed for unknown or ambiguous events.

It can also repair a previously misclassified Native result.json using the
existing xrun.log without rerunning Xcelium.  The original result files should be
archived by the caller before repair.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

PROGRAM_VERSION = "1.0.0"
SCHEMA_VERSION = "1.0"

TOOL_DIAGNOSTIC_RE = re.compile(
    r"^(?P<tool>xmvlog|xmelab|xmsim|xrun):\s*"
    r"\*(?P<severity>[EF]),(?P<mnemonic>[A-Za-z0-9_]+):?\s*"
    r"(?P<body>.*)$",
    re.IGNORECASE,
)
LOCATION_RE = re.compile(
    r"\((?P<source_file>[^(),]+(?:/|\\)[^(),]+),(?P<source_line>\d+)\)"
)
TIME_RE = re.compile(
    r"\(time\s+(?P<value>[0-9]+(?:\.[0-9]+)?)\s+(?P<unit>[A-Za-z]+)\)",
    re.IGNORECASE,
)
ASRTST_LEAF_RE = re.compile(
    r"Assertion\s+(?P<name>\S+)\s+has\s+failed",
    re.IGNORECASE,
)
TIMEOUT_RE = re.compile(
    r"Simulation aborted due to maximum cycle limit|maximum cycle limit|"
    r"\bMAXCYCLES\b.*(?:reached|exceeded)",
    re.IGNORECASE,
)
EXACT_SIGNATURE_RE = re.compile(
    r"CRC32\s+PASS:\s*vector=(?:0x)?cbf43926\s+"
    r"signature=(?:0x)?2d6352b3\s+last=(?:0x)?5650ac83\b",
    re.IGNORECASE,
)
ANY_CRC_PASS_RE = re.compile(r"CRC32\s+PASS:", re.IGNORECASE)
EXIT_SUCCESS_RE = re.compile(r"\bEXIT\s+SUCCESS\b", re.IGNORECASE)
OUTPUT_FAILURE_RE = re.compile(
    r"CRC32\s+FAIL|\bEXIT\s+FAILURE\b|TEST\(S\)\s+FAILED",
    re.IGNORECASE,
)
RUNNER_ERROR_RE = re.compile(r"^F2A_RUNNER_ERROR:", re.IGNORECASE)
NUMBER_RE = re.compile(r"\b(?:0x[0-9a-f]+|\d+)\b", re.IGNORECASE)
ABSOLUTE_PATH_RE = re.compile(r"/(?:[^\s():]+/)+[^\s():,]+")


class SignatureError(RuntimeError):
    """Controlled signature-classification error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SignatureError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SignatureError(f"invalid {label} JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SignatureError(f"{label} must contain one JSON object: {path}")
    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def normalize_path(value: str | None) -> str | None:
    if not value:
        return None
    return value.replace("\\", "/")


def source_is_user_code(source_file: str | None) -> bool:
    source = (normalize_path(source_file) or "").lower()
    return any(token in source for token in ("/verification/", "/tb/", "/platform/"))


def normalize_signature(text: str) -> str:
    value = text.strip().lower().replace("\\", "/")
    value = ABSOLUTE_PATH_RE.sub("<path>", value)
    value = TIME_RE.sub("(time <n> <unit>)", value)
    value = NUMBER_RE.sub("<n>", value)
    value = re.sub(r"\s+", " ", value)
    return value


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def detector_records(policy: Mapping[str, Any]) -> list[dict[str, Any]]:
    detectors = policy.get("detectors")
    if not isinstance(detectors, list) or not detectors:
        raise SignatureError("assertion policy has no detectors array")
    result = [dict(item) for item in detectors if isinstance(item, dict)]
    if len(result) != len(detectors):
        raise SignatureError("assertion policy contains a non-object detector")
    return result


def true_infrastructure_patterns(policy: Mapping[str, Any]) -> list[re.Pattern[str]]:
    record = policy.get("signature_policy")
    if not isinstance(record, dict):
        raise SignatureError("assertion policy has no signature_policy object")
    raw = record.get("true_infrastructure_message_regexes")
    if not isinstance(raw, list):
        raise SignatureError("signature policy has no infrastructure regex list")
    patterns: list[re.Pattern[str]] = []
    for item in raw:
        if not isinstance(item, str) or not item:
            raise SignatureError("invalid infrastructure regex")
        patterns.append(re.compile(item, re.IGNORECASE))
    return patterns


def parse_diagnostics(lines: Sequence[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    timeout_indexes = {
        index for index, line in enumerate(lines) if TIMEOUT_RE.search(line)
    }
    for index, line in enumerate(lines):
        match = TOOL_DIAGNOSTIC_RE.match(line)
        if match is None:
            continue
        body = match.group("body")
        location = LOCATION_RE.search(body)
        timing = TIME_RE.search(body)
        assertion = ASRTST_LEAF_RE.search(body)
        event = {
            "log_line": index + 1,
            "tool": match.group("tool").lower(),
            "severity": match.group("severity").upper(),
            "mnemonic": match.group("mnemonic").upper(),
            "message": line,
            "normalized_signature": normalize_signature(line),
            "source_file": normalize_path(location.group("source_file")) if location else None,
            "source_line": int(location.group("source_line")) if location else None,
            "simulation_time": (
                {
                    "value": timing.group("value"),
                    "unit": timing.group("unit").upper(),
                }
                if timing
                else None
            ),
            "assertion_name": assertion.group("name") if assertion else None,
            "assertion_leaf_name": (
                assertion.group("name").rsplit(".", 1)[-1] if assertion else None
            ),
            "adjacent_to_timeout": any(
                abs(index - timeout_index) <= 3 for timeout_index in timeout_indexes
            ),
        }
        events.append(event)
    return events


def signature_matches(
    event: Mapping[str, Any],
    detector: Mapping[str, Any],
    signature: Mapping[str, Any],
) -> bool:
    if str(signature.get("phase", "run")) != "run":
        return False
    if str(event.get("tool", "")).lower() != str(signature.get("tool", "")).lower():
        return False
    if str(event.get("mnemonic", "")).upper() != str(signature.get("mnemonic", "")).upper():
        return False

    source_suffix = normalize_path(str(detector.get("source_suffix", ""))) or ""
    event_source = normalize_path(str(event.get("source_file") or "")) or ""
    if signature.get("require_source_suffix") is True:
        if not source_suffix or not event_source.endswith(source_suffix):
            return False

    if signature.get("require_assertion_leaf_name") is True:
        expected = str(detector.get("assertion_leaf_name", ""))
        actual = str(event.get("assertion_leaf_name") or "")
        if not expected or actual != expected:
            return False

    message_regex = signature.get("message_regex")
    if message_regex is not None:
        if not isinstance(message_regex, str) or not re.search(
            message_regex, str(event.get("message", "")), re.IGNORECASE
        ):
            return False
    return True


def resolve_registered_event(
    event: Mapping[str, Any], detectors: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for detector in detectors:
        signatures = detector.get("termination_signatures")
        if not isinstance(signatures, list):
            continue
        for signature in signatures:
            if not isinstance(signature, dict):
                continue
            if signature_matches(event, detector, signature):
                matches.append(
                    {
                        "detector_id": detector.get("detector_id"),
                        "detector_origin": detector.get("origin"),
                        "assertion_leaf_name": detector.get("assertion_leaf_name"),
                        "effect_hint": detector.get("effect_hint"),
                        "quarantine_action": detector.get("quarantine_action"),
                        "signature_id": signature.get("signature_id"),
                        "terminal_kind": signature.get("terminal_kind"),
                        "event": dict(event),
                    }
                )
    return matches


def classification_record(
    *,
    semantic_class: str,
    status: str,
    reason: str,
    recommended_exit_code: int,
    event: Mapping[str, Any] | None,
    detector_match: Mapping[str, Any] | None,
    all_events: Sequence[Mapping[str, Any]],
    log_path: Path,
    policy_path: Path,
) -> dict[str, Any]:
    tool = event.get("tool") if event else None
    mnemonic = event.get("mnemonic") if event else None
    detector_id = detector_match.get("detector_id") if detector_match else None
    normalized = event.get("normalized_signature") if event else semantic_class.lower()
    dedupe_source = "|".join(
        str(item or "")
        for item in (semantic_class, tool, mnemonic, detector_id, normalized)
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "generated_at_utc": utc_now(),
        "kind": "stage5_limited_signature_classification",
        "policy": str(policy_path.resolve()),
        "log": str(log_path.resolve()),
        "semantic_class": semantic_class,
        "status": status,
        "reason": reason,
        "recommended_exit_code": recommended_exit_code,
        "fail_closed": semantic_class in {
            "UNREGISTERED_USER_FATAL",
            "AMBIGUOUS_REGISTERED_SIGNATURE",
            "TOOL_INFRASTRUCTURE_FAILURE",
            "UNKNOWN_TERMINATION",
        },
        "detector_match": dict(detector_match) if detector_match else None,
        "terminal_event": dict(event) if event else None,
        "diagnostic_events": [dict(item) for item in all_events],
        "normalized_signature": normalized,
        "dedupe_key": sha256_text(dedupe_source),
    }


def classify_log(log_path: Path, policy_path: Path) -> dict[str, Any]:
    log_path = log_path.expanduser().resolve()
    policy_path = policy_path.expanduser().resolve()
    if not log_path.is_file():
        raise SignatureError(f"xrun log not found: {log_path}")
    policy = load_json(policy_path, "assertion policy")
    detectors = detector_records(policy)
    infrastructure_patterns = true_infrastructure_patterns(policy)
    text = log_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    events = parse_diagnostics(lines)

    if any(RUNNER_ERROR_RE.search(line) for line in lines):
        event = next(
            (
                {
                    "tool": "runner",
                    "mnemonic": "F2A_RUNNER_ERROR",
                    "message": line,
                    "normalized_signature": normalize_signature(line),
                    "log_line": index + 1,
                }
                for index, line in enumerate(lines)
                if RUNNER_ERROR_RE.search(line)
            ),
            None,
        )
        return classification_record(
            semantic_class="TOOL_INFRASTRUCTURE_FAILURE",
            status="ERROR",
            reason="stage5_runner_invariant_error",
            recommended_exit_code=4,
            event=event,
            detector_match=None,
            all_events=events,
            log_path=log_path,
            policy_path=policy_path,
        )

    timeout_line = next(
        (index + 1 for index, line in enumerate(lines) if TIMEOUT_RE.search(line)),
        None,
    )
    if timeout_line is not None:
        event = {
            "tool": "testbench",
            "mnemonic": "MAXCYCLES",
            "message": lines[timeout_line - 1],
            "normalized_signature": normalize_signature(lines[timeout_line - 1]),
            "log_line": timeout_line,
        }
        return classification_record(
            semantic_class="WATCHDOG_TIMEOUT",
            status="TIMEOUT",
            reason="maximum_cycle_limit_reached",
            recommended_exit_code=2,
            event=event,
            detector_match=None,
            all_events=events,
            log_path=log_path,
            policy_path=policy_path,
        )

    for event in events:
        if event.get("adjacent_to_timeout"):
            continue
        message = str(event.get("message", ""))
        if any(pattern.search(message) for pattern in infrastructure_patterns):
            return classification_record(
                semantic_class="TOOL_INFRASTRUCTURE_FAILURE",
                status="ERROR",
                reason="explicit_tool_infrastructure_signature",
                recommended_exit_code=4,
                event=event,
                detector_match=None,
                all_events=events,
                log_path=log_path,
                policy_path=policy_path,
            )

        matches = resolve_registered_event(event, detectors)
        if len(matches) == 1:
            return classification_record(
                semantic_class="REGISTERED_DETECTOR_TERMINATION",
                status="EXISTING_ASSERTION_DETECTED",
                reason="native_fault_execution_terminated_by_registered_detector",
                recommended_exit_code=2,
                event=event,
                detector_match=matches[0],
                all_events=events,
                log_path=log_path,
                policy_path=policy_path,
            )
        if len(matches) > 1:
            return classification_record(
                semantic_class="AMBIGUOUS_REGISTERED_SIGNATURE",
                status="ERROR",
                reason="runtime_signature_matches_multiple_registered_detectors",
                recommended_exit_code=4,
                event=event,
                detector_match={"matches": matches},
                all_events=events,
                log_path=log_path,
                policy_path=policy_path,
            )

        if event.get("tool") == "xmsim" and source_is_user_code(
            str(event.get("source_file") or "")
        ):
            return classification_record(
                semantic_class="UNREGISTERED_USER_FATAL",
                status="ERROR",
                reason="unregistered_user_runtime_termination",
                recommended_exit_code=4,
                event=event,
                detector_match=None,
                all_events=events,
                log_path=log_path,
                policy_path=policy_path,
            )

        return classification_record(
            semantic_class="TOOL_INFRASTRUCTURE_FAILURE",
            status="ERROR",
            reason="unmatched_xcelium_error_or_fatal",
            recommended_exit_code=4,
            event=event,
            detector_match=None,
            all_events=events,
            log_path=log_path,
            policy_path=policy_path,
        )

    failure_line = next(
        (index + 1 for index, line in enumerate(lines) if OUTPUT_FAILURE_RE.search(line)),
        None,
    )
    if failure_line is not None:
        event = {
            "tool": "workload",
            "mnemonic": "OUTPUT_FAILURE",
            "message": lines[failure_line - 1],
            "normalized_signature": normalize_signature(lines[failure_line - 1]),
            "log_line": failure_line,
        }
        return classification_record(
            semantic_class="WORKLOAD_FAILURE",
            status="OUTPUT_MISMATCH",
            reason="workload_reported_failure",
            recommended_exit_code=2,
            event=event,
            detector_match=None,
            all_events=events,
            log_path=log_path,
            policy_path=policy_path,
        )

    exact = bool(EXACT_SIGNATURE_RE.search(text))
    exit_success = bool(EXIT_SUCCESS_RE.search(text))
    if exact and exit_success:
        event = {
            "tool": "workload",
            "mnemonic": "OUTPUT_MATCH",
            "message": "CRC32 golden signature and EXIT SUCCESS",
            "normalized_signature": "crc32 golden signature and exit success",
            "log_line": None,
        }
        return classification_record(
            semantic_class="WORKLOAD_SUCCESS",
            status="OUTPUT_MATCH",
            reason="workload_completed_with_exact_golden_signature",
            recommended_exit_code=0,
            event=event,
            detector_match=None,
            all_events=events,
            log_path=log_path,
            policy_path=policy_path,
        )

    if ANY_CRC_PASS_RE.search(text) and not exact:
        event = {
            "tool": "workload",
            "mnemonic": "WRONG_SIGNATURE",
            "message": "CRC32 PASS marker with non-golden signature",
            "normalized_signature": "crc32 pass marker with non-golden signature",
            "log_line": None,
        }
        return classification_record(
            semantic_class="WORKLOAD_FAILURE",
            status="OUTPUT_MISMATCH",
            reason="workload_completed_with_wrong_signature",
            recommended_exit_code=2,
            event=event,
            detector_match=None,
            all_events=events,
            log_path=log_path,
            policy_path=policy_path,
        )

    return classification_record(
        semantic_class="UNKNOWN_TERMINATION",
        status="ERROR",
        reason="no_whitelisted_terminal_signature",
        recommended_exit_code=4,
        event=None,
        detector_match=None,
        all_events=events,
        log_path=log_path,
        policy_path=policy_path,
    )


def ensure_mapping(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        value = {}
        parent[key] = value
    return value


def repaired_result(
    original: Mapping[str, Any], classification: Mapping[str, Any]
) -> dict[str, Any]:
    if classification.get("semantic_class") != "REGISTERED_DETECTOR_TERMINATION":
        raise SignatureError("only a registered detector termination may repair a result")
    detector = classification.get("detector_match")
    if not isinstance(detector, dict):
        raise SignatureError("registered classification has no detector match")

    result = copy.deepcopy(dict(original))
    result["status"] = "EXISTING_ASSERTION_DETECTED"
    result["reason"] = "native_fault_execution_terminated_by_registered_detector"
    result["recommended_exit_code"] = 2
    result["signature_classifier_version"] = PROGRAM_VERSION
    result["signature_classification"] = dict(classification)

    raw = ensure_mapping(result, "raw_facts")
    tool = ensure_mapping(raw, "tool")
    tool["status"] = "OK"
    tool["infrastructure_error_count"] = 0
    tool["infrastructure_events"] = []
    tool["reclassified_runtime_diagnostics"] = classification.get(
        "diagnostic_events", []
    )

    execution = ensure_mapping(raw, "execution")
    execution["completion"] = "TERMINATED_BY_EXISTING_ASSERTION"
    execution["valid_execution"] = True
    execution.setdefault("run_purpose", "NATIVE_CHARACTERIZATION")
    execution.setdefault("assertion_mode", "native")

    workload = ensure_mapping(raw, "workload")
    workload["outcome"] = "NOT_REACHED"
    workload["architectural_outcome"] = "CENSORED"

    detector_record = {
        "detector_id": detector.get("detector_id"),
        "detector_origin": detector.get("detector_origin"),
        "assertion_leaf_name": detector.get("assertion_leaf_name"),
        "detector_reported_effect_hint": detector.get("effect_hint"),
        "effect_hint": detector.get("effect_hint"),
        "quarantine_action": detector.get("quarantine_action"),
        "action": "FATAL_TERMINATION",
        "source": "WHITELISTED_TERMINATION_SIGNATURE",
        "signature_id": detector.get("signature_id"),
        "runtime_event": detector.get("event"),
    }
    raw["preexisting_detectors"] = [detector_record]
    raw["detectors"] = [detector_record]
    baseline = ensure_mapping(raw, "existing_detector_baseline")
    baseline["detected"] = True
    baseline["events"] = [detector_record]
    baseline["xcelium_events"] = [detector_record]
    raw["signature_resolution"] = dict(classification)

    intervention = ensure_mapping(raw, "intervention")
    intervention.setdefault("termination_suppressed", False)
    intervention.setdefault("transaction_quarantine", False)
    intervention.setdefault("counterfactual_after_first_detector_event", False)

    result.setdefault("run_purpose", "NATIVE_CHARACTERIZATION")
    result.setdefault("assertion_mode", "native")

    markers = result.get("markers")
    if isinstance(markers, dict):
        markers["infrastructure_error_count"] = 0
        markers["xcelium_assertion_event_count"] = max(
            1, int(markers.get("xcelium_assertion_event_count", 0) or 0)
        )
    return result


def write_env(path: Path, result: Mapping[str, Any]) -> None:
    values = {
        "result": result.get("status", "ERROR"),
        "reason": result.get("reason", "unknown"),
        "recommended_exit_code": result.get("recommended_exit_code", 4),
    }
    lines = [f"{key}={value}" for key, value in values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_text_result(path: Path, result: Mapping[str, Any]) -> None:
    lines = [
        f"status={result.get('status')}",
        f"reason={result.get('reason')}",
        f"recommended_exit_code={result.get('recommended_exit_code')}",
        "classification=WHITELISTED_REGISTERED_DETECTOR_TERMINATION",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def command_classify(args: argparse.Namespace) -> int:
    report = classify_log(args.log, args.policy)
    write_json(args.output, report)
    print("Stage5 termination-signature classification")
    print("===========================================")
    print(f"Semantic class : {report['semantic_class']}")
    print(f"Status         : {report['status']}")
    print(f"Fail closed    : {report['fail_closed']}")
    print(f"Dedupe key     : {report['dedupe_key']}")
    detector = report.get("detector_match")
    print(
        "Detector       : "
        + (str(detector.get("detector_id")) if isinstance(detector, dict) else "NONE")
    )
    print(f"Report         : {args.output.resolve()}")
    return 0 if report["fail_closed"] is False else 2


def command_repair(args: argparse.Namespace) -> int:
    original = load_json(args.result_json, "Stage5 result")
    classification = load_json(args.classification, "signature classification")
    repaired = repaired_result(original, classification)
    write_json(args.result_json, repaired)
    write_env(args.result_env, repaired)
    write_text_result(args.result_text, repaired)
    print("Stage5 Native result reclassification: PASS")
    print(f"Result JSON : {args.result_json.resolve()}")
    print(f"New status  : {repaired['status']}")
    detector = classification["detector_match"]
    print(f"Detector    : {detector['detector_id']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--policy",
        type=Path,
        default=root / "platform/cv32e40p/stage5_assertion_policy_v1.json",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    classify = sub.add_parser("classify", help="classify one saved xrun.log")
    classify.add_argument("--log", type=Path, required=True)
    classify.add_argument("--output", type=Path, required=True)
    classify.set_defaults(func=command_classify)

    repair = sub.add_parser(
        "repair-result",
        help="repair a misclassified Native result using an approved classification",
    )
    repair.add_argument("--result-json", type=Path, required=True)
    repair.add_argument("--result-env", type=Path, required=True)
    repair.add_argument("--result-text", type=Path, required=True)
    repair.add_argument("--classification", type=Path, required=True)
    repair.set_defaults(func=command_repair)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SignatureError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
