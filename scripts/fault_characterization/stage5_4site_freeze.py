#!/usr/bin/env python3
"""Validate and freeze the completed Stage-5 four-site micro batch.

This tool does not run simulation and does not impose storage limits. It checks
that the already prepared four-site batch completed through the existing generic
Stage-5 path, that every selected fault oracle passed independent validation,
and that validated temporary work was cleaned. On PASS it writes one compact,
durable checkpoint containing the experiment summary and SHA-256 evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = "1.0"
PROGRAM_VERSION = "1.0.0"
EXPECTED_SITE_CLASSES = {
    "sequential_state",
    "control_path",
    "architectural_data",
    "generic_observable",
}
FINAL_STATE = "ORACLE_VALIDATED_CLEANED"
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


class FreezeError(RuntimeError):
    """Controlled micro-batch validation failure."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FreezeError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise FreezeError(f"invalid {label} JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise FreezeError(f"{label} must contain one JSON object: {path}")
    return value


def require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise FreezeError(f"{label} must be an array")
    return value


def write_json(path: Path, value: Mapping[str, Any], *, overwrite: bool) -> None:
    path = path.expanduser().resolve()
    if path.exists() and not overwrite:
        raise FreezeError(f"refusing to overwrite existing checkpoint: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


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


def directory_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except FileNotFoundError:
            continue
    return total


def human_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024.0 or unit == "TiB":
            return f"{amount:.2f} {unit}"
        amount /= 1024.0
    return f"{value} B"


def resolve_record_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise FreezeError(f"{label} must be one non-empty path string")
    return Path(value).expanduser().resolve()


def ensure_under(path: Path, root: Path, label: str) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise FreezeError(f"{label} escapes expected root: {path}") from exc


def file_evidence(path: Path, root: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FreezeError(f"evidence file not found: {path}")
    try:
        display = str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        display = str(path.resolve())
    return {
        "path": display,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def first_existing(paths: Iterable[Path]) -> Path | None:
    for path in paths:
        if path.is_file():
            return path
    return None


def validate_plan(plan: Mapping[str, Any]) -> tuple[list[str], list[str], str]:
    if plan.get("kind") != "stage5_nested_unique_site_batch_plan":
        raise FreezeError(f"unexpected plan kind: {plan.get('kind')!r}")
    if plan.get("requested_unique_site_count") != 4:
        raise FreezeError("plan requested_unique_site_count is not 4")
    if plan.get("selected_unique_site_count") != 4:
        raise FreezeError("plan selected_unique_site_count is not 4")
    if plan.get("selected_fault_instance_count") != 8:
        raise FreezeError("four-site plan must contain exactly 8 fault instances")
    if plan.get("reference_site_included") is not True:
        raise FreezeError("plan does not affirm reference-site inclusion")

    contract = plan.get("selection_contract")
    if not isinstance(contract, dict):
        raise FreezeError("plan has no selection_contract object")
    required_true = (
        "canonical_ids_preserved",
        "all_stage4_fault_instances_per_site_preserved",
        "first_site_is_reference",
        "first_four_sites_cover_primary_classes_when_available",
        "larger_batches_are_prefixes_of_one_frozen_site_order",
    )
    for key in required_true:
        if contract.get(key) is not True:
            raise FreezeError(f"selection contract is not frozen true: {key}")
    if contract.get("random_seed_used") is not False:
        raise FreezeError("four-site scientific plan unexpectedly uses a seed")

    sites = require_list(plan.get("sites"), "plan sites")
    if len(sites) != 4:
        raise FreezeError(f"plan site array length is not 4: {len(sites)}")

    selection_ids: list[str] = []
    fault_ids: list[str] = []
    classes: set[str] = set()
    reference_fault = str(plan.get("reference_fault_id", ""))
    if not reference_fault:
        raise FreezeError("plan has no reference_fault_id")

    for expected_order, raw_site in enumerate(sites, start=1):
        if not isinstance(raw_site, dict):
            raise FreezeError("plan contains a non-object site record")
        if raw_site.get("order") != expected_order:
            raise FreezeError(
                f"plan site order mismatch: expected {expected_order}, "
                f"got {raw_site.get('order')!r}"
            )
        selection_id = str(raw_site.get("selection_id", ""))
        if not selection_id:
            raise FreezeError("plan site has no selection_id")
        selection_ids.append(selection_id)
        classes.add(str(raw_site.get("fault_class", "")))

        site_faults = require_list(
            raw_site.get("faults"), f"plan faults for {selection_id}"
        )
        if len(site_faults) != 2:
            raise FreezeError(
                f"site {selection_id} must contain exactly SA0 and SA1"
            )
        site_polarities: set[str] = set()
        site_stuck_at: set[int] = set()
        for raw_fault in site_faults:
            if not isinstance(raw_fault, dict):
                raise FreezeError(f"site {selection_id} has invalid fault record")
            fault_id = str(raw_fault.get("fault_id", ""))
            if not fault_id:
                raise FreezeError(f"site {selection_id} has empty fault ID")
            fault_ids.append(fault_id)
            site_polarities.add(str(raw_fault.get("polarity", "")))
            stuck_at = raw_fault.get("stuck_at")
            if stuck_at not in (0, 1):
                raise FreezeError(f"invalid stuck-at value for {fault_id}: {stuck_at}")
            site_stuck_at.add(int(stuck_at))
        if site_polarities != {"SA0", "SA1"} or site_stuck_at != {0, 1}:
            raise FreezeError(
                f"site {selection_id} does not preserve dual polarity"
            )

    if len(set(selection_ids)) != 4:
        raise FreezeError("plan selection IDs are not unique")
    if len(set(fault_ids)) != 8:
        raise FreezeError("plan fault IDs are not unique")
    if classes != EXPECTED_SITE_CLASSES:
        raise FreezeError(
            "four-site plan does not cover exactly the four primary classes: "
            f"{sorted(classes)}"
        )
    if sites[0].get("is_reference_site") is not True:
        raise FreezeError("first plan site is not marked as the reference site")
    first_faults = {
        str(item.get("fault_id"))
        for item in require_list(sites[0].get("faults"), "reference site faults")
        if isinstance(item, dict)
    }
    if reference_fault not in first_faults:
        raise FreezeError("reference fault is not in the first plan site")
    return selection_ids, fault_ids, reference_fault


def validate_batch(
    root: Path,
    plan_root: Path,
    batch_root: Path,
) -> tuple[dict[str, Any], list[Path]]:
    plan_path = plan_root / "batch_plan.json"
    manifest_path = batch_root / "pilot_manifest.json"
    qualification_path = batch_root / "reference_qualification.json"
    status_summary_path = batch_root / "pilot_status.json"
    storage_path = batch_root / "storage" / "latest.json"

    plan = load_json(plan_path, "four-site batch plan")
    selection_ids, planned_fault_ids, reference_fault = validate_plan(plan)
    planned_set = set(planned_fault_ids)

    manifest = load_json(manifest_path, "four-site pilot manifest")
    if manifest.get("kind") != "stage5_site_based_batch_pilot":
        raise FreezeError(f"unexpected pilot manifest kind: {manifest.get('kind')!r}")
    if manifest.get("selected_count") != 8:
        raise FreezeError("pilot manifest selected_count is not 8")
    distributions = manifest.get("distributions")
    if not isinstance(distributions, dict):
        raise FreezeError("pilot manifest has no distributions object")
    if distributions.get("site_count") != 4:
        raise FreezeError("pilot manifest site_count is not 4")
    if distributions.get("fault_count") != 8:
        raise FreezeError("pilot manifest fault_count is not 8")

    fault_records = require_list(manifest.get("faults"), "pilot manifest faults")
    if len(fault_records) != 8:
        raise FreezeError("pilot manifest must contain exactly 8 fault records")
    manifest_fault_ids = [
        str(item.get("fault_id", ""))
        for item in fault_records
        if isinstance(item, dict)
    ]
    if len(manifest_fault_ids) != 8 or set(manifest_fault_ids) != planned_set:
        missing = sorted(planned_set - set(manifest_fault_ids))
        extra = sorted(set(manifest_fault_ids) - planned_set)
        raise FreezeError(
            f"pilot fault set differs from plan; missing={missing}, extra={extra}"
        )

    qualification = load_json(qualification_path, "reference qualification")
    if qualification.get("status") != "PASS":
        raise FreezeError("reference qualification is not PASS")
    if qualification.get("canonical_fault_id") != reference_fault:
        raise FreezeError("qualified reference fault differs from the plan")
    if qualification.get("errors") not in ([], None):
        raise FreezeError("reference qualification contains errors")

    status_summary = load_json(status_summary_path, "pilot status summary")
    summary_counts = status_summary.get("counts")
    if not isinstance(summary_counts, dict):
        raise FreezeError("pilot status summary has no counts object")
    if summary_counts != {FINAL_STATE: 8}:
        raise FreezeError(
            f"pilot status is not fully complete: {summary_counts}"
        )

    storage = load_json(storage_path, "latest storage report")
    storage_states = storage.get("states")
    if storage_states != {FINAL_STATE: 8}:
        raise FreezeError(
            f"storage report state counts are not fully complete: {storage_states}"
        )
    if storage.get("work_bytes") != 0:
        raise FreezeError(
            f"validated batch still retains work bytes: {storage.get('work_bytes')}"
        )

    state_counts: Counter[str] = Counter()
    native_counts: Counter[str] = Counter()
    route_counts: Counter[str] = Counter()
    capability_counts: Counter[str] = Counter()
    class_counts: Counter[str] = Counter()
    polarity_counts: Counter[str] = Counter()
    per_fault: list[dict[str, Any]] = []
    evidence_paths: list[Path] = [
        plan_path,
        manifest_path,
        qualification_path,
        status_summary_path,
        storage_path,
    ]

    for raw_record in fault_records:
        if not isinstance(raw_record, dict):
            raise FreezeError("pilot manifest contains invalid fault record")
        fault_id = str(raw_record.get("fault_id", ""))
        fault_root = resolve_record_path(raw_record.get("fault_root"), "fault_root")
        ensure_under(fault_root, batch_root, f"fault root for {fault_id}")

        status_path = fault_root / "status.json"
        fault_json_path = fault_root / "fault.json"
        routing_path = fault_root / "routing.json"
        cleanup_path = fault_root / "cleanup.json"
        oracle_path = fault_root / "oracle" / "oracle.json"
        prompt_path = fault_root / "oracle" / "prompt_context.json"
        validation_path = fault_root / "oracle" / "validation.json"
        native_result_path = fault_root / "native" / "run" / "result.json"

        status = load_json(status_path, f"status for {fault_id}")
        fault_json = load_json(fault_json_path, f"fault spec for {fault_id}")
        routing = load_json(routing_path, f"routing for {fault_id}")
        cleanup = load_json(cleanup_path, f"cleanup report for {fault_id}")
        validation = load_json(validation_path, f"oracle validation for {fault_id}")
        native_result = load_json(native_result_path, f"Native result for {fault_id}")

        if fault_json.get("fault_id") != fault_id:
            raise FreezeError(f"fault spec ID mismatch for {fault_id}")
        if status.get("state") != FINAL_STATE:
            raise FreezeError(f"fault {fault_id} final state is {status.get('state')!r}")
        if status.get("work_retained") is not False:
            raise FreezeError(f"fault {fault_id} still marks work_retained")
        if validation.get("status") != "PASS":
            raise FreezeError(f"fault {fault_id} oracle validation is not PASS")
        if validation.get("fault_id") != fault_id:
            raise FreezeError(f"fault {fault_id} validation ID mismatch")
        if routing.get("fault_id") != fault_id:
            raise FreezeError(f"fault {fault_id} routing ID mismatch")

        native_status = str(status.get("native_status", ""))
        route = str(status.get("route", ""))
        if native_status not in NATIVE_STATUSES:
            raise FreezeError(
                f"fault {fault_id} has unsupported Native status {native_status!r}"
            )
        if native_result.get("status") != native_status:
            raise FreezeError(f"fault {fault_id} Native status does not replay")
        if validation.get("native_status") != native_status:
            raise FreezeError(f"fault {fault_id} validation Native status mismatch")
        if routing.get("native_status") != native_status:
            raise FreezeError(f"fault {fault_id} routing Native status mismatch")

        observe_status = status.get("observe_status")
        quarantine_status = status.get("diagnostic_quarantine_status")
        mode_evidence: list[Path] = []
        if route == "NATIVE_ONLY":
            if routing.get("route") != "NATIVE_ONLY":
                raise FreezeError(f"fault {fault_id} Native-only route mismatch")
            if observe_status is not None or quarantine_status is not None:
                raise FreezeError(
                    f"fault {fault_id} Native-only route has diagnostic statuses"
                )
            if validation.get("observe_status") is not None:
                raise FreezeError(f"fault {fault_id} validation unexpectedly has OBSERVE")
            if validation.get("diagnostic_quarantine_status") is not None:
                raise FreezeError(
                    f"fault {fault_id} validation unexpectedly has QUARANTINE"
                )
        elif route == "DIAGNOSTIC_THREE_MODE":
            if routing.get("route") != "DIAGNOSTIC_THREE_MODE":
                raise FreezeError(f"fault {fault_id} diagnostic route mismatch")
            if observe_status not in DIAGNOSTIC_STATUSES:
                raise FreezeError(f"fault {fault_id} invalid OBSERVE status")
            if quarantine_status not in DIAGNOSTIC_STATUSES:
                raise FreezeError(f"fault {fault_id} invalid QUARANTINE status")
            observe_result_path = fault_root / "observe" / "run" / "result.json"
            quarantine_result_path = (
                fault_root
                / "diagnostic_quarantine"
                / "run"
                / "result.json"
            )
            observe_result = load_json(
                observe_result_path, f"OBSERVE result for {fault_id}"
            )
            quarantine_result = load_json(
                quarantine_result_path, f"QUARANTINE result for {fault_id}"
            )
            if observe_result.get("status") != observe_status:
                raise FreezeError(f"fault {fault_id} OBSERVE status does not replay")
            if quarantine_result.get("status") != quarantine_status:
                raise FreezeError(f"fault {fault_id} QUARANTINE status does not replay")
            if validation.get("observe_status") != observe_status:
                raise FreezeError(f"fault {fault_id} validation OBSERVE mismatch")
            if validation.get("diagnostic_quarantine_status") != quarantine_status:
                raise FreezeError(f"fault {fault_id} validation QUARANTINE mismatch")
            mode_evidence.extend([observe_result_path, quarantine_result_path])
        else:
            raise FreezeError(f"fault {fault_id} unsupported final route: {route!r}")

        capability = status.get("validated_capability")
        if validation.get("validated_capability") != capability:
            raise FreezeError(f"fault {fault_id} validated capability mismatch")
        if cleanup.get("cleanup_condition") != "ORACLE_INDEPENDENT_VALIDATION_PASS":
            raise FreezeError(f"fault {fault_id} cleanup condition is invalid")
        if cleanup.get("vcd_retained") is not False:
            raise FreezeError(f"fault {fault_id} cleanup retained a VCD")

        remaining_work = [
            path for path in fault_root.glob("**/run/work") if path.is_dir()
        ]
        if remaining_work:
            raise FreezeError(
                f"fault {fault_id} retains work directories: {remaining_work}"
            )
        retained_vcd = [path for path in fault_root.rglob("*.vcd") if path.is_file()]
        if retained_vcd:
            raise FreezeError(f"fault {fault_id} retains VCD files: {retained_vcd}")
        retained_netlists = [
            path
            for path in fault_root.rglob("fault_netlist.v")
            if path.is_file()
        ]
        if retained_netlists:
            raise FreezeError(
                f"fault {fault_id} retains run-local fault netlists: {retained_netlists}"
            )

        state_counts[str(status.get("state"))] += 1
        native_counts[native_status] += 1
        route_counts[route] += 1
        capability_counts[str(capability)] += 1
        class_counts[str(raw_record.get("fault_class"))] += 1
        polarity_counts[str(raw_record.get("polarity_directory"))] += 1
        per_fault.append(
            {
                "order": raw_record.get("order"),
                "fault_id": fault_id,
                "base_fault_id": raw_record.get("base_fault_id"),
                "fault_class": raw_record.get("fault_class"),
                "polarity": raw_record.get("polarity_directory"),
                "state": status.get("state"),
                "native_status": native_status,
                "route": route,
                "observe_status": observe_status,
                "diagnostic_quarantine_status": quarantine_status,
                "validated_capability": capability,
                "bytes_freed": cleanup.get("bytes_freed"),
            }
        )
        evidence_paths.extend(
            [
                fault_json_path,
                status_path,
                routing_path,
                cleanup_path,
                oracle_path,
                prompt_path,
                validation_path,
                native_result_path,
                *mode_evidence,
            ]
        )

    if state_counts != Counter({FINAL_STATE: 8}):
        raise FreezeError(f"unexpected final state counts: {dict(state_counts)}")

    forbidden_batch_files = [
        path
        for pattern in ("*.vcd", "fault_netlist.v")
        for path in batch_root.rglob(pattern)
        if path.is_file()
    ]
    if forbidden_batch_files:
        raise FreezeError(
            f"batch retains forbidden temporary files: {forbidden_batch_files}"
        )
    retained_batch_work = [
        path for path in batch_root.glob("sites/*/SA*/**/run/work") if path.is_dir()
    ]
    if retained_batch_work:
        raise FreezeError(
            f"batch retains validated work directories: {retained_batch_work}"
        )

    # Record the exact code and policy interface used by the generic path.
    interface_candidates = [
        root / "scripts/fault_characterization/stage5_batch.py",
        root / "scripts/fault_characterization/stage5_batch_control.py",
        root / "scripts/fault_characterization/stage5_batch_oracle.py",
        root / "scripts/fault_characterization/stage5_batch_oracle_validate.py",
        root / "scripts/fault_characterization/stage5_faults.py",
        root / "scripts/fault_characterization/stage5_faults_v107_impl.py",
        root / "scripts/fault_characterization/stage5_phase2_modes.py",
        root / "scripts/fault_characterization/stage5_verdict.py",
        root / "scripts/fault_characterization/run_stage5_batch.sh",
        root / "scripts/run_xrun_stage5_fault.sh",
        root / "scripts/lib/xrun_stage5_common.sh",
        root / "platform/cv32e40p/stage5_assertion_policy_v1.json",
        root / "platform/cv32e40p/stage5_phase2_execution_policy_v1.json",
        root / "faults/cv32e40p/site_catalog/frozen_stage4_batch_v1/reference.json",
        root
        / "faults/cv32e40p/site_catalog/frozen_stage4_batch_v1/interface_freeze.json",
    ]
    for path in interface_candidates:
        if path.is_file():
            evidence_paths.append(path)

    unique_evidence: dict[str, Path] = {}
    for path in evidence_paths:
        unique_evidence[str(path.resolve())] = path.resolve()
    evidence = [
        file_evidence(path, root)
        for _, path in sorted(unique_evidence.items(), key=lambda item: item[0])
    ]

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "kind": "stage5_four_site_micro_batch_checkpoint",
        "generated_at_utc": utc_now(),
        "status": "PASS",
        "repository_root": str(root),
        "plan_root": str(plan_root),
        "batch_root": str(batch_root),
        "experiment_scope": {
            "design": "cv32e40p",
            "workload": "crc32",
            "unique_site_count": 4,
            "fault_instance_count": 8,
            "dual_polarity_per_site": True,
            "site_classes": sorted(EXPECTED_SITE_CLASSES),
            "reference_fault_id": reference_fault,
            "reference_qualification": "PASS",
        },
        "gate_claims": {
            "canonical_stage4_ids_preserved": True,
            "reference_is_first_site": True,
            "four_primary_classes_covered": True,
            "all_eight_fault_instances_completed": True,
            "all_oracles_independently_validated": True,
            "no_blocked_faults": True,
            "no_failed_faults": True,
            "validated_work_cleaned": True,
            "no_vcd_retained": True,
            "no_run_local_fault_netlist_retained": True,
            "storage_limit_applied": False,
            "sva_generated": False,
        },
        "selection_ids": selection_ids,
        "fault_ids": planned_fault_ids,
        "result_summary": {
            "final_states": dict(state_counts),
            "native_statuses": dict(native_counts),
            "routes": dict(route_counts),
            "validated_capabilities": dict(capability_counts),
            "fault_classes": dict(class_counts),
            "polarities": dict(polarity_counts),
        },
        "storage_observation": {
            "batch_total_bytes": directory_bytes(batch_root),
            "batch_total_human": human_bytes(directory_bytes(batch_root)),
            "reported_total_bytes": storage.get("total_bytes"),
            "reported_durable_estimate_bytes": storage.get(
                "durable_estimate_bytes"
            ),
            "reported_work_bytes": storage.get("work_bytes"),
            "filesystem_free_bytes_at_report": storage.get(
                "filesystem_free_bytes"
            ),
            "interpretation": (
                "observational only; no storage threshold was enforced"
            ),
        },
        "fault_results": sorted(
            per_fault,
            key=lambda item: (int(item.get("order") or 10**9), item["fault_id"]),
        ),
        "evidence_files": evidence,
    }
    digest_input = dict(report)
    digest_input.pop("generated_at_utc", None)
    report["checkpoint_digest_sha256"] = canonical_digest(digest_input)
    return report, list(unique_evidence.values())


def command_validate(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    plan_root = args.plan_root.resolve()
    batch_root = args.batch_root.resolve()
    report, _ = validate_batch(root, plan_root, batch_root)
    output = args.validation_output.resolve()
    write_json(output, report, overwrite=True)

    print()
    print("======================================================================")
    print("Stage5 four-site micro-batch validation: PASS")
    print("======================================================================")
    print(f"Reference fault     : {report['experiment_scope']['reference_fault_id']}")
    print(f"Unique sites        : {report['experiment_scope']['unique_site_count']}")
    print(f"Fault instances     : {report['experiment_scope']['fault_instance_count']}")
    print(f"Final states        : {report['result_summary']['final_states']}")
    print(f"Native outcomes     : {report['result_summary']['native_statuses']}")
    print(f"Routes              : {report['result_summary']['routes']}")
    print(f"Capabilities        : {report['result_summary']['validated_capabilities']}")
    print(f"Observed batch size : {report['storage_observation']['batch_total_human']}")
    print("Storage limit       : NOT APPLIED")
    print(f"Validation report   : {output}")
    return 0


def command_freeze(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    plan_root = args.plan_root.resolve()
    batch_root = args.batch_root.resolve()
    report, _ = validate_batch(root, plan_root, batch_root)

    validation_output = args.validation_output.resolve()
    write_json(validation_output, report, overwrite=True)

    checkpoint = args.checkpoint.resolve()
    if checkpoint.exists() and not args.force:
        existing = load_json(checkpoint, "existing four-site checkpoint")
        if (
            existing.get("status") == "PASS"
            and existing.get("checkpoint_digest_sha256")
            == report.get("checkpoint_digest_sha256")
        ):
            print("Existing four-site checkpoint already matches: PASS")
            print(f"Checkpoint: {checkpoint}")
            return 0
        raise FreezeError(
            "checkpoint exists but differs from the current validated batch; "
            "archive it or rerun with --force only for an intentional replacement"
        )

    write_json(checkpoint, report, overwrite=args.force)
    print()
    print("======================================================================")
    print("Stage5 four-site micro-batch freeze: PASS")
    print("======================================================================")
    print(f"Checkpoint         : {checkpoint}")
    print(f"Checkpoint digest  : {report['checkpoint_digest_sha256']}")
    print(f"Evidence file count: {len(report['evidence_files'])}")
    print("Large files copied : NO")
    print("Storage limit      : NOT APPLIED")
    return 0


def build_parser() -> argparse.ArgumentParser:
    root_default = Path(__file__).resolve().parents[2]
    plan_default = (
        root_default / "runs/stage5_plans/cv32e40p/crc32/sites_4"
    )
    batch_default = (
        root_default / "runs/stage5_campaign_v2/cv32e40p/crc32/sites_4"
    )
    validation_default = batch_default / "micro_batch_validation.json"
    checkpoint_default = (
        root_default / "docs/stage5/stage5_4site_micro_batch_checkpoint.json"
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root_default)
    parser.add_argument("--plan-root", type=Path, default=plan_default)
    parser.add_argument("--batch-root", type=Path, default=batch_default)
    parser.add_argument(
        "--validation-output", type=Path, default=validation_default
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser(
        "validate", help="independently validate the completed four-site batch"
    )
    validate.set_defaults(func=command_validate)

    freeze = subparsers.add_parser(
        "freeze", help="validate and write the durable four-site checkpoint"
    )
    freeze.add_argument("--checkpoint", type=Path, default=checkpoint_default)
    freeze.add_argument("--force", action="store_true")
    freeze.set_defaults(func=command_freeze)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except FreezeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"ERROR: unexpected four-site freeze failure: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
