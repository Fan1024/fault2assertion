#!/usr/bin/env python3
"""Independently validate one generic Stage-5 batch oracle.

The validator re-reads the original run result files, routing record, fault spec,
and detector registry. It does not import the oracle builder and does not run
simulation.
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


class ValidationError(RuntimeError):
    """Controlled validation failure."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid {label} JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must contain one object: {path}")
    return value


def require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be an object")
    return value


def require_value(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise ValidationError(
            f"{label} mismatch: expected={expected!r}, actual={actual!r}"
        )


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
        raise ValidationError(f"refusing to overwrite report: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def expected_capability(
    native_status: str,
    observe_status: str | None,
    quarantine_status: str | None,
) -> tuple[str, str]:
    if native_status == "OUTPUT_MATCH":
        return "NATIVE_COMPLETES", "NATURAL_EXECUTION"
    if native_status == "OUTPUT_MISMATCH":
        return "NATIVE_COMPLETES_WITH_OUTPUT_CORRUPTION", "NATURAL_EXECUTION"
    if native_status == "TIMEOUT":
        return "NATIVE_TIMEOUT", "NATURAL_EXECUTION"
    if observe_status == "DIAGNOSTIC_OUTPUT_MATCH":
        return "OBSERVE_CONTINUABLE", "CURRENT_REGISTERED_QUARANTINE_POLICY"
    if observe_status == "DIAGNOSTIC_OUTPUT_MISMATCH":
        return (
            "OBSERVE_CONTINUABLE_WITH_OUTPUT_CORRUPTION",
            "CURRENT_REGISTERED_QUARANTINE_POLICY",
        )
    if quarantine_status == "DIAGNOSTIC_OUTPUT_MATCH":
        return "QUARANTINE_REQUIRED", "CURRENT_REGISTERED_QUARANTINE_POLICY"
    if quarantine_status == "DIAGNOSTIC_OUTPUT_MISMATCH":
        return (
            "QUARANTINE_REQUIRED_WITH_OUTPUT_CORRUPTION",
            "CURRENT_REGISTERED_QUARANTINE_POLICY",
        )
    return "NON_CONTINUABLE", "CURRENT_REGISTERED_QUARANTINE_POLICY"


def registry_record(
    registry: Mapping[str, Any], detector_id: str | None
) -> dict[str, Any] | None:
    if detector_id is None:
        return None
    values = registry.get("detectors")
    if not isinstance(values, list):
        raise ValidationError("registry has no detectors array")
    matches = [
        item
        for item in values
        if isinstance(item, dict) and item.get("detector_id") == detector_id
    ]
    if len(matches) != 1:
        raise ValidationError(f"detector is not uniquely registered: {detector_id}")
    return dict(matches[0])


def reject_forbidden_prompt_keys(value: Any, path: str = "prompt") -> None:
    forbidden = {
        "source_net",
        "receivers",
        "receiver_signals",
        "cycle",
        "simulation_time",
        "address",
        "write_data",
        "byte_enable",
        "golden_value",
        "fault_value",
        "candidate_preview",
        "expected_sva",
        "expression",
    }
    if isinstance(value, dict):
        for key, item in value.items():
            if key in forbidden:
                raise ValidationError(
                    f"prompt exposes forbidden exact-label key: {path}.{key}"
                )
            reject_forbidden_prompt_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            reject_forbidden_prompt_keys(item, f"{path}[{index}]")


def contains_exact_value(value: Any, target: str) -> bool:
    if isinstance(value, str):
        return value == target
    if isinstance(value, dict):
        return any(contains_exact_value(item, target) for item in value.values())
    if isinstance(value, list):
        return any(contains_exact_value(item, target) for item in value)
    return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oracle", type=Path, required=True)
    parser.add_argument("--prompt-context", type=Path, required=True)
    parser.add_argument("--fault-json", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--routing", type=Path, required=True)
    parser.add_argument("--native-run", type=Path, required=True)
    parser.add_argument("--observe-run", type=Path)
    parser.add_argument("--quarantine-run", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        oracle = load_json(args.oracle.resolve(), "oracle")
        prompt = load_json(args.prompt_context.resolve(), "prompt context")
        spec = load_json(args.fault_json.resolve(), "fault spec")
        registry = load_json(args.registry.resolve(), "assertion registry")
        routing = load_json(args.routing.resolve(), "routing record")
        native = load_json(
            args.native_run.resolve() / "result.json", "Native result"
        )

        fault_id = str(spec.get("fault_id", ""))
        require_value(oracle.get("fault_id"), fault_id, "oracle fault ID")
        require_value(prompt.get("fault_id"), fault_id, "prompt fault ID")
        require_value(routing.get("fault_id"), fault_id, "routing fault ID")
        require_value(
            oracle.get("kind"),
            "stage5_batch_multidimensional_oracle",
            "oracle kind",
        )
        require_value(prompt.get("sva_generated"), False, "prompt SVA flag")

        native_status = str(native.get("status"))
        if native_status not in NATIVE_STATUSES:
            raise ValidationError(f"unsupported Native status: {native_status}")
        require_value(
            native.get("run_purpose"),
            "NATIVE_CHARACTERIZATION",
            "Native run purpose",
        )

        observe: dict[str, Any] | None = None
        quarantine: dict[str, Any] | None = None
        observe_status: str | None = None
        quarantine_status: str | None = None

        if native_status == "EXISTING_ASSERTION_DETECTED":
            require_value(
                routing.get("route"),
                "DIAGNOSTIC_THREE_MODE",
                "diagnostic route",
            )
            if args.observe_run is None or args.quarantine_run is None:
                raise ValidationError("diagnostic route is missing run directories")
            observe = load_json(
                args.observe_run.resolve() / "result.json", "OBSERVE result"
            )
            quarantine = load_json(
                args.quarantine_run.resolve() / "result.json",
                "DIAGNOSTIC_QUARANTINE result",
            )
            observe_status = str(observe.get("status"))
            quarantine_status = str(quarantine.get("status"))
            if observe_status not in DIAGNOSTIC_STATUSES:
                raise ValidationError(
                    f"unsupported OBSERVE status: {observe_status}"
                )
            if quarantine_status not in DIAGNOSTIC_STATUSES:
                raise ValidationError(
                    "unsupported DIAGNOSTIC_QUARANTINE status: "
                    f"{quarantine_status}"
                )
            require_value(
                observe.get("run_purpose"),
                "DIAGNOSTIC_OBSERVE",
                "OBSERVE run purpose",
            )
            require_value(
                quarantine.get("run_purpose"),
                "DIAGNOSTIC_QUARANTINE",
                "DIAGNOSTIC_QUARANTINE run purpose",
            )
        else:
            require_value(routing.get("route"), "NATIVE_ONLY", "Native route")
            if args.observe_run is not None or args.quarantine_run is not None:
                raise ValidationError("Native-only oracle received diagnostic runs")

        raw = require_mapping(oracle.get("raw_facts"), "oracle raw facts")
        require_value(raw.get("native"), native.get("raw_facts"), "Native raw facts")
        require_value(
            raw.get("observe"),
            observe.get("raw_facts") if observe else None,
            "OBSERVE raw facts",
        )
        require_value(
            raw.get("diagnostic_quarantine"),
            quarantine.get("raw_facts") if quarantine else None,
            "DIAGNOSTIC_QUARANTINE raw facts",
        )

        conclusions = require_mapping(
            oracle.get("derived_conclusions"), "derived conclusions"
        )
        natural = require_mapping(
            conclusions.get("natural_execution"), "natural execution"
        )
        require_value(
            natural.get("runner_status"), native_status, "natural runner status"
        )
        native_raw = require_mapping(native.get("raw_facts"), "Native raw facts")
        native_workload = require_mapping(
            native_raw.get("workload"), "Native workload"
        )
        require_value(
            natural.get("architectural_outcome"),
            native_workload.get("architectural_outcome"),
            "natural architectural outcome",
        )

        expected, expected_scope = expected_capability(
            native_status, observe_status, quarantine_status
        )
        capability = require_mapping(
            conclusions.get("continuation_capability"),
            "continuation capability",
        )
        require_value(
            capability.get("validated_capability"),
            expected,
            "validated capability",
        )
        require_value(capability.get("scope"), expected_scope, "capability scope")

        detector_id = routing.get("detector_id")
        detector = registry_record(registry, detector_id)
        require_value(
            oracle.get("detector_registry_record"), detector, "detector record"
        )
        detection = require_mapping(conclusions.get("detection"), "detection")
        require_value(
            detection.get("detector_id"),
            detector.get("detector_id") if detector else None,
            "derived detector ID",
        )

        private = require_mapping(
            oracle.get("private_ground_truth"), "private ground truth"
        )
        injection = require_mapping(
            private.get("exact_fault_injection_signal"), "injection label"
        )
        site = require_mapping(spec.get("site"), "fault site")
        require_value(injection.get("module"), site.get("module"), "module label")
        require_value(
            injection.get("source_net"), site.get("source_net"), "source label"
        )
        boundaries = require_mapping(
            private.get("first_observable_detector_boundaries"),
            "detector boundaries",
        )
        require_value(
            boundaries.get("earliest_local_divergence_status"),
            "NOT_COMPUTED_IN_BATCH_ORACLE",
            "local divergence status",
        )

        contract = require_mapping(
            oracle.get("interpretation_contract"), "interpretation contract"
        )
        require_value(contract.get("sva_generated"), False, "oracle SVA flag")
        require_value(
            contract.get("native_result_defines_natural_outcome"),
            True,
            "Native authority contract",
        )

        reject_forbidden_prompt_keys(prompt)
        redaction = require_mapping(prompt.get("redaction_policy"), "redaction")
        require_value(
            redaction.get("exact_labels_hidden"), True, "exact-label redaction"
        )
        source_net = str(site.get("source_net", ""))
        if source_net and contains_exact_value(prompt, source_net):
            raise ValidationError("prompt exposes exact source-net value")

        oracle_copy = dict(oracle)
        stored_oracle_digest = oracle_copy.pop("oracle_digest_sha256", None)
        oracle_copy.pop("generated_at_utc", None)
        require_value(
            stored_oracle_digest,
            canonical_digest(oracle_copy),
            "oracle digest",
        )

        prompt_copy = dict(prompt)
        stored_prompt_digest = prompt_copy.pop(
            "prompt_context_digest_sha256", None
        )
        prompt_copy.pop("generated_at_utc", None)
        require_value(
            stored_prompt_digest,
            canonical_digest(prompt_copy),
            "prompt digest",
        )

        report = {
            "schema_version": SCHEMA_VERSION,
            "program_version": PROGRAM_VERSION,
            "kind": "stage5_batch_oracle_validation",
            "generated_at_utc": utc_now(),
            "status": "PASS",
            "fault_id": fault_id,
            "native_status": native_status,
            "observe_status": observe_status,
            "diagnostic_quarantine_status": quarantine_status,
            "validated_capability": expected,
            "gate_claims": {
                "fault_identity_matches": True,
                "routing_matches_native_status": True,
                "raw_facts_replayed": True,
                "natural_outcome_preserved": True,
                "registry_detector_replayed": True,
                "generic_outcome_classification_valid": True,
                "exact_injection_label_private": True,
                "earliest_local_divergence_not_overclaimed": True,
                "prompt_exact_labels_hidden": True,
                "oracle_digest_valid": True,
                "prompt_digest_valid": True,
                "sva_generated": False,
            },
        }
        write_json(args.report, report)

        print("Stage5 generic batch oracle validation: PASS")
        print(f"Fault ID                    : {fault_id}")
        print(f"Native status               : {native_status}")
        print(f"OBSERVE status              : {observe_status or 'NOT_RUN'}")
        print(
            "DIAGNOSTIC_QUARANTINE status: "
            f"{quarantine_status or 'NOT_RUN'}"
        )
        print(f"Validated capability        : {expected}")
        print("Prompt exact labels hidden  : YES")
        print("SVA generated               : NO")
        return 0
    except ValidationError as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
