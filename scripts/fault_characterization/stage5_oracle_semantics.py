#!/usr/bin/env python3
"""Pure Stage-5 oracle semantic classifier.

This module contains no trace parsing and no file I/O in the classification
function.  It maps an immutable raw-fact summary to one label according to the
frozen policy order.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

PROGRAM_VERSION = "2.0.0"
EXPECTED_PRIORITY = (
    "SIMULATION_ERROR_OR_UNKNOWN",
    "TRACE_INVALID",
    "TRACE_SCOPE_MISMATCH",
    "INJECTION_ERROR",
    "FACT_CONTRADICTION",
    "NOT_ACTIVATED",
    "DETECTED_HANG",
    "DETECTED_OUTPUT_CORRUPTION",
    "LOCAL_PROPAGATION_MASKED_AT_OUTPUT",
    "SITE_DIVERGENCE_LOCALLY_MASKED",
    "FUNCTIONALLY_EQUIVALENT_UNDER_WORKLOAD",
)


class SemanticsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Classification:
    primary_class: str
    priority_index: int
    reason: str
    semantics_version: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_policy(path: Path) -> dict[str, Any]:
    path = path.resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SemanticsError("oracle policy must contain one JSON object")
    if payload.get("semantics_version") != PROGRAM_VERSION:
        raise SemanticsError(
            f"semantics version mismatch: expected={PROGRAM_VERSION}, "
            f"actual={payload.get('semantics_version')}"
        )
    priority = tuple(payload.get("classification_priority", []))
    if priority != EXPECTED_PRIORITY:
        raise SemanticsError(
            "classification priority does not match the frozen implementation"
        )
    classes = payload.get("classes")
    if not isinstance(classes, dict) or set(classes) != set(EXPECTED_PRIORITY):
        raise SemanticsError("oracle policy class definitions are incomplete")
    return payload


def _bool(mapping: Mapping[str, Any], key: str) -> bool:
    value = mapping.get(key)
    if not isinstance(value, bool):
        raise SemanticsError(f"raw fact {key!r} must be boolean; got {value!r}")
    return value


def _integer(mapping: Mapping[str, Any], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise SemanticsError(f"raw fact {key!r} must be integer; got {value!r}")
    return value


def classify(raw_facts: Mapping[str, Any], policy: Mapping[str, Any]) -> Classification:
    priority = tuple(policy["classification_priority"])
    runner = raw_facts.get("runner")
    trace = raw_facts.get("trace_validity")
    scope = raw_facts.get("scope_alignment")
    activation = raw_facts.get("activation")
    injection = raw_facts.get("injection")
    divergence = raw_facts.get("divergence")
    functional = raw_facts.get("functional_outcome")
    groups = {
        "runner": runner,
        "trace_validity": trace,
        "scope_alignment": scope,
        "activation": activation,
        "injection": injection,
        "divergence": divergence,
        "functional_outcome": functional,
    }
    for name, value in groups.items():
        if not isinstance(value, Mapping):
            raise SemanticsError(f"raw facts missing mapping group: {name}")

    runner_valid = _bool(runner, "valid_fault_run")
    status = str(runner.get("status", ""))
    golden_valid = _bool(trace, "golden_valid")
    fault_valid = _bool(trace, "fault_valid")
    common_scope_count = _integer(scope, "common_scope_count")
    activated = _bool(activation, "activated")
    injection_effective = _bool(injection, "effective")
    site_diverged = _bool(divergence, "site_diverged")
    receiver_diverged = _bool(divergence, "receiver_diverged")

    def result(name: str, reason: str) -> Classification:
        return Classification(
            primary_class=name,
            priority_index=priority.index(name),
            reason=reason,
            semantics_version=str(policy["semantics_version"]),
        )

    accepted = set(policy["runner_contract"]["accepted_fault_results"])
    if not runner_valid or status not in accepted:
        return result(
            "SIMULATION_ERROR_OR_UNKNOWN",
            f"runner verdict is not a valid fault result: {status!r}",
        )
    if not golden_valid or not fault_valid:
        return result(
            "TRACE_INVALID",
            "one or both compact traces failed structural validation",
        )
    if common_scope_count <= 0:
        return result(
            "TRACE_SCOPE_MISMATCH",
            "valid traces contain no common normalized scope",
        )
    if not injection_effective:
        return result(
            "INJECTION_ERROR",
            "observed injected-site values are not constrained to stuck-at value",
        )
    if not activated and status in {"OUTPUT_MISMATCH", "TIMEOUT"}:
        return result(
            "FACT_CONTRADICTION",
            "detected final outcome conflicts with missing golden activation",
        )
    if not activated:
        return result(
            "NOT_ACTIVATED",
            "golden source never reached the value required to activate the fault",
        )
    if status == "TIMEOUT":
        return result("DETECTED_HANG", "strict runner verdict is TIMEOUT")
    if status == "OUTPUT_MISMATCH":
        return result(
            "DETECTED_OUTPUT_CORRUPTION",
            "strict runner verdict reports CRC32/signature mismatch",
        )
    if receiver_diverged:
        return result(
            "LOCAL_PROPAGATION_MASKED_AT_OUTPUT",
            "direct receiver divergence occurred while final output matched",
        )
    if site_diverged:
        return result(
            "SITE_DIVERGENCE_LOCALLY_MASKED",
            "injected site diverged without direct receiver or final-output divergence",
        )
    return result(
        "FUNCTIONALLY_EQUIVALENT_UNDER_WORKLOAD",
        "activated effective fault produced no sampled local or final divergence",
    )
