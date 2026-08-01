#!/usr/bin/env python3
"""Independently validate the minimal Stage-5 Phase2-G5 oracle.

The validator replays the key G2/G3/G4 facts, recomputes continuation
capability, checks that private exact labels are present, and confirms that the
prompt-context view omits exact cycle/signal labels. It also rejects any SVA
artifact in the G5 output root.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


class G5ValidationError(RuntimeError):
    """Controlled G5 validation failure."""


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise G5ValidationError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise G5ValidationError(f"invalid {label} JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise G5ValidationError(f"{label} must contain one JSON object: {path}")
    return payload


def require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise G5ValidationError(f"{label} must be an object")
    return value


def require_value(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise G5ValidationError(
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
        raise G5ValidationError(f"refusing to overwrite existing report: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def first_event(result: Mapping[str, Any], label: str) -> dict[str, Any]:
    raw = require_mapping(result.get("raw_facts"), f"{label} raw facts")
    detector = require_mapping(
        raw.get("existing_detector_baseline"), f"{label} detector"
    )
    for key in ("events", "xcelium_events"):
        events = detector.get(key)
        if isinstance(events, list) and events and isinstance(events[0], dict):
            return dict(events[0])
    raise G5ValidationError(f"{label} has no detector event")


def capability(observe_status: str, quarantine_status: str) -> str:
    completed = {
        "DIAGNOSTIC_OUTPUT_MATCH",
        "DIAGNOSTIC_OUTPUT_MISMATCH",
    }
    if observe_status in completed:
        return "OBSERVE_CONTINUABLE"
    if quarantine_status in completed:
        return "QUARANTINE_REQUIRED"
    return "NON_CONTINUABLE"


def reject_forbidden_prompt_keys(value: Any, path: str = "prompt") -> None:
    forbidden = {
        "cycle",
        "simulation_time",
        "source_net",
        "receiver_signals",
        "expression",
        "address",
        "write_data",
        "byte_enable",
        "golden_value",
        "fault_value",
        "candidate_preview",
        "expected_sva",
    }
    if isinstance(value, dict):
        for key, item in value.items():
            if key in forbidden:
                raise G5ValidationError(
                    f"prompt context exposes forbidden exact-label key: {path}.{key}"
                )
            reject_forbidden_prompt_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            reject_forbidden_prompt_keys(item, f"{path}[{index}]")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oracle", type=Path, required=True)
    parser.add_argument("--prompt-context", type=Path, required=True)
    parser.add_argument("--g2-report", type=Path, required=True)
    parser.add_argument("--g3-report", type=Path, required=True)
    parser.add_argument("--g4-report", type=Path, required=True)
    parser.add_argument("--g2-run", type=Path, required=True)
    parser.add_argument("--g3-run", type=Path, required=True)
    parser.add_argument("--g4-run", type=Path, required=True)
    parser.add_argument("--fault-json", type=Path, required=True)
    parser.add_argument("--g5-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        oracle = load_json(args.oracle.resolve(), "G5 oracle")
        prompt = load_json(args.prompt_context.resolve(), "G5 prompt context")
        g2 = load_json(args.g2_report.resolve(), "G2 report")
        g3 = load_json(args.g3_report.resolve(), "G3 report")
        g4 = load_json(args.g4_report.resolve(), "G4 report")
        spec = load_json(args.fault_json.resolve(), "fault spec")
        native_result = load_json(
            args.g2_run.resolve() / "result.json", "G2 Native result"
        )
        observe_result = load_json(
            args.g3_run.resolve() / "result.json", "G3 OBSERVE result"
        )
        quarantine_result = load_json(
            args.g4_run.resolve() / "result.json",
            "G4 DIAGNOSTIC_QUARANTINE result",
        )

        fault_id = str(spec.get("fault_id", ""))
        for label, report in (("G2", g2), ("G3", g3), ("G4", g4)):
            require_value(report.get("status"), "PASS", f"{label} status")
            require_value(report.get("fault_id"), fault_id, f"{label} fault ID")

        require_value(oracle.get("fault_id"), fault_id, "oracle fault ID")
        require_value(prompt.get("fault_id"), fault_id, "prompt fault ID")
        require_value(
            oracle.get("kind"),
            "stage5_phase2_g5_multidimensional_diagnostic_oracle",
            "oracle kind",
        )
        require_value(prompt.get("sva_generated"), False, "prompt SVA flag")

        raw = require_mapping(oracle.get("raw_facts"), "oracle raw facts")
        require_value(raw.get("native"), native_result.get("raw_facts"), "Native raw facts")
        require_value(raw.get("observe"), observe_result.get("raw_facts"), "OBSERVE raw facts")
        require_value(
            raw.get("diagnostic_quarantine"),
            quarantine_result.get("raw_facts"),
            "DIAGNOSTIC_QUARANTINE raw facts",
        )

        conclusions = require_mapping(
            oracle.get("derived_conclusions"), "derived conclusions"
        )
        natural = require_mapping(
            conclusions.get("natural_execution"), "natural execution"
        )
        require_value(
            natural.get("runner_status"),
            "EXISTING_ASSERTION_DETECTED",
            "natural runner status",
        )
        require_value(
            natural.get("architectural_outcome"),
            "CENSORED",
            "natural architectural outcome",
        )
        require_value(
            natural.get("natural_outcome_censored"),
            True,
            "natural censorship",
        )

        observe_status = str(observe_result.get("status"))
        quarantine_status = str(quarantine_result.get("status"))
        require_value(observe_status, "DIAGNOSTIC_TIMEOUT", "OBSERVE status")
        require_value(
            quarantine_status,
            "DIAGNOSTIC_TIMEOUT",
            "DIAGNOSTIC_QUARANTINE status",
        )
        continuation = require_mapping(
            conclusions.get("continuation_capability"),
            "continuation capability",
        )
        require_value(
            continuation.get("validated_capability"),
            capability(observe_status, quarantine_status),
            "validated capability",
        )
        require_value(
            continuation.get("scope"),
            "CURRENT_REGISTERED_QUARANTINE_POLICY",
            "capability scope",
        )

        private = require_mapping(
            oracle.get("private_ground_truth"), "private ground truth"
        )
        injection = require_mapping(
            private.get("exact_fault_injection_signal"),
            "exact fault injection signal",
        )
        boundary = require_mapping(
            private.get("first_observable_detector_boundary"),
            "first observable detector boundary",
        )
        site = require_mapping(spec.get("site"), "fault spec site")
        require_value(injection.get("module"), site.get("module"), "injection module")
        require_value(
            injection.get("source_net"), site.get("source_net"), "injection signal"
        )

        observe_event = first_event(observe_result, "OBSERVE")
        quarantine_event = first_event(
            quarantine_result, "DIAGNOSTIC_QUARANTINE"
        )
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
                f"G3/G4 detector {key}",
            )
        require_value(
            boundary.get("cycle"), observe_event.get("cycle"), "private detector cycle"
        )
        require_value(
            boundary.get("simulation_time"),
            observe_event.get("simulation_time"),
            "private detector time",
        )
        require_value(
            boundary.get("earliest_local_divergence_status"),
            "NOT_COMPUTED_IN_MINIMAL_G5",
            "local divergence status",
        )

        require_value(
            oracle.get("interpretation_contract", {}).get("sva_generated"),
            False,
            "oracle SVA flag",
        )
        reject_forbidden_prompt_keys(prompt)
        redaction = require_mapping(prompt.get("redaction_policy"), "redaction policy")
        require_value(
            redaction.get("exact_labels_hidden"), True, "exact-label redaction"
        )

        oracle_digest_input = dict(oracle)
        stored_oracle_digest = oracle_digest_input.pop("oracle_digest_sha256", None)
        oracle_digest_input.pop("generated_at_utc", None)
        require_value(
            stored_oracle_digest,
            canonical_digest(oracle_digest_input),
            "oracle digest",
        )

        prompt_digest_input = dict(prompt)
        stored_prompt_digest = prompt_digest_input.pop(
            "prompt_context_digest_sha256", None
        )
        prompt_digest_input.pop("generated_at_utc", None)
        require_value(
            stored_prompt_digest,
            canonical_digest(prompt_digest_input),
            "prompt context digest",
        )

        g5_root = args.g5_root.resolve()
        sva_files = list(g5_root.rglob("*.sva"))
        if sva_files:
            raise G5ValidationError(f"G5 generated forbidden SVA: {sva_files[0]}")

        output = args.report.resolve()
        payload = {
            "schema_version": "1.0",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "gate": "stage5_phase2_g5_minimal_oracle_validation",
            "status": "PASS",
            "fault_id": fault_id,
            "oracle": str(args.oracle.resolve()),
            "prompt_context": str(args.prompt_context.resolve()),
            "validated_capability": "NON_CONTINUABLE",
            "capability_scope": "CURRENT_REGISTERED_QUARANTINE_POLICY",
            "gate_claims": {
                "g2_g3_g4_merged": True,
                "raw_facts_preserved": True,
                "derived_conclusions_validated": True,
                "capability_is_non_continuable_under_current_policy": True,
                "exact_injection_signal_stored_privately": True,
                "exact_detector_cycle_stored_privately": True,
                "earliest_local_divergence_not_overclaimed": True,
                "prompt_exact_labels_hidden": True,
                "oracle_digest_valid": True,
                "prompt_context_digest_valid": True,
                "sva_generated": False,
            },
        }
        write_json(output, payload)

    except G5ValidationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print("Stage5 Phase2-G5 independent oracle validation: PASS")
    print(f"Fault ID                         : {fault_id}")
    print("G2/G3/G4 merged                  : YES")
    print("Raw facts preserved              : YES")
    print("Validated capability              : NON_CONTINUABLE")
    print("Exact injection signal private    : YES")
    print("Exact detector cycle private      : YES")
    print("Prompt exact labels hidden        : YES")
    print("SVA generated                     : NO")
    print(f"Validation report                 : {args.report.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
