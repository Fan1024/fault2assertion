#!/usr/bin/env python3
"""Fail-closed validation for a Stage-5 v2 diagnostic oracle.

The validator performs three independent checks:
1. every source path and SHA recorded in raw provenance still matches;
2. the pure semantic classifier reproduces the stored label;
3. the analyzer re-parses the source traces/result and reproduces all raw facts,
   diagnostic candidates, and durable oracle fields.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence
from datetime import datetime, timezone


class ValidationError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid {label} JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError(f"{label} must contain one JSON object")
    return payload


def load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path.resolve())
    if spec is None or spec.loader is None:
        raise ValidationError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def forbidden_semantic_keys(value: Any, path: str = "raw_facts") -> list[str]:
    errors: list[str] = []
    forbidden = {
        "semantic_classification",
        "characterization_class",
        "primary_class",
        "priority_index",
    }
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) in forbidden:
                errors.append(f"{path}.{key}")
            errors.extend(forbidden_semantic_keys(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            errors.extend(forbidden_semantic_keys(item, f"{path}[{index}]"))
    return errors


def compare_source(
    errors: list[str],
    provenance: Mapping[str, Any],
    path_key: str,
    sha_key: str,
    expected_path: Path | None = None,
) -> Path | None:
    raw_path = provenance.get(path_key)
    if not isinstance(raw_path, str) or not raw_path:
        errors.append(f"provenance path missing: {path_key}")
        return None
    path = Path(raw_path).resolve()
    if expected_path is not None and path != expected_path.resolve():
        errors.append(
            f"provenance path mismatch: {path_key}: expected={expected_path.resolve()}, actual={path}"
        )
    if not path.is_file():
        errors.append(f"provenance source missing: {path_key}: {path}")
        return path
    expected_sha = provenance.get(sha_key)
    actual_sha = sha256_file(path)
    if expected_sha != actual_sha:
        errors.append(
            f"provenance SHA mismatch: {sha_key}: expected={expected_sha}, actual={actual_sha}"
        )
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oracle", type=Path, required=True)
    parser.add_argument("--fault-json", type=Path, required=True)
    parser.add_argument("--analyzer", type=Path, required=True)
    parser.add_argument("--semantics", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)

    errors: list[str] = []
    try:
        oracle_path = args.oracle.resolve()
        fault_path = args.fault_json.resolve()
        analyzer_path = args.analyzer.resolve()
        semantics_path = args.semantics.resolve()
        policy_path = args.policy.resolve()
        oracle = load_json(oracle_path, "oracle")
        fault = load_json(fault_path, "fault spec")
        semantics_module = load_module(
            semantics_path, "f2a_oracle_semantics_validator"
        )
        analyzer_module = load_module(analyzer_path, "f2a_oracle_analyzer_validator")
        policy = semantics_module.load_policy(policy_path)

        if oracle.get("stage") != "stage_05_diagnostic_oracle_v2":
            errors.append("oracle stage marker mismatch")
        if oracle.get("schema_version") != "2.0":
            errors.append("oracle schema version mismatch")
        if oracle.get("program_version") != "2.0.0":
            errors.append("oracle program version mismatch")
        if oracle.get("fault_id") != fault.get("fault_id"):
            errors.append("oracle/fault ID mismatch")
        if oracle.get("selection_id") != fault.get("selection_id"):
            errors.append("oracle/fault selection ID mismatch")

        raw = oracle.get("raw_facts")
        semantic = oracle.get("semantic_classification")
        if not isinstance(raw, dict):
            errors.append("oracle raw_facts missing or invalid")
            raw = {}
        if not isinstance(semantic, dict):
            errors.append("oracle semantic_classification missing or invalid")
            semantic = {}

        semantic_leaks = forbidden_semantic_keys(raw)
        if semantic_leaks:
            errors.append(
                "semantic labels leaked into raw_facts: "
                + ", ".join(semantic_leaks[:10])
            )

        required_groups = policy["raw_fact_policy"]["required_groups"]
        for group in required_groups:
            if group not in raw:
                errors.append(f"raw_facts missing required group: {group}")

        provenance = raw.get("provenance")
        if not isinstance(provenance, dict):
            errors.append("raw provenance missing")
            provenance = {}

        fault_source = compare_source(
            errors,
            provenance,
            "fault_spec",
            "fault_spec_sha256",
            fault_path,
        )
        golden_source = compare_source(
            errors,
            provenance,
            "golden_trace",
            "golden_trace_sha256",
        )
        fault_trace_source = compare_source(
            errors,
            provenance,
            "fault_trace",
            "fault_trace_sha256",
        )
        result_source = compare_source(
            errors,
            provenance,
            "runner_result",
            "runner_result_sha256",
        )
        log_source = compare_source(
            errors,
            provenance,
            "xrun_log",
            "xrun_log_sha256",
        )
        compare_source(
            errors,
            provenance,
            "semantics_policy",
            "semantics_policy_sha256",
            policy_path,
        )
        compare_source(
            errors,
            provenance,
            "semantics_implementation",
            "semantics_implementation_sha256",
            semantics_path,
        )
        compare_source(
            errors,
            provenance,
            "oracle_analyzer",
            "oracle_analyzer_sha256",
            analyzer_path,
        )

        if provenance.get("fault_spec_digest_sha256") != fault.get(
            "fault_spec_digest_sha256"
        ):
            errors.append("fault-spec canonical digest mismatch in oracle")

        try:
            rebuilt_classification = semantics_module.classify(raw, policy)
            expected_semantic = {
                "primary_class": rebuilt_classification.primary_class,
                "priority_index": rebuilt_classification.priority_index,
                "reason": rebuilt_classification.reason,
                "semantics_version": rebuilt_classification.semantics_version,
            }
            if semantic != expected_semantic:
                errors.append("stored semantic classification is not reproducible")
        except Exception as exc:
            errors.append(f"semantic reclassification failed: {exc}")

        if all(
            path is not None and path.is_file()
            for path in (
                fault_source,
                golden_source,
                fault_trace_source,
                result_source,
                log_source,
            )
        ):
            try:
                rebuilt = analyzer_module.analyze(
                    SimpleNamespace(
                        fault_json=fault_source,
                        golden_trace=golden_source,
                        fault_trace=fault_trace_source,
                        result_json=result_source,
                        xrun_log=log_source,
                        semantics=semantics_path,
                        policy=policy_path,
                    )
                )
                durable_keys = {
                    key
                    for key in oracle
                    if key not in {"generated_at_utc", "oracle_digest_sha256"}
                }
                rebuilt_keys = {
                    key
                    for key in rebuilt
                    if key not in {"generated_at_utc", "oracle_digest_sha256"}
                }
                if durable_keys != rebuilt_keys:
                    errors.append(
                        "re-analyzed oracle durable key set differs from stored oracle"
                    )
                else:
                    for key in sorted(durable_keys):
                        if oracle.get(key) != rebuilt.get(key):
                            errors.append(
                                f"re-analysis mismatch in durable oracle field: {key}"
                            )
            except Exception as exc:
                errors.append(f"full oracle re-analysis failed: {exc}")
        else:
            errors.append("full oracle re-analysis skipped because a source is missing")

        rebuilt_digest = canonical_json_digest(
            {
                key: value
                for key, value in oracle.items()
                if key not in {"generated_at_utc", "oracle_digest_sha256"}
            }
        )
        if oracle.get("oracle_digest_sha256") != rebuilt_digest:
            errors.append("oracle digest mismatch")

        diagnostic = oracle.get("diagnostic_oracle")
        if not isinstance(diagnostic, dict):
            errors.append("diagnostic_oracle missing")
        else:
            candidate_count = diagnostic.get("candidate_count")
            preview = diagnostic.get("candidate_preview")
            if not isinstance(candidate_count, int) or candidate_count < 0:
                errors.append("invalid diagnostic candidate_count")
            if not isinstance(preview, list):
                errors.append("invalid diagnostic candidate_preview")
            elif isinstance(candidate_count, int) and len(preview) > min(
                20, candidate_count
            ):
                errors.append("diagnostic preview exceeds candidate count/limit")

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    report = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "kind": "stage5_oracle_v2_validation",
        "oracle": str(args.oracle.resolve()),
        "oracle_sha256": sha256_file(args.oracle.resolve()),
        "analyzer": str(args.analyzer.resolve()),
        "analyzer_sha256": sha256_file(args.analyzer.resolve()),
        "policy": str(args.policy.resolve()),
        "policy_sha256": sha256_file(args.policy.resolve()),
        "semantics": str(args.semantics.resolve()),
        "semantics_sha256": sha256_file(args.semantics.resolve()),
        "full_source_reanalysis_performed": not any(
            error.startswith("full oracle re-analysis") for error in errors
        ),
        "error_count": len(errors),
        "errors": errors,
        "status": "PASS" if not errors else "FAIL",
    }
    output = args.report.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"Oracle validation errors              : {len(errors)}")
    print(f"Oracle validation result              : {report['status']}")
    print(f"Full source re-analysis                : {report['full_source_reanalysis_performed']}")
    print(f"Oracle validation report              : {output}")
    if errors:
        for error in errors[:30]:
            print(f"  - {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
