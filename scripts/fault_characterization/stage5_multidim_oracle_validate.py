#!/usr/bin/env python3
"""Replay and validate a Stage-5 multidimensional fault oracle."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence


class ValidationError(RuntimeError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValidationError("oracle must be one JSON object")
    return value


def import_analyzer(path: Path):
    spec = importlib.util.spec_from_file_location("f2a_multidim_oracle_replay", path.resolve())
    if spec is None or spec.loader is None:
        raise ValidationError(f"cannot import analyzer: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oracle", type=Path, required=True)
    parser.add_argument("--analyzer", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        oracle_path = args.oracle.resolve()
        oracle = load_json(oracle_path)
        analyzer = import_analyzer(args.analyzer)
        if oracle.get("schema_version") != analyzer.SCHEMA_VERSION:
            raise ValidationError("schema version mismatch")
        if oracle.get("program_version") != analyzer.PROGRAM_VERSION:
            raise ValidationError("program version mismatch")
        if oracle.get("stage") != analyzer.ORACLE_STAGE:
            raise ValidationError("oracle stage marker mismatch")
        stored_digest = oracle.get("oracle_digest_sha256")
        recomputed_stored_digest = analyzer.canonical_digest(
            {
                key: value
                for key, value in oracle.items()
                if key not in {"generated_at_utc", "oracle_digest_sha256"}
            }
        )
        if stored_digest != recomputed_stored_digest:
            raise ValidationError(
                "stored oracle content digest mismatch: "
                f"stored={stored_digest}, recomputed={recomputed_stored_digest}"
            )
        identity = oracle.get("identity")
        provenance = oracle.get("provenance")
        if not isinstance(identity, dict) or not isinstance(provenance, dict):
            raise ValidationError("identity/provenance missing")
        modes = provenance.get("execution_modes")
        if not isinstance(modes, dict):
            raise ValidationError("execution mode provenance missing")
        required_modes = {"native", "observe", "diagnostic_quarantine"}
        if set(modes) != required_modes:
            raise ValidationError(f"unexpected execution modes: {sorted(modes)}")

        namespace = SimpleNamespace(
            fault_json=Path(provenance["fault_spec"]),
            assertion_policy=Path(provenance["assertion_policy"]),
            golden_trace=Path(provenance["golden_trace"]),
            native_run=Path(modes["native"]["run_directory"]),
            native_trace=Path(modes["native"]["trace"]),
            observe_run=Path(modes["observe"]["run_directory"]),
            observe_trace=Path(modes["observe"]["trace"]),
            quarantine_run=Path(modes["diagnostic_quarantine"]["run_directory"]),
            quarantine_trace=Path(modes["diagnostic_quarantine"]["trace"]),
        )
        rebuilt = analyzer.build_oracle(namespace)
        if rebuilt.get("oracle_digest_sha256") != oracle.get("oracle_digest_sha256"):
            raise ValidationError(
                "oracle replay digest mismatch: "
                f"stored={oracle.get('oracle_digest_sha256')}, "
                f"rebuilt={rebuilt.get('oracle_digest_sha256')}"
            )

        dimensions = oracle.get("dimensions", {})
        raw = oracle.get("raw_facts", {})
        guardrails = oracle.get("guardrails", {})
        if dimensions.get("execution_validity") != "VALID":
            raise ValidationError("oracle execution validity is not VALID")
        if dimensions.get("activation_class") != "ACTIVATED":
            raise ValidationError("smoke oracle fault was not activated")
        if dimensions.get("injection_class") != "EFFECTIVE":
            raise ValidationError("smoke oracle injection is not effective")
        if "ILLEGAL_MEMORY_WRITE" not in dimensions.get("effect_classes", []):
            raise ValidationError("expected illegal-memory-write effect missing")
        if dimensions.get("propagation_class") != "ARCHITECTURAL_INTERFACE_REACHED":
            raise ValidationError("expected interface propagation missing")
        if raw.get("mode_consistency", {}).get("first_detector_event_equal") is not True:
            raise ValidationError("detector events are not mode-consistent")
        if guardrails.get("quarantine_outcome_is_not_natural_architectural_outcome") is not True:
            raise ValidationError("quarantine interpretation guardrail missing")
        if guardrails.get("sva_seed_generated") is not False:
            raise ValidationError("oracle stage generated an SVA seed")
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1

    print(f"Oracle              : {oracle_path}")
    print(f"Fault ID            : {identity['fault_id']}")
    print(f"Replay digest       : {oracle['oracle_digest_sha256']}")
    print("Multidimensional oracle validation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
