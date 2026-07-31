#!/usr/bin/env python3
"""Table-driven tests for the frozen Stage-5 oracle classification priority."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import sys
from pathlib import Path
from typing import Any, Sequence


class SelfTestError(RuntimeError):
    pass


def load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path.resolve())
    if spec is None or spec.loader is None:
        raise SelfTestError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def base_facts() -> dict[str, Any]:
    return {
        "provenance": {},
        "runner": {
            "valid_fault_run": True,
            "status": "OUTPUT_MATCH",
            "strict_signature_valid": True,
        },
        "trace_validity": {
            "golden_valid": True,
            "fault_valid": True,
        },
        "scope_alignment": {"common_scope_count": 1},
        "activation": {"activated": True},
        "injection": {"effective": True},
        "divergence": {
            "site_diverged": False,
            "receiver_diverged": False,
        },
        "functional_outcome": {"status": "OUTPUT_MATCH"},
    }


def mutate(base: dict[str, Any], *changes: tuple[str, str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for group, key, value in changes:
        result[group][key] = value
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--semantics", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        module = load_module(args.semantics, "f2a_oracle_semantics_selftest")
        policy = module.load_policy(args.policy)
        base = base_facts()
        cases = [
            (
                "runner error dominates",
                mutate(base, ("runner", "valid_fault_run", False), ("runner", "status", "ERROR")),
                "SIMULATION_ERROR_OR_UNKNOWN",
            ),
            (
                "trace invalid",
                mutate(base, ("trace_validity", "fault_valid", False)),
                "TRACE_INVALID",
            ),
            (
                "scope mismatch",
                mutate(base, ("scope_alignment", "common_scope_count", 0)),
                "TRACE_SCOPE_MISMATCH",
            ),
            (
                "injection error before activation",
                mutate(
                    base,
                    ("injection", "effective", False),
                    ("activation", "activated", False),
                ),
                "INJECTION_ERROR",
            ),
            (
                "contradiction",
                mutate(
                    base,
                    ("runner", "status", "OUTPUT_MISMATCH"),
                    ("functional_outcome", "status", "OUTPUT_MISMATCH"),
                    ("activation", "activated", False),
                ),
                "FACT_CONTRADICTION",
            ),
            (
                "not activated",
                mutate(base, ("activation", "activated", False)),
                "NOT_ACTIVATED",
            ),
            (
                "detected hang",
                mutate(
                    base,
                    ("runner", "status", "TIMEOUT"),
                    ("functional_outcome", "status", "TIMEOUT"),
                    ("runner", "strict_signature_valid", False),
                ),
                "DETECTED_HANG",
            ),
            (
                "detected corruption",
                mutate(
                    base,
                    ("runner", "status", "OUTPUT_MISMATCH"),
                    ("functional_outcome", "status", "OUTPUT_MISMATCH"),
                    ("runner", "strict_signature_valid", False),
                ),
                "DETECTED_OUTPUT_CORRUPTION",
            ),
            (
                "receiver propagation masked",
                mutate(base, ("divergence", "receiver_diverged", True)),
                "LOCAL_PROPAGATION_MASKED_AT_OUTPUT",
            ),
            (
                "site divergence only",
                mutate(base, ("divergence", "site_diverged", True)),
                "SITE_DIVERGENCE_LOCALLY_MASKED",
            ),
            (
                "functionally equivalent",
                base,
                "FUNCTIONALLY_EQUIVALENT_UNDER_WORKLOAD",
            ),
            (
                "runner error beats malformed trace",
                mutate(
                    base,
                    ("runner", "valid_fault_run", False),
                    ("runner", "status", "UNKNOWN"),
                    ("trace_validity", "golden_valid", False),
                    ("trace_validity", "fault_valid", False),
                ),
                "SIMULATION_ERROR_OR_UNKNOWN",
            ),
            (
                "trace invalid beats scope mismatch",
                mutate(
                    base,
                    ("trace_validity", "golden_valid", False),
                    ("scope_alignment", "common_scope_count", 0),
                ),
                "TRACE_INVALID",
            ),
            (
                "injection error beats detected hang",
                mutate(
                    base,
                    ("runner", "status", "TIMEOUT"),
                    ("functional_outcome", "status", "TIMEOUT"),
                    ("injection", "effective", False),
                ),
                "INJECTION_ERROR",
            ),
        ]
        for index, (label, facts, expected) in enumerate(cases, start=1):
            actual = module.classify(facts, policy)
            if actual.primary_class != expected:
                raise SelfTestError(
                    f"scenario {index} {label}: expected={expected}, "
                    f"actual={actual.primary_class}"
                )
            if actual.priority_index != policy["classification_priority"].index(expected):
                raise SelfTestError(f"scenario {index} priority index mismatch")
            print(f"Scenario {index:02d} {label:37s}: PASS ({expected})")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("Stage-5 oracle semantics self-test        : PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
