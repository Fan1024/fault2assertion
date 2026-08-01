#!/usr/bin/env python3
"""Build one generic Stage-5 batch oracle from validated mode results.

This program never runs Xcelium and never generates SVA. It supports:

* Native-only outcomes: OUTPUT_MATCH, OUTPUT_MISMATCH, TIMEOUT;
* assertion-censored outcomes with OBSERVE and DIAGNOSTIC_QUARANTINE;
* registry-selected detector metadata;
* private exact labels separated from a redacted prompt context.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "1.0"
PROGRAM_VERSION = "1.0.0"

NATIVE_STATUSES = {
    "OUTPUT_MATCH",
    "OUTPUT_MISMATCH",
    "TIMEOUT",
    "EXISTING_ASSERTION_DETECTED",
}
DIAGNOSTIC_STATUSES = {
    "DIAGNOSTIC_OUTPUT_MATCH",
    "DIAGNOSTIC_OUTPUT_MISMATCH",
    "DIAGNOSTIC_TIMEOUT",
}
COMPLETED_DIAGNOSTIC = {
    "DIAGNOSTIC_OUTPUT_MATCH",
    "DIAGNOSTIC_OUTPUT_MISMATCH",
}


class OracleError(RuntimeError):
    """Controlled oracle construction failure."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OracleError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise OracleError(f"invalid {label} JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise OracleError(f"{label} must contain one JSON object: {path}")
    return value


def require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OracleError(f"{label} must be an object")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path = path.expanduser().resolve()
    if path.exists():
        raise OracleError(f"refusing to overwrite existing output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def first_structured_event(result: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = result.get("raw_facts")
    if not isinstance(raw, dict):
        return None
    detector = raw.get("existing_detector_baseline")
    if not isinstance(detector, dict):
        return None
    for key in ("events", "xcelium_events"):
        events = detector.get(key)
        if isinstance(events, list) and events and isinstance(events[0], dict):
            return dict(events[0])
    return None


def run_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    raw = require_mapping(result.get("raw_facts"), "result.raw_facts")
    execution = require_mapping(raw.get("execution"), "raw execution")
    workload = require_mapping(raw.get("workload"), "raw workload")
    intervention = require_mapping(raw.get("intervention"), "raw intervention")
    return {
        "runner_status": result.get("status"),
        "completion": execution.get("completion"),
        "workload_outcome": workload.get("outcome"),
        "architectural_outcome": workload.get("architectural_outcome"),
        "run_purpose": result.get("run_purpose"),
        "assertion_mode": result.get("assertion_mode"),
        "termination_suppressed": intervention.get("termination_suppressed"),
        "transaction_quarantine": intervention.get("transaction_quarantine"),
        "counterfactual_after_first_detector_event": intervention.get(
            "counterfactual_after_first_detector_event"
        ),
        "first_detector_event": first_structured_event(result),
    }


def classify_native(status: str) -> dict[str, Any]:
    table = {
        "OUTPUT_MATCH": (
            "NATIVE_COMPLETES",
            "NATIVE_REACHED_CORRECT_WORKLOAD_COMPLETION",
        ),
        "OUTPUT_MISMATCH": (
            "NATIVE_COMPLETES_WITH_OUTPUT_CORRUPTION",
            "NATIVE_REACHED_INCORRECT_WORKLOAD_COMPLETION",
        ),
        "TIMEOUT": (
            "NATIVE_TIMEOUT",
            "NATIVE_REACHED_MAXIMUM_CYCLE_LIMIT",
        ),
    }
    capability, basis = table[status]
    return {
        "validated_capability": capability,
        "scope": "NATURAL_EXECUTION",
        "basis": basis,
        "observe_continuation_restored": None,
        "diagnostic_quarantine_continuation_restored": None,
    }


def classify_diagnostic(observe: str, quarantine: str) -> dict[str, Any]:
    if observe == "DIAGNOSTIC_OUTPUT_MATCH":
        capability = "OBSERVE_CONTINUABLE"
        basis = "OBSERVE_REACHED_CORRECT_WORKLOAD_COMPLETION"
    elif observe == "DIAGNOSTIC_OUTPUT_MISMATCH":
        capability = "OBSERVE_CONTINUABLE_WITH_OUTPUT_CORRUPTION"
        basis = "OBSERVE_REACHED_INCORRECT_WORKLOAD_COMPLETION"
    elif quarantine == "DIAGNOSTIC_OUTPUT_MATCH":
        capability = "QUARANTINE_REQUIRED"
        basis = "OBSERVE_TIMED_OUT_AND_QUARANTINE_REACHED_CORRECT_COMPLETION"
    elif quarantine == "DIAGNOSTIC_OUTPUT_MISMATCH":
        capability = "QUARANTINE_REQUIRED_WITH_OUTPUT_CORRUPTION"
        basis = "OBSERVE_TIMED_OUT_AND_QUARANTINE_REACHED_INCORRECT_COMPLETION"
    else:
        capability = "NON_CONTINUABLE"
        basis = "OBSERVE_AND_DIAGNOSTIC_QUARANTINE_TIMED_OUT"
    return {
        "validated_capability": capability,
        "scope": "CURRENT_REGISTERED_QUARANTINE_POLICY",
        "basis": basis,
        "observe_continuation_restored": observe in COMPLETED_DIAGNOSTIC,
        "diagnostic_quarantine_continuation_restored": (
            quarantine in COMPLETED_DIAGNOSTIC
        ),
    }


def exact_fault_label(spec: Mapping[str, Any]) -> dict[str, Any]:
    site = require_mapping(spec.get("site"), "fault site")
    module = site.get("module")
    source_net = site.get("source_net")
    if not isinstance(module, str) or not module:
        raise OracleError("fault site.module is missing")
    if not isinstance(source_net, str) or not source_net:
        raise OracleError("fault site.source_net is missing")
    return {
        "label_kind": "FAULT_INJECTION_SIGNAL",
        "base_fault_id": spec.get("base_fault_id"),
        "module": module,
        "source_net": source_net,
        "fault_class": spec.get("fault_class"),
        "polarity": spec.get("polarity"),
        "stuck_at": spec.get("stuck_at"),
        "receiver_class": site.get("receiver_class"),
        "receivers": site.get("receivers", []),
        "receiver_signals": site.get("receiver_signals", []),
    }


def detector_boundaries(
    native: Mapping[str, Any],
    observe: Mapping[str, Any] | None,
    quarantine: Mapping[str, Any] | None,
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "native": first_structured_event(native),
        "observe": first_structured_event(observe) if observe else None,
        "diagnostic_quarantine": (
            first_structured_event(quarantine) if quarantine else None
        ),
        "earliest_local_divergence_status": "NOT_COMPUTED_IN_BATCH_ORACLE",
        "boundary_interpretation": (
            "First observable registered detector event; not the earliest local "
            "signal divergence."
        ),
    }
    return values


def detector_record(
    registry: Mapping[str, Any], routing: Mapping[str, Any]
) -> dict[str, Any] | None:
    detector_id = routing.get("detector_id")
    if detector_id is None:
        return None
    detectors = registry.get("detectors")
    if not isinstance(detectors, list):
        raise OracleError("assertion registry has no detectors array")
    matches = [
        item
        for item in detectors
        if isinstance(item, dict) and item.get("detector_id") == detector_id
    ]
    if len(matches) != 1:
        raise OracleError(
            f"routing detector is not uniquely registered: {detector_id}"
        )
    return dict(matches[0])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fault-json", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--routing", type=Path, required=True)
    parser.add_argument("--native-run", type=Path, required=True)
    parser.add_argument("--observe-run", type=Path)
    parser.add_argument("--quarantine-run", type=Path)
    parser.add_argument("--oracle", type=Path, required=True)
    parser.add_argument("--prompt-context", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        fault_path = args.fault_json.resolve()
        registry_path = args.registry.resolve()
        routing_path = args.routing.resolve()
        native_result_path = args.native_run.resolve() / "result.json"

        spec = load_json(fault_path, "fault spec")
        registry = load_json(registry_path, "assertion registry")
        routing = load_json(routing_path, "routing record")
        native = load_json(native_result_path, "Native result")

        fault_id = str(spec.get("fault_id", ""))
        if not fault_id:
            raise OracleError("fault spec fault_id is missing")
        if routing.get("fault_id") != fault_id:
            raise OracleError("routing/fault fault_id mismatch")
        if native.get("status") not in NATIVE_STATUSES:
            raise OracleError(f"unsupported Native status: {native.get('status')}")
        if native.get("run_purpose") != "NATIVE_CHARACTERIZATION":
            raise OracleError("Native run purpose is not NATIVE_CHARACTERIZATION")

        native_status = str(native["status"])
        observe: dict[str, Any] | None = None
        quarantine: dict[str, Any] | None = None
        observe_path: Path | None = None
        quarantine_path: Path | None = None

        if native_status == "EXISTING_ASSERTION_DETECTED":
            if args.observe_run is None or args.quarantine_run is None:
                raise OracleError(
                    "assertion-censored Native result requires both diagnostic runs"
                )
            observe_path = args.observe_run.resolve() / "result.json"
            quarantine_path = args.quarantine_run.resolve() / "result.json"
            observe = load_json(observe_path, "OBSERVE result")
            quarantine = load_json(
                quarantine_path, "DIAGNOSTIC_QUARANTINE result"
            )
            if observe.get("status") not in DIAGNOSTIC_STATUSES:
                raise OracleError(
                    f"unsupported OBSERVE status: {observe.get('status')}"
                )
            if quarantine.get("status") not in DIAGNOSTIC_STATUSES:
                raise OracleError(
                    "unsupported DIAGNOSTIC_QUARANTINE status: "
                    f"{quarantine.get('status')}"
                )
            if observe.get("run_purpose") != "DIAGNOSTIC_OBSERVE":
                raise OracleError("OBSERVE run purpose mismatch")
            if quarantine.get("run_purpose") != "DIAGNOSTIC_QUARANTINE":
                raise OracleError("DIAGNOSTIC_QUARANTINE run purpose mismatch")
            if routing.get("route") != "DIAGNOSTIC_THREE_MODE":
                raise OracleError("assertion-censored result has wrong routing")
            capability = classify_diagnostic(
                str(observe["status"]), str(quarantine["status"])
            )
        else:
            if routing.get("route") != "NATIVE_ONLY":
                raise OracleError("Native-only result has wrong routing")
            capability = classify_native(native_status)

        detector = detector_record(registry, routing)
        raw_facts = {
            "native": native["raw_facts"],
            "observe": observe["raw_facts"] if observe else None,
            "diagnostic_quarantine": (
                quarantine["raw_facts"] if quarantine else None
            ),
        }

        natural = run_summary(native)
        natural["natural_execution"] = True
        natural["natural_outcome_censored"] = (
            natural.get("architectural_outcome") == "CENSORED"
        )

        derived = {
            "natural_execution": natural,
            "observe_execution": run_summary(observe) if observe else None,
            "diagnostic_quarantine_execution": (
                run_summary(quarantine) if quarantine else None
            ),
            "detection": {
                "detected_by_existing_detector": (
                    native_status == "EXISTING_ASSERTION_DETECTED"
                ),
                "detector_id": detector.get("detector_id") if detector else None,
                "detector_origin": detector.get("origin") if detector else None,
                "detector_leaf_name": (
                    detector.get("assertion_leaf_name") if detector else None
                ),
                "detector_reported_effect_hint": (
                    detector.get("effect_hint") if detector else None
                ),
                "effect_hint_status": (
                    "HINT_ONLY_NOT_INDEPENDENTLY_CONFIRMED"
                    if detector
                    else "NOT_APPLICABLE"
                ),
            },
            "continuation_capability": capability,
            "root_cause": {
                "status": "UNRESOLVED",
                "unique_root_cause_proven": False,
                "note": (
                    "Outcome classification is supported by simulation; the "
                    "unique physical root cause is not inferred by this oracle."
                ),
            },
            "confidence": {
                "raw_facts": "HIGH",
                "natural_outcome": "HIGH",
                "detector_identity": "HIGH" if detector else "NOT_APPLICABLE",
                "continuation_result": (
                    "HIGH" if observe and quarantine else "NOT_APPLICABLE"
                ),
                "root_cause": "LOW",
            },
        }

        private_ground_truth = {
            "exact_fault_injection_signal": exact_fault_label(spec),
            "first_observable_detector_boundaries": detector_boundaries(
                native, observe, quarantine
            ),
        }

        input_files: dict[str, Any] = {
            "fault_json": {
                "path": str(fault_path),
                "sha256": sha256_file(fault_path),
            },
            "registry": {
                "path": str(registry_path),
                "sha256": sha256_file(registry_path),
            },
            "routing": {
                "path": str(routing_path),
                "sha256": sha256_file(routing_path),
            },
            "native_result": {
                "path": str(native_result_path),
                "sha256": sha256_file(native_result_path),
            },
        }
        if observe_path is not None:
            input_files["observe_result"] = {
                "path": str(observe_path),
                "sha256": sha256_file(observe_path),
            }
        if quarantine_path is not None:
            input_files["diagnostic_quarantine_result"] = {
                "path": str(quarantine_path),
                "sha256": sha256_file(quarantine_path),
            }

        oracle: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "program_version": PROGRAM_VERSION,
            "kind": "stage5_batch_multidimensional_oracle",
            "generated_at_utc": utc_now(),
            "fault_id": fault_id,
            "base_fault_id": spec.get("base_fault_id"),
            "route": routing.get("route"),
            "raw_facts": raw_facts,
            "derived_conclusions": derived,
            "private_ground_truth": private_ground_truth,
            "detector_registry_record": detector,
            "input_files": input_files,
            "interpretation_contract": {
                "native_result_defines_natural_outcome": True,
                "diagnostic_results_are_counterfactual_after_first_event": True,
                "detector_effect_is_a_hint_not_unique_root_cause": True,
                "first_detector_boundary_is_not_earliest_local_divergence": True,
                "exact_labels_are_private_ground_truth": True,
                "sva_generated": False,
            },
        }
        digest_input = dict(oracle)
        digest_input.pop("generated_at_utc", None)
        oracle["oracle_digest_sha256"] = canonical_digest(digest_input)

        site = require_mapping(spec.get("site"), "fault site")
        prompt: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "program_version": PROGRAM_VERSION,
            "kind": "stage5_batch_assertion_generation_prompt_context",
            "generated_at_utc": utc_now(),
            "fault_id": fault_id,
            "base_fault_id": spec.get("base_fault_id"),
            "design": "cv32e40p",
            "workload": "crc32",
            "fault": {
                "fault_class": spec.get("fault_class"),
                "polarity": spec.get("polarity"),
                "stuck_at": spec.get("stuck_at"),
                "module": site.get("module"),
                "receiver_class": site.get("receiver_class"),
            },
            "observed_behavior": {
                "native_status": native_status,
                "native_completion": natural.get("completion"),
                "native_architectural_outcome": natural.get(
                    "architectural_outcome"
                ),
                "existing_detector_id": (
                    detector.get("detector_id") if detector else None
                ),
                "detector_effect_hint": (
                    detector.get("effect_hint") if detector else None
                ),
                "observe_status": observe.get("status") if observe else None,
                "diagnostic_quarantine_status": (
                    quarantine.get("status") if quarantine else None
                ),
                "validated_capability": capability["validated_capability"],
                "capability_scope": capability["scope"],
            },
            "redaction_policy": {
                "exact_labels_hidden": True,
                "hidden_fields": [
                    "exact source net",
                    "receiver signal expressions",
                    "detector cycle and simulation time",
                    "detector payload address/data/byte-enable",
                    "golden/fault value pairs",
                    "expected assertion expression",
                ],
            },
            "sva_generated": False,
        }
        prompt_digest_input = dict(prompt)
        prompt_digest_input.pop("generated_at_utc", None)
        prompt["prompt_context_digest_sha256"] = canonical_digest(
            prompt_digest_input
        )

        write_json(args.oracle, oracle)
        write_json(args.prompt_context, prompt)

        print("Stage5 generic batch oracle construction: PASS")
        print(f"Fault ID                    : {fault_id}")
        print(f"Route                       : {routing.get('route')}")
        print(f"Native status               : {native_status}")
        print(
            "OBSERVE status              : "
            + (str(observe.get("status")) if observe else "NOT_RUN")
        )
        print(
            "DIAGNOSTIC_QUARANTINE status: "
            + (str(quarantine.get("status")) if quarantine else "NOT_RUN")
        )
        print(
            "Validated capability        : "
            f"{capability['validated_capability']}"
        )
        print("Exact labels private        : YES")
        print("SVA generated               : NO")
        return 0
    except OracleError as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
