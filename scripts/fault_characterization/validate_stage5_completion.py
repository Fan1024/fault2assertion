#!/usr/bin/env python3
"""Strict completion validator for Fault2Assertion Stage 5.

This validator intentionally distinguishes Stage-5 preparation from Stage-5
completion. It returns success only when every campaign fault has a current,
self-consistent diagnostic oracle and all durable/cleanup artifacts satisfy the
Stage-5 contract.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

CAMPAIGN_STAGE = "stage_05_fault_characterization_campaign"
FAULT_STAGE = "stage_05_fault_materialization"
ORACLE_STAGE = "stage_05_diagnostic_oracle"
ALLOWED_CHARACTERIZATIONS = {
    "TRACE_SCOPE_MISMATCH",
    "NOT_ACTIVATED",
    "INJECTION_ERROR",
    "DETECTED_HANG",
    "DETECTED_OUTPUT_CORRUPTION",
    "ARCHITECTURALLY_MASKED_AFTER_LOCAL_PROPAGATION",
    "LOCALLY_MASKED_AFTER_SITE_DIVERGENCE",
    "FUNCTIONALLY_EQUIVALENT_UNDER_WORKLOAD",
}
ALLOWED_ORACLE_KINDS = {
    "earliest_cycle_local_divergence",
    "functional_outcome_oracle",
}
FORBIDDEN_NAMES = {
    "fault_netlist.v",
    "cv32e40p.mapped.sim.v",
}
FORBIDDEN_SUFFIXES = (
    ".vcd",
    ".trace.tsv",
)


class ValidationError(RuntimeError):
    pass


def load_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise ValidationError(f"missing {label}: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid {label} JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be a JSON object: {path}")
    return value


def canonical_json_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def require_nonempty(path: Path, label: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValidationError(f"missing or empty {label}: {path}")


def collect_forbidden_files(roots: Iterable[Path]) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.name in FORBIDDEN_NAMES or path.name.endswith(FORBIDDEN_SUFFIXES):
                found.append(path)
    return sorted(found)


def validate_oracle(
    *,
    record: dict[str, Any],
    spec: dict[str, Any],
    oracle: dict[str, Any],
    oracle_path: Path,
) -> None:
    fault_id = str(record["fault_id"])
    if oracle.get("stage") != ORACLE_STAGE:
        raise ValidationError(f"oracle stage mismatch for {fault_id}: {oracle_path}")
    for key, expected in (
        ("fault_id", fault_id),
        ("selection_id", str(record["selection_id"])),
        ("site_id", str(record["site_id"])),
        ("polarity", str(record["polarity"])),
        ("stuck_at", int(record["stuck_at"])),
        ("fault_spec_digest_sha256", str(record["fault_spec_digest_sha256"])),
    ):
        if oracle.get(key) != expected:
            raise ValidationError(
                f"oracle {key} mismatch for {fault_id}: "
                f"expected={expected!r}, actual={oracle.get(key)!r}"
            )
    if spec.get("fault_spec_digest_sha256") != record.get(
        "fault_spec_digest_sha256"
    ):
        raise ValidationError(f"campaign/spec digest mismatch for {fault_id}")

    characterization = oracle.get("characterization_class")
    if characterization not in ALLOWED_CHARACTERIZATIONS:
        raise ValidationError(
            f"unknown characterization for {fault_id}: {characterization!r}"
        )

    local = oracle.get("local_characterization")
    diagnostic = oracle.get("diagnostic_oracle")
    functional = oracle.get("functional_result")
    if not isinstance(local, dict):
        raise ValidationError(f"missing local_characterization for {fault_id}")
    if not isinstance(diagnostic, dict):
        raise ValidationError(f"missing diagnostic_oracle for {fault_id}")
    if not isinstance(functional, dict) or not functional.get("classification"):
        raise ValidationError(f"missing functional_result for {fault_id}")

    oracle_kind = diagnostic.get("oracle_kind")
    if oracle_kind not in ALLOWED_ORACLE_KINDS:
        raise ValidationError(f"invalid oracle_kind for {fault_id}: {oracle_kind!r}")
    candidate_count = diagnostic.get("candidate_count")
    if not isinstance(candidate_count, int) or candidate_count < 0:
        raise ValidationError(f"invalid candidate_count for {fault_id}")
    if oracle_kind == "earliest_cycle_local_divergence":
        if candidate_count <= 0:
            raise ValidationError(
                f"local-divergence oracle has no candidate for {fault_id}"
            )
        if diagnostic.get("earliest_cycle") is None:
            raise ValidationError(
                f"local-divergence oracle lacks earliest_cycle for {fault_id}"
            )
        if not diagnostic.get("scope") or not diagnostic.get("expression"):
            raise ValidationError(
                f"local-divergence oracle lacks scope/expression for {fault_id}"
            )
    else:
        if candidate_count != 0:
            raise ValidationError(
                f"functional-outcome oracle unexpectedly has candidates for {fault_id}"
            )

    stored_digest = oracle.get("oracle_digest_sha256")
    if not isinstance(stored_digest, str) or len(stored_digest) != 64:
        raise ValidationError(f"invalid oracle digest for {fault_id}")
    digest_payload = {
        key: value
        for key, value in oracle.items()
        if key not in {"generated_at_utc", "oracle_digest_sha256"}
    }
    recomputed = canonical_json_digest(digest_payload)
    if recomputed != stored_digest:
        raise ValidationError(
            f"oracle content digest mismatch for {fault_id}: "
            f"stored={stored_digest}, recomputed={recomputed}"
        )

    storage = oracle.get("storage_confirmation")
    if not isinstance(storage, dict):
        raise ValidationError(f"missing storage_confirmation for {fault_id}")
    if storage.get("faulty_netlist_retained") is not False:
        raise ValidationError(f"oracle claims retained faulty netlist: {fault_id}")
    if storage.get("vcd_retained") is not False:
        raise ValidationError(f"oracle claims retained VCD: {fault_id}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Strictly validate complete Fault2Assertion Stage-5 outputs"
    )
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument(
        "--run-root",
        type=Path,
        required=True,
        help="Stage-5 scratch root used by run_stage5_campaign.py",
    )
    parser.add_argument(
        "--allow-golden-cache",
        action="store_true",
        help="allow a retained golden_cache directory after a partial/smoke run",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    campaign_path = args.campaign.resolve()
    campaign_root = campaign_path.parent
    run_root = args.run_root.resolve()
    campaign = load_object(campaign_path, "Stage-5 campaign")
    if campaign.get("stage") != CAMPAIGN_STAGE:
        raise ValidationError(
            f"campaign stage mismatch: {campaign.get('stage')!r}"
        )
    faults = campaign.get("faults")
    if not isinstance(faults, list) or not faults:
        raise ValidationError("campaign contains no fault records")
    expected = len(faults)
    fault_ids = [str(item.get("fault_id")) for item in faults]
    if len(set(fault_ids)) != expected:
        raise ValidationError("campaign fault IDs are duplicated")

    summary = campaign.get("campaign_summary")
    if not isinstance(summary, dict):
        raise ValidationError("campaign_summary missing")
    if int(summary.get("fault_instance_count", -1)) != expected:
        raise ValidationError("campaign fault_instance_count mismatch")

    oracle_dir = campaign_root / "oracles"
    report_dir = campaign_root / "reports"
    sva_dir = campaign_root / "sva_seeds"
    summary_dir = campaign_root / "summary"

    characterization_counts: Counter[str] = Counter()
    oracle_kind_counts: Counter[str] = Counter()
    for record in faults:
        fault_id = str(record["fault_id"])
        spec_path = Path(str(record["fault_spec"])).resolve()
        patch_path = Path(str(record["patch"])).resolve()
        oracle_path = oracle_dir / f"{fault_id}.json"
        report_path = report_dir / f"{fault_id}.txt"
        sva_path = sva_dir / f"{fault_id}.sva"

        spec = load_object(spec_path, f"fault spec {fault_id}")
        if spec.get("stage") != FAULT_STAGE:
            raise ValidationError(f"fault-spec stage mismatch for {fault_id}")
        if spec.get("fault_id") != fault_id:
            raise ValidationError(f"fault-spec ID mismatch for {fault_id}")
        require_nonempty(patch_path, f"fault description {fault_id}")
        oracle = load_object(oracle_path, f"oracle {fault_id}")
        validate_oracle(
            record=record,
            spec=spec,
            oracle=oracle,
            oracle_path=oracle_path,
        )
        require_nonempty(report_path, f"characterization report {fault_id}")
        require_nonempty(sva_path, f"SVA seed {fault_id}")
        characterization_counts[str(oracle["characterization_class"])] += 1
        oracle_kind_counts[str(oracle["diagnostic_oracle"]["oracle_kind"])] += 1

    jsonl_path = summary_dir / "stage_05_oracles.jsonl"
    csv_path = summary_dir / "stage_05_summary.csv"
    report_path = summary_dir / "stage_05_report.txt"
    require_nonempty(jsonl_path, "aggregated oracle JSONL")
    require_nonempty(csv_path, "Stage-5 summary CSV")
    require_nonempty(report_path, "Stage-5 campaign report")

    jsonl_records = [
        json.loads(line)
        for line in jsonl_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(jsonl_records) != expected:
        raise ValidationError(
            f"aggregated JSONL count mismatch: expected={expected}, "
            f"actual={len(jsonl_records)}"
        )
    if {str(item.get("fault_id")) for item in jsonl_records} != set(fault_ids):
        raise ValidationError("aggregated JSONL fault-ID set mismatch")

    with csv_path.open(newline="", encoding="utf-8") as handle:
        csv_rows = list(csv.DictReader(handle))
    if len(csv_rows) != expected:
        raise ValidationError(
            f"summary CSV row count mismatch: expected={expected}, actual={len(csv_rows)}"
        )
    if {str(item.get("fault_id")) for item in csv_rows} != set(fault_ids):
        raise ValidationError("summary CSV fault-ID set mismatch")

    report_text = report_path.read_text(encoding="utf-8")
    required_report_lines = (
        f"Expected fault instances: {expected}",
        f"Generated oracles       : {expected}",
        "Missing oracles         : 0",
        "Campaign complete       : True",
    )
    for line in required_report_lines:
        if line not in report_text:
            raise ValidationError(
                f"campaign report lacks completion marker: {line!r}"
            )

    forbidden = collect_forbidden_files((campaign_root, run_root))
    if forbidden:
        preview = "\n".join(f"  - {path}" for path in forbidden[:30])
        raise ValidationError(
            f"temporary Stage-5 files remain ({len(forbidden)}):\n{preview}"
        )
    golden_cache = run_root / "golden_cache"
    if golden_cache.exists() and not args.allow_golden_cache:
        remaining = [path for path in golden_cache.rglob("*") if path.is_file()]
        if remaining:
            raise ValidationError(
                "golden cache is still retained; rerun without "
                "--keep-golden-cache or remove it after campaign completion"
            )

    print("Fault2Assertion Stage-5 Completion Validation")
    print("=" * 72)
    print(f"Campaign                 : {campaign_path}")
    print(f"Expected faults          : {expected}")
    print(f"Validated fault specs    : {expected}")
    print(f"Validated oracles        : {expected}")
    print(f"Validated reports        : {expected}")
    print(f"Validated SVA seeds      : {expected}")
    print(f"Aggregated JSONL records : {len(jsonl_records)}")
    print(f"Summary CSV rows         : {len(csv_rows)}")
    print(f"Characterization classes: {dict(sorted(characterization_counts.items()))}")
    print(f"Oracle kinds             : {dict(sorted(oracle_kind_counts.items()))}")
    print("Retained VCD files       : 0")
    print("Retained faulty netlists : 0")
    print("Missing oracles          : 0")
    print("Stage-5 completion       : PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValidationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
