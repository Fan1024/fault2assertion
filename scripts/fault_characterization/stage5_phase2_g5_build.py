#!/usr/bin/env python3
"""Build the minimal Stage-5 Phase2-G5 diagnostic oracle.

G5 does not run simulation and does not generate SVA. It merges the validated
G2 Native, G3 OBSERVE, and G4 DIAGNOSTIC_QUARANTINE evidence into:

* one private machine-readable oracle with raw facts and derived conclusions;
* one prompt-context view that hides exact cycle/signal labels.

The minimal G5 ground truth records the exact injected signal from fault.json
and the exact first observable detector cycle from structured diagnostic events.
It does not claim to compute the earliest local signal divergence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "1.0"
PROGRAM_VERSION = "1.0.0"


class G5BuildError(RuntimeError):
    """Controlled G5 oracle construction failure."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise G5BuildError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise G5BuildError(f"invalid {label} JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise G5BuildError(f"{label} must contain one JSON object: {path}")
    return payload


def require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise G5BuildError(f"{label} must be an object")
    return value


def require_value(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise G5BuildError(
            f"{label} mismatch: expected={expected!r}, actual={actual!r}"
        )


def canonical_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path = path.expanduser().resolve()
    if path.exists():
        raise G5BuildError(f"refusing to overwrite existing output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def first_detector_event(result: Mapping[str, Any], label: str) -> dict[str, Any]:
    raw = require_mapping(result.get("raw_facts"), f"{label}.raw_facts")
    detector = require_mapping(
        raw.get("existing_detector_baseline"),
        f"{label}.existing_detector_baseline",
    )
    for key in ("events", "xcelium_events"):
        events = detector.get(key)
        if isinstance(events, list) and events:
            event = events[0]
            if isinstance(event, dict):
                return dict(event)
    raise G5BuildError(f"{label} contains no detector event")


def validated_report(report: Mapping[str, Any], gate: str, fault_id: str) -> None:
    require_value(report.get("status"), "PASS", f"{gate} status")
    require_value(report.get("fault_id"), fault_id, f"{gate} fault ID")


def detector_record(policy: Mapping[str, Any]) -> dict[str, Any]:
    detectors = policy.get("detectors")
    if not isinstance(detectors, list):
        raise G5BuildError("assertion policy has no detectors array")
    matches = [
        item
        for item in detectors
        if isinstance(item, dict)
        and item.get("assertion_leaf_name") == "out_of_bounds_write"
    ]
    if len(matches) != 1:
        raise G5BuildError(
            "assertion policy must contain exactly one out_of_bounds_write detector"
        )
    return dict(matches[0])


def diagnostic_summary(result: Mapping[str, Any], expected_mode: str) -> dict[str, Any]:
    raw = require_mapping(result.get("raw_facts"), f"{expected_mode} raw facts")
    execution = require_mapping(raw.get("execution"), f"{expected_mode} execution")
    workload = require_mapping(raw.get("workload"), f"{expected_mode} workload")
    intervention = require_mapping(
        raw.get("intervention"), f"{expected_mode} intervention"
    )
    event = first_detector_event(result, expected_mode)
    return {
        "runner_status": result.get("status"),
        "completion": execution.get("completion"),
        "workload_outcome": workload.get("outcome"),
        "architectural_outcome": workload.get("architectural_outcome"),
        "termination_suppressed": intervention.get("termination_suppressed"),
        "transaction_quarantine": intervention.get("transaction_quarantine"),
        "counterfactual_after_first_detector_event": intervention.get(
            "counterfactual_after_first_detector_event"
        ),
        "detector_event": event,
    }


def continuation_restored(status: str) -> bool:
    return status in {
        "DIAGNOSTIC_OUTPUT_MATCH",
        "DIAGNOSTIC_OUTPUT_MISMATCH",
    }


def classify_capability(observe_status: str, quarantine_status: str) -> dict[str, Any]:
    observe_restored = continuation_restored(observe_status)
    quarantine_restored = continuation_restored(quarantine_status)
    if observe_restored:
        capability = "OBSERVE_CONTINUABLE"
        basis = "OBSERVE_REACHED_WORKLOAD_COMPLETION"
    elif quarantine_restored:
        capability = "QUARANTINE_REQUIRED"
        basis = "OBSERVE_DID_NOT_COMPLETE_AND_DIAGNOSTIC_QUARANTINE_COMPLETED"
    else:
        capability = "NON_CONTINUABLE"
        basis = "OBSERVE_AND_DIAGNOSTIC_QUARANTINE_DID_NOT_COMPLETE"
    return {
        "validated_capability": capability,
        "scope": "CURRENT_REGISTERED_QUARANTINE_POLICY",
        "basis": basis,
        "observe_continuation_restored": observe_restored,
        "diagnostic_quarantine_continuation_restored": quarantine_restored,
    }


def exact_injection_label(spec: Mapping[str, Any]) -> dict[str, Any]:
    site = require_mapping(spec.get("site"), "fault spec site")
    source_net = site.get("source_net")
    module = site.get("module")
    if not isinstance(source_net, str) or not source_net:
        raise G5BuildError("fault spec site.source_net is missing")
    if not isinstance(module, str) or not module:
        raise G5BuildError("fault spec site.module is missing")
    return {
        "label_kind": "FAULT_INJECTION_SIGNAL",
        "module": module,
        "source_net": source_net,
        "selection_id": spec.get("selection_id"),
        "site_id": spec.get("site_id"),
        "fault_class": spec.get("fault_class"),
        "polarity": spec.get("polarity"),
        "stuck_at": spec.get("stuck_at"),
        "receiver_signals": spec.get("receiver_signals", []),
    }


def common_detector_boundary(
    observe_event: Mapping[str, Any],
    quarantine_event: Mapping[str, Any],
) -> dict[str, Any]:
    for key in (
        "detector_origin",
        "assertion_leaf_name",
        "detector_reported_effect_hint",
        "cycle",
        "simulation_time",
    ):
        require_value(
            quarantine_event.get(key),
            observe_event.get(key),
            f"G3/G4 first detector {key}",
        )
    return {
        "label_kind": "FIRST_OBSERVABLE_DETECTOR_BOUNDARY",
        "cycle": observe_event.get("cycle"),
        "simulation_time": observe_event.get("simulation_time"),
        "detector_origin": observe_event.get("detector_origin"),
        "assertion_leaf_name": observe_event.get("assertion_leaf_name"),
        "detector_reported_effect_hint": observe_event.get(
            "detector_reported_effect_hint"
        ),
        "address": observe_event.get("address"),
        "write_data": observe_event.get("write_data"),
        "byte_enable": observe_event.get("byte_enable"),
        "observe_action": observe_event.get("action"),
        "diagnostic_quarantine_action": quarantine_event.get("action"),
        "earliest_local_divergence_status": "NOT_COMPUTED_IN_MINIMAL_G5",
    }


def build_prompt_context(
    *,
    fault_id: str,
    spec: Mapping[str, Any],
    manifest: Mapping[str, Any],
    detector: Mapping[str, Any],
    natural: Mapping[str, Any],
    observe: Mapping[str, Any],
    quarantine: Mapping[str, Any],
    capability: Mapping[str, Any],
) -> dict[str, Any]:
    site = require_mapping(spec.get("site"), "fault spec site")
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": utc_now(),
        "kind": "stage5_assertion_generation_prompt_context",
        "fault_id": fault_id,
        "design": manifest.get("design"),
        "workload": manifest.get("workload"),
        "fault_context": {
            "fault_class": spec.get("fault_class"),
            "polarity": spec.get("polarity"),
            "stuck_at": spec.get("stuck_at"),
            "module": site.get("module"),
        },
        "existing_detector_context": {
            "detector_id": detector.get("detector_id"),
            "origin": detector.get("origin"),
            "assertion_leaf_name": detector.get("assertion_leaf_name"),
            "detector_reported_effect_hint": detector.get("effect_hint"),
        },
        "execution_context": {
            "native_completion": natural.get("completion"),
            "native_architectural_outcome": natural.get("architectural_outcome"),
            "observe_result": observe.get("runner_status"),
            "diagnostic_quarantine_result": quarantine.get("runner_status"),
            "validated_continuation_capability": capability.get(
                "validated_capability"
            ),
        },
        "generation_task": (
            "Generate diagnostic assertions that detect the fault-induced abnormal "
            "behavior without using the hidden exact cycle or exact signal labels."
        ),
        "redaction_policy": {
            "exact_labels_hidden": True,
            "hidden_fields": [
                "fault injection source_net",
                "receiver signal expressions",
                "first observable detector cycle",
                "first observable detector simulation time",
                "detector address/write_data/byte_enable payload",
                "exact golden/fault value pairs",
                "expected SVA expression",
            ],
        },
        "sva_generated": False,
    }
    digest_input = dict(payload)
    digest_input.pop("generated_at_utc", None)
    payload["prompt_context_digest_sha256"] = canonical_digest(digest_input)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--g2-report", type=Path, required=True)
    parser.add_argument("--g3-report", type=Path, required=True)
    parser.add_argument("--g4-report", type=Path, required=True)
    parser.add_argument("--g2-run", type=Path, required=True)
    parser.add_argument("--g3-run", type=Path, required=True)
    parser.add_argument("--g4-run", type=Path, required=True)
    parser.add_argument("--fault-json", type=Path, required=True)
    parser.add_argument("--assertion-policy", type=Path, required=True)
    parser.add_argument("--oracle", type=Path, required=True)
    parser.add_argument("--prompt-context", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        g2_report = load_json(args.g2_report.resolve(), "G2 report")
        g3_report = load_json(args.g3_report.resolve(), "G3 report")
        g4_report = load_json(args.g4_report.resolve(), "G4 report")
        fault_spec = load_json(args.fault_json.resolve(), "fault spec")
        policy = load_json(args.assertion_policy.resolve(), "assertion policy")

        fault_id = str(fault_spec.get("fault_id", ""))
        if not fault_id:
            raise G5BuildError("fault spec has no fault_id")
        validated_report(g2_report, "G2", fault_id)
        validated_report(g3_report, "G3", fault_id)
        validated_report(g4_report, "G4", fault_id)

        g2_run = args.g2_run.resolve()
        g3_run = args.g3_run.resolve()
        g4_run = args.g4_run.resolve()
        native_result = load_json(g2_run / "result.json", "G2 Native result")
        observe_result = load_json(g3_run / "result.json", "G3 OBSERVE result")
        quarantine_result = load_json(
            g4_run / "result.json", "G4 DIAGNOSTIC_QUARANTINE result"
        )
        g4_manifest = load_json(g4_run / "manifest.json", "G4 run manifest")

        require_value(native_result.get("status"), "EXISTING_ASSERTION_DETECTED", "Native status")
        require_value(observe_result.get("status"), "DIAGNOSTIC_TIMEOUT", "OBSERVE status")
        require_value(
            quarantine_result.get("status"),
            "DIAGNOSTIC_TIMEOUT",
            "DIAGNOSTIC_QUARANTINE status",
        )
        require_value(g4_manifest.get("fault_id"), fault_id, "G4 manifest fault ID")

        native_raw = require_mapping(native_result.get("raw_facts"), "Native raw facts")
        native_execution = require_mapping(native_raw.get("execution"), "Native execution")
        native_workload = require_mapping(native_raw.get("workload"), "Native workload")
        native_event = first_detector_event(native_result, "Native")
        observe = diagnostic_summary(observe_result, "OBSERVE")
        quarantine = diagnostic_summary(
            quarantine_result, "DIAGNOSTIC_QUARANTINE"
        )
        detector = detector_record(policy)

        require_value(
            native_event.get("assertion_leaf_name"),
            detector.get("assertion_leaf_name"),
            "Native detector name",
        )
        require_value(
            observe["detector_event"].get("assertion_leaf_name"),
            detector.get("assertion_leaf_name"),
            "OBSERVE detector name",
        )
        require_value(
            quarantine["detector_event"].get("assertion_leaf_name"),
            detector.get("assertion_leaf_name"),
            "DIAGNOSTIC_QUARANTINE detector name",
        )
        require_value(
            observe["detector_event"].get("action"),
            "RECORD_ONLY",
            "OBSERVE action",
        )
        require_value(
            quarantine["detector_event"].get("action"),
            "RECORD_AND_QUARANTINE",
            "DIAGNOSTIC_QUARANTINE action",
        )

        natural = {
            "runner_status": native_result.get("status"),
            "completion": native_execution.get("completion"),
            "workload_outcome": native_workload.get("outcome"),
            "architectural_outcome": native_workload.get("architectural_outcome"),
            "natural_outcome_censored": (
                native_workload.get("architectural_outcome") == "CENSORED"
            ),
            "detector_event": native_event,
        }
        capability = classify_capability(
            str(observe["runner_status"]),
            str(quarantine["runner_status"]),
        )
        injection_label = exact_injection_label(fault_spec)
        detector_boundary = common_detector_boundary(
            observe["detector_event"],
            quarantine["detector_event"],
        )

        oracle: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "program_version": PROGRAM_VERSION,
            "generated_at_utc": utc_now(),
            "kind": "stage5_phase2_g5_multidimensional_diagnostic_oracle",
            "status": "VALIDATED_INPUTS_MERGED",
            "fault_id": fault_id,
            "design": g4_manifest.get("design"),
            "workload": g4_manifest.get("workload"),
            "fault_spec": fault_spec,
            "detector_registry_record": detector,
            "raw_facts": {
                "native": native_raw,
                "observe": require_mapping(
                    observe_result.get("raw_facts"), "OBSERVE raw facts"
                ),
                "diagnostic_quarantine": require_mapping(
                    quarantine_result.get("raw_facts"),
                    "DIAGNOSTIC_QUARANTINE raw facts",
                ),
            },
            "derived_conclusions": {
                "detection": {
                    "detected_by_existing_detector": True,
                    "detector_id": detector.get("detector_id"),
                    "detector_origin": detector.get("origin"),
                    "detector_reported_effect_hint": detector.get("effect_hint"),
                    "effect_hint_status": "HINT_ONLY_NOT_INDEPENDENTLY_CONFIRMED",
                },
                "natural_execution": natural,
                "observe_execution": observe,
                "diagnostic_quarantine_execution": quarantine,
                "continuation_capability": capability,
                "timeout_interpretation": {
                    "fault_associated_timeout_observed": True,
                    "unique_root_cause_proven": False,
                    "statement": (
                        "Neither fatal suppression alone nor the current registered "
                        "ACKNOWLEDGE_AND_DROP_WRITE quarantine policy restored workload "
                        "completion within the configured simulation window."
                    ),
                },
                "root_cause": {
                    "status": "UNRESOLVED",
                    "confidence": "LOW",
                },
                "confidence": {
                    "raw_execution_facts": "HIGH",
                    "detector_identity": "HIGH",
                    "natural_completion": "HIGH",
                    "continuation_capability_under_current_policy": "HIGH",
                    "unique_timeout_root_cause": "LOW",
                },
            },
            "private_ground_truth": {
                "visibility": "TRAINING_EVALUATION_ONLY",
                "exact_fault_injection_signal": injection_label,
                "first_observable_detector_boundary": detector_boundary,
                "exact_local_divergence": {
                    "status": "NOT_COMPUTED_IN_MINIMAL_G5",
                    "reason": (
                        "G5 intentionally avoids additional trace mining; the exact "
                        "injection signal and first observable detector boundary are "
                        "preserved without claiming an earliest local divergence."
                    ),
                },
            },
            "evidence": {
                "g2_report": str(args.g2_report.resolve()),
                "g3_report": str(args.g3_report.resolve()),
                "g4_report": str(args.g4_report.resolve()),
                "g2_native_result": str(g2_run / "result.json"),
                "g3_observe_result": str(g3_run / "result.json"),
                "g4_diagnostic_quarantine_result": str(g4_run / "result.json"),
                "fault_json": str(args.fault_json.resolve()),
                "assertion_policy": str(args.assertion_policy.resolve()),
                "g2_native_trace": g2_report.get("traces", {}).get("fault"),
                "g3_observe_trace": g3_report.get("artifacts", {}).get("trace"),
                "g4_diagnostic_quarantine_trace": g4_report.get("artifacts", {}).get("trace"),
            },
            "interpretation_contract": {
                "native_defines_natural_outcome": True,
                "diagnostic_results_are_counterfactual_after_first_event": True,
                "diagnostic_results_do_not_replace_native_outcome": True,
                "exact_labels_are_private_ground_truth": True,
                "sva_generated": False,
            },
        }
        digest_input = dict(oracle)
        digest_input.pop("generated_at_utc", None)
        oracle["oracle_digest_sha256"] = canonical_digest(digest_input)

        prompt_context = build_prompt_context(
            fault_id=fault_id,
            spec=fault_spec,
            manifest=g4_manifest,
            detector=detector,
            natural=natural,
            observe=observe,
            quarantine=quarantine,
            capability=capability,
        )

        write_json(args.oracle, oracle)
        write_json(args.prompt_context, prompt_context)

    except G5BuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print("Stage5 Phase2-G5 oracle construction: PASS")
    print(f"Fault ID                         : {fault_id}")
    print("Natural architectural outcome    : CENSORED")
    print("OBSERVE result                    : DIAGNOSTIC_TIMEOUT")
    print("DIAGNOSTIC_QUARANTINE result      : DIAGNOSTIC_TIMEOUT")
    print("Validated capability              : NON_CONTINUABLE")
    print("Capability scope                  : CURRENT_REGISTERED_QUARANTINE_POLICY")
    print("Exact labels stored privately     : YES")
    print("Prompt exact labels hidden        : YES")
    print("SVA generated                     : NO")
    print(f"Oracle                            : {args.oracle.resolve()}")
    print(f"Prompt context                    : {args.prompt_context.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
