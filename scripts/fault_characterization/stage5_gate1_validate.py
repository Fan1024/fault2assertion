#!/usr/bin/env python3
"""Fail-closed validation for Stage-5 mini-smoke Gate 1 (static generation).

The validator never invokes a simulator.  It verifies the mini selection,
independently generated Stage-5 campaign/specs/patches, generated golden/fault
monitors and manifests, immutable source hashes, and the absence of simulation
or permanent-netlist artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


EXPECTED_CLASSES = (
    "sequential_state",
    "control_path",
    "architectural_data",
    "generic_observable",
)
SELECTION_STAGE = "stage_04_targeted_fault_selection_plan"
CAMPAIGN_STAGE = "stage_05_fault_characterization_campaign"
FAULT_STAGE = "stage_05_fault_materialization"


class Gate1Error(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise Gate1Error(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise Gate1Error(f"invalid {label} JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise Gate1Error(f"{label} must be one JSON object")
    return payload


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def import_module(path: Path, name: str) -> Any:
    path = path.resolve()
    if not path.is_file():
        raise Gate1Error(f"Python module not found: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise Gate1Error(f"cannot import Python module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def resolve_reference(value: Any, base_dir: Path) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def fault_spec_digest(spec: Mapping[str, Any]) -> str:
    return canonical_json_digest(
        {
            key: value
            for key, value in spec.items()
            if key not in {"generated_at_utc", "fault_spec_digest_sha256"}
        }
    )


def selection_digest(selection: Mapping[str, Any]) -> str:
    return canonical_json_digest(
        {
            "selected_sites": selection.get("selected_sites"),
            "fault_instances": selection.get("fault_instances"),
        }
    )


def campaign_digest(campaign: Mapping[str, Any]) -> str:
    return canonical_json_digest(
        {
            "source_stage4": campaign.get("source_stage4"),
            "mapped_netlist": campaign.get("mapped_netlist"),
            "selected_sites": campaign.get("selected_sites"),
            "faults": campaign.get("faults"),
        }
    )


def monitor_facts(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="strict")
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
        "bind_count": len(re.findall(r"(?m)^bind\s+", text)),
        "contains_final": bool(re.search(r"\bfinal\s+(?:begin|:)", text)),
        "contains_removed_flush": bool(
            re.search(r"::flush\s*\(\s*\)\s*;", text)
        ),
        "fflush_count": text.count("$fflush"),
        "fopen_count": text.count("$fopen"),
    }


def add_error(errors: list[str], condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate Stage-5 mini-smoke Gate 1 without simulation."
    )
    parser.add_argument("--stage5-tool", type=Path, required=True)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--parent-selection", type=Path, required=True)
    parser.add_argument("--parent-campaign", type=Path, required=True)
    parser.add_argument("--mini-selection", type=Path, required=True)
    parser.add_argument("--mini-campaign", type=Path, required=True)
    parser.add_argument("--golden-monitor", type=Path, required=True)
    parser.add_argument("--golden-manifest", type=Path, required=True)
    parser.add_argument("--fault-monitor-dir", type=Path, required=True)
    parser.add_argument("--fault-manifest-dir", type=Path, required=True)
    parser.add_argument("--trace-dir", type=Path, required=True)
    parser.add_argument("--mini-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--max-receivers", type=int, default=32)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tool_path = args.stage5_tool.resolve()
    candidates_path = args.candidates.resolve()
    parent_selection_path = args.parent_selection.resolve()
    parent_campaign_path = args.parent_campaign.resolve()
    mini_selection_path = args.mini_selection.resolve()
    mini_campaign_path = args.mini_campaign.resolve()
    golden_monitor_path = args.golden_monitor.resolve()
    golden_manifest_path = args.golden_manifest.resolve()
    fault_monitor_dir = args.fault_monitor_dir.resolve()
    fault_manifest_dir = args.fault_manifest_dir.resolve()
    trace_dir = args.trace_dir.resolve()
    mini_root = args.mini_root.resolve()

    errors: list[str] = []
    warnings: list[str] = []

    try:
        tool = import_module(tool_path, "f2a_stage5_gate1_target")
        candidates = load_json(candidates_path, "Stage-4 candidates")
        parent_selection = load_json(parent_selection_path, "parent selection")
        parent_campaign = load_json(parent_campaign_path, "parent campaign")
        mini_selection = load_json(mini_selection_path, "mini selection")
        mini_campaign = load_json(mini_campaign_path, "mini campaign")
        golden_manifest = load_json(golden_manifest_path, "golden manifest")
    except Gate1Error as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    add_error(
        errors,
        mini_selection.get("stage") == SELECTION_STAGE,
        "mini selection stage marker mismatch",
    )
    add_error(
        errors,
        mini_campaign.get("stage") == CAMPAIGN_STAGE,
        "mini campaign stage marker mismatch",
    )
    add_error(
        errors,
        str(mini_campaign.get("program_version")) == str(tool.PROGRAM_VERSION),
        "mini campaign program version does not match Stage-5 tool",
    )
    add_error(
        errors,
        str(mini_campaign.get("schema_version")) == str(tool.SCHEMA_VERSION),
        "mini campaign schema does not match Stage-5 tool",
    )

    add_error(
        errors,
        mini_selection.get("selection_digest_sha256")
        == selection_digest(mini_selection),
        "mini selection digest mismatch",
    )
    add_error(
        errors,
        mini_campaign.get("campaign_digest_sha256")
        == campaign_digest(mini_campaign),
        "mini campaign digest mismatch",
    )

    provenance = mini_selection.get("mini_smoke_provenance")
    if not isinstance(provenance, dict):
        errors.append("mini selection missing mini_smoke_provenance")
        provenance = {}
    expected_parent_files = (
        ("parent_candidates", "parent_candidates_sha256", candidates_path),
        (
            "parent_selection",
            "parent_selection_sha256",
            parent_selection_path,
        ),
        ("parent_campaign", "parent_campaign_sha256", parent_campaign_path),
    )
    for path_key, sha_key, expected_path in expected_parent_files:
        actual_path = resolve_reference(provenance.get(path_key), mini_selection_path.parent)
        add_error(
            errors,
            actual_path == expected_path,
            f"mini provenance path mismatch for {path_key}",
        )
        add_error(
            errors,
            provenance.get(sha_key) == sha256_file(expected_path),
            f"mini provenance SHA mismatch for {path_key}",
        )
    add_error(
        errors,
        provenance.get("parent_candidate_digest_sha256")
        == candidates.get("candidate_digest_sha256"),
        "mini provenance candidate digest mismatch",
    )
    add_error(
        errors,
        provenance.get("parent_selection_digest_sha256")
        == parent_selection.get("selection_digest_sha256"),
        "mini provenance parent-selection digest mismatch",
    )
    add_error(
        errors,
        provenance.get("parent_campaign_digest_sha256")
        == parent_campaign.get("campaign_digest_sha256"),
        "mini provenance parent-campaign digest mismatch",
    )

    candidate_records = candidates.get("sites")
    selected_sites = mini_selection.get("selected_sites")
    fault_instances = mini_selection.get("fault_instances")
    campaign_selected = mini_campaign.get("selected_sites")
    campaign_faults = mini_campaign.get("faults")
    if not isinstance(candidate_records, list):
        errors.append("candidate sites array missing")
        candidate_records = []
    if not isinstance(selected_sites, list):
        errors.append("mini selected_sites array missing")
        selected_sites = []
    if not isinstance(fault_instances, list):
        errors.append("mini fault_instances array missing")
        fault_instances = []
    if not isinstance(campaign_selected, list):
        errors.append("mini campaign selected_sites array missing")
        campaign_selected = []
    if not isinstance(campaign_faults, list):
        errors.append("mini campaign faults array missing")
        campaign_faults = []

    add_error(errors, len(selected_sites) == 4, "mini selection must contain 4 sites")
    add_error(errors, len(fault_instances) == 8, "mini selection must contain 8 faults")
    add_error(errors, len(campaign_selected) == 4, "mini campaign must contain 4 sites")
    add_error(errors, len(campaign_faults) == 8, "mini campaign must contain 8 faults")

    candidate_by_id = {
        str(item.get("site_id")): item
        for item in candidate_records
        if isinstance(item, dict) and item.get("site_id")
    }
    selected_class_counts: Counter[str] = Counter()
    selected_ids: set[str] = set()
    selected_site_ids: set[str] = set()

    for rank, selected in enumerate(selected_sites, start=1):
        if not isinstance(selected, dict):
            errors.append(f"selected-site record {rank} is not an object")
            continue
        selection_id = str(selected.get("selection_id", ""))
        site_id = str(selected.get("site_id", ""))
        add_error(
            errors,
            selection_id == f"TS{rank:06d}",
            f"selected-site deterministic ID/rank mismatch: {selection_id}",
        )
        add_error(
            errors,
            int(selected.get("selection_rank", -1)) == rank,
            f"selected-site rank field mismatch: {selection_id}",
        )
        add_error(
            errors,
            selection_id not in selected_ids,
            f"duplicate selection ID: {selection_id}",
        )
        selected_ids.add(selection_id)
        add_error(
            errors,
            site_id not in selected_site_ids,
            f"duplicate selected site: {site_id}",
        )
        selected_site_ids.add(site_id)
        candidate = candidate_by_id.get(site_id)
        if candidate is None:
            errors.append(f"selected candidate missing: {site_id}")
            continue
        classification = candidate.get("classification")
        class_name = (
            str(classification.get("primary_class", ""))
            if isinstance(classification, dict)
            else ""
        )
        selected_class_counts[class_name] += 1
        add_error(
            errors,
            class_name in EXPECTED_CLASSES,
            f"unknown selected class for {site_id}: {class_name}",
        )
        add_error(
            errors,
            sorted(selected.get("eligible_polarities", [])) == ["SA0", "SA1"],
            f"selected site is not dual-polarity: {selection_id}",
        )
        activity = candidate.get("activity")
        usable_activity = (
            isinstance(activity, dict)
            and int(activity.get("instance_count", 0)) == 1
            and activity.get("seen_0") is True
            and activity.get("seen_1") is True
            and activity.get("unknown_seen") is False
            and int(activity.get("value_change_count", 0)) > 0
        )
        add_error(
            errors,
            usable_activity,
            f"selected site activity/instance invariant failed: {selection_id}",
        )
        safety = candidate.get("static_safety")
        usable_safety = (
            isinstance(safety, dict)
            and safety.get("clock_safe") is True
            and safety.get("reset_set_safe") is True
            and not bool(safety.get("touches_scan_structure", False))
        )
        add_error(
            errors,
            usable_safety,
            f"selected site safety invariant failed: {selection_id}",
        )

    add_error(
        errors,
        selected_class_counts == Counter({name: 1 for name in EXPECTED_CLASSES}),
        f"four-class site coverage mismatch: {dict(selected_class_counts)}",
    )

    instance_by_id: dict[str, dict[str, Any]] = {}
    polarity_by_selection: dict[str, set[str]] = defaultdict(set)
    for rank in range(1, 5):
        for polarity in ("SA0", "SA1"):
            expected_id = f"TF{rank:06d}_{polarity}"
            matches = [
                item
                for item in fault_instances
                if isinstance(item, dict) and item.get("fault_id") == expected_id
            ]
            add_error(
                errors,
                len(matches) == 1,
                f"mini selection fault ID missing/duplicate: {expected_id}",
            )
    for instance in fault_instances:
        if not isinstance(instance, dict):
            continue
        fault_id = str(instance.get("fault_id", ""))
        selection_id = str(instance.get("selection_id", ""))
        polarity = str(instance.get("polarity", ""))
        add_error(errors, fault_id not in instance_by_id, f"duplicate fault ID: {fault_id}")
        instance_by_id[fault_id] = instance
        polarity_by_selection[selection_id].add(polarity)
        add_error(
            errors,
            polarity in {"SA0", "SA1"}
            and int(instance.get("stuck_at", -1)) == (0 if polarity == "SA0" else 1),
            f"fault polarity/stuck-at mismatch: {fault_id}",
        )
    for selection_id in selected_ids:
        add_error(
            errors,
            polarity_by_selection.get(selection_id) == {"SA0", "SA1"},
            f"mini polarity expansion incomplete: {selection_id}",
        )

    source_stage4 = mini_campaign.get("source_stage4")
    if not isinstance(source_stage4, dict):
        errors.append("mini campaign missing source_stage4")
        source_stage4 = {}
    add_error(
        errors,
        resolve_reference(source_stage4.get("candidates_path"), mini_campaign_path.parent)
        == candidates_path,
        "mini campaign candidate path mismatch",
    )
    add_error(
        errors,
        source_stage4.get("candidates_sha256") == sha256_file(candidates_path),
        "mini campaign candidate SHA mismatch",
    )
    add_error(
        errors,
        resolve_reference(source_stage4.get("selection_path"), mini_campaign_path.parent)
        == mini_selection_path,
        "mini campaign selection path mismatch",
    )
    add_error(
        errors,
        source_stage4.get("selection_sha256") == sha256_file(mini_selection_path),
        "mini campaign selection SHA mismatch",
    )
    add_error(
        errors,
        source_stage4.get("selection_digest_sha256")
        == mini_selection.get("selection_digest_sha256"),
        "mini campaign selection digest mismatch",
    )

    mapped = mini_campaign.get("mapped_netlist")
    if not isinstance(mapped, dict):
        errors.append("mini campaign missing mapped_netlist")
        mapped = {}
    mapped_path = resolve_reference(mapped.get("path"), mini_campaign_path.parent)
    add_error(errors, mapped_path.is_file(), f"mapped netlist missing: {mapped_path}")
    if mapped_path.is_file():
        add_error(
            errors,
            mapped.get("sha256") == sha256_file(mapped_path),
            "mapped netlist SHA mismatch",
        )

    campaign_fault_by_id: dict[str, dict[str, Any]] = {}
    campaign_polarity_by_selection: dict[str, set[str]] = defaultdict(set)
    spec_records: list[dict[str, Any]] = []
    for record in campaign_faults:
        if not isinstance(record, dict):
            errors.append("campaign fault record is not an object")
            continue
        fault_id = str(record.get("fault_id", ""))
        add_error(
            errors,
            fault_id not in campaign_fault_by_id,
            f"duplicate mini campaign fault: {fault_id}",
        )
        campaign_fault_by_id[fault_id] = record
        campaign_polarity_by_selection[str(record.get("selection_id", ""))].add(
            str(record.get("polarity", ""))
        )

        spec_path = resolve_reference(record.get("fault_spec"), mini_campaign_path.parent)
        patch_path = resolve_reference(record.get("patch"), mini_campaign_path.parent)
        add_error(errors, spec_path.is_file(), f"fault spec missing: {spec_path}")
        add_error(
            errors,
            patch_path.is_file() and patch_path.stat().st_size > 0,
            f"fault patch missing/empty: {patch_path}",
        )
        if not spec_path.is_file():
            continue
        spec = load_json(spec_path, f"fault spec {fault_id}")
        add_error(errors, spec.get("stage") == FAULT_STAGE, f"fault stage mismatch: {fault_id}")
        add_error(errors, spec.get("fault_id") == fault_id, f"fault ID mismatch: {fault_id}")
        add_error(
            errors,
            str(spec.get("program_version")) == str(tool.PROGRAM_VERSION),
            f"fault program version mismatch: {fault_id}",
        )
        add_error(
            errors,
            spec.get("fault_spec_digest_sha256") == fault_spec_digest(spec),
            f"fault spec digest mismatch: {fault_id}",
        )
        add_error(
            errors,
            record.get("fault_spec_digest_sha256")
            == spec.get("fault_spec_digest_sha256"),
            f"campaign/spec digest mismatch: {fault_id}",
        )
        add_error(
            errors,
            spec.get("source_stage4", {}).get("selection_sha256")
            == sha256_file(mini_selection_path),
            f"fault spec selection SHA mismatch: {fault_id}",
        )
        add_error(
            errors,
            spec.get("source_stage4", {}).get("selection_digest_sha256")
            == mini_selection.get("selection_digest_sha256"),
            f"fault spec selection digest mismatch: {fault_id}",
        )
        add_error(
            errors,
            spec.get("mapped_netlist", {}).get("sha256") == mapped.get("sha256"),
            f"fault spec mapped-netlist SHA mismatch: {fault_id}",
        )
        receivers = spec.get("receiver_signals")
        receiver_count = len(receivers) if isinstance(receivers, list) else 0
        add_error(
            errors,
            1 <= receiver_count <= args.max_receivers,
            f"fault receiver count outside 1..{args.max_receivers}: "
            f"{fault_id} count={receiver_count}",
        )
        activity = spec.get("site", {}).get("activity")
        add_error(
            errors,
            isinstance(activity, dict)
            and int(activity.get("instance_count", 0)) == 1,
            f"fault spec definition-level instance count is not 1: {fault_id}",
        )
        modification = spec.get("modification")
        add_error(
            errors,
            isinstance(modification, dict)
            and modification.get("temporary_pre_fault_net")
            and modification.get("stuck_at_assignment"),
            f"fault modification metadata incomplete: {fault_id}",
        )
        spec_records.append(
            {
                "fault_id": fault_id,
                "spec": str(spec_path),
                "spec_sha256": sha256_file(spec_path),
                "patch": str(patch_path),
                "patch_sha256": sha256_file(patch_path) if patch_path.is_file() else None,
                "receiver_count": receiver_count,
            }
        )

    add_error(
        errors,
        set(campaign_fault_by_id) == set(instance_by_id),
        "mini campaign fault IDs differ from mini selection fault IDs",
    )
    for selection_id in selected_ids:
        add_error(
            errors,
            campaign_polarity_by_selection.get(selection_id) == {"SA0", "SA1"},
            f"mini campaign polarity expansion incomplete: {selection_id}",
        )

    golden_monitor_record: dict[str, Any] = {}
    if not golden_monitor_path.is_file():
        errors.append(f"golden monitor missing: {golden_monitor_path}")
    else:
        golden_monitor_record = monitor_facts(golden_monitor_path)
        add_error(
            errors,
            not golden_monitor_record["contains_final"],
            "golden monitor contains a final block",
        )
        add_error(
            errors,
            not golden_monitor_record["contains_removed_flush"],
            "golden monitor contains removed ::flush()",
        )
        add_error(
            errors,
            golden_monitor_record["fflush_count"] > 0,
            "golden monitor contains no $fflush",
        )

    add_error(
        errors,
        golden_manifest.get("kind") == "stage5_comprehensive_golden_monitor",
        "golden manifest kind mismatch",
    )
    add_error(
        errors,
        golden_manifest.get("campaign_digest_sha256")
        == mini_campaign.get("campaign_digest_sha256"),
        "golden manifest campaign digest mismatch",
    )
    add_error(
        errors,
        int(golden_manifest.get("selected_site_count", -1)) == 4,
        "golden manifest selected-site count mismatch",
    )
    bound_module_count = int(golden_manifest.get("bound_module_count", -1))
    if golden_monitor_record:
        add_error(
            errors,
            golden_monitor_record["bind_count"] == bound_module_count,
            "golden monitor bind count does not match manifest bound_module_count",
        )
    golden_trace_path = resolve_reference(
        golden_manifest.get("trace_output"), golden_manifest_path.parent
    )
    add_error(
        errors,
        golden_trace_path.parent == trace_dir,
        "golden trace output is outside the Gate-1 trace directory",
    )
    add_error(
        errors,
        not golden_trace_path.exists(),
        "golden trace already exists; Gate 1 must not run simulation",
    )

    fault_monitor_records: list[dict[str, Any]] = []
    expected_monitor_names = {f"{fault_id}.sv" for fault_id in campaign_fault_by_id}
    actual_monitor_names = {
        path.name for path in fault_monitor_dir.glob("*.sv") if path.is_file()
    }
    add_error(
        errors,
        actual_monitor_names == expected_monitor_names,
        f"fault monitor file set mismatch: expected={sorted(expected_monitor_names)}, "
        f"actual={sorted(actual_monitor_names)}",
    )
    expected_manifest_names = {
        f"{fault_id}.json" for fault_id in campaign_fault_by_id
    }
    actual_manifest_names = {
        path.name for path in fault_manifest_dir.glob("*.json") if path.is_file()
    }
    add_error(
        errors,
        actual_manifest_names == expected_manifest_names,
        "fault manifest file set mismatch",
    )

    for fault_id in sorted(campaign_fault_by_id):
        monitor_path = fault_monitor_dir / f"{fault_id}.sv"
        manifest_path = fault_manifest_dir / f"{fault_id}.json"
        if not monitor_path.is_file() or not manifest_path.is_file():
            continue
        facts = monitor_facts(monitor_path)
        manifest = load_json(manifest_path, f"fault manifest {fault_id}")
        add_error(errors, not facts["contains_final"], f"fault monitor contains final: {fault_id}")
        add_error(
            errors,
            not facts["contains_removed_flush"],
            f"fault monitor contains removed ::flush(): {fault_id}",
        )
        add_error(errors, facts["fflush_count"] > 0, f"fault monitor has no $fflush: {fault_id}")
        add_error(errors, facts["bind_count"] == 1, f"fault monitor bind count is not 1: {fault_id}")
        add_error(
            errors,
            manifest.get("kind") == "stage5_fault_monitor",
            f"fault manifest kind mismatch: {fault_id}",
        )
        add_error(errors, manifest.get("fault_id") == fault_id, f"fault manifest ID mismatch: {fault_id}")
        record = campaign_fault_by_id[fault_id]
        add_error(
            errors,
            manifest.get("fault_spec_digest_sha256")
            == record.get("fault_spec_digest_sha256"),
            f"fault manifest/spec digest mismatch: {fault_id}",
        )
        trace_path = resolve_reference(manifest.get("trace_output"), manifest_path.parent)
        add_error(
            errors,
            trace_path.parent == trace_dir,
            f"fault trace output is outside Gate-1 trace directory: {fault_id}",
        )
        add_error(
            errors,
            not trace_path.exists(),
            f"fault trace already exists; Gate 1 must not run simulation: {fault_id}",
        )
        facts["fault_id"] = fault_id
        facts["manifest"] = str(manifest_path)
        facts["manifest_sha256"] = sha256_file(manifest_path)
        fault_monitor_records.append(facts)

    forbidden_files: list[str] = []
    forbidden_names = {"fault_netlist.v", "temporary_netlist.v", "xrun.log"}
    forbidden_suffixes = (".vcd", ".vcd.gz", ".fsdb", ".fst", ".mapped.sim.v")
    forbidden_dirs = {"xcelium.d", "INCA_libs", "work", "shm", "waves"}
    for path in mini_root.rglob("*"):
        if path.is_dir() and path.name in forbidden_dirs:
            forbidden_files.append(str(path))
        elif path.is_file() and (
            path.name in forbidden_names
            or any(path.name.endswith(suffix) for suffix in forbidden_suffixes)
        ):
            forbidden_files.append(str(path))
    add_error(
        errors,
        not forbidden_files,
        f"simulation/permanent-netlist artifacts exist during Gate 1: {forbidden_files}",
    )

    report = {
        "kind": "stage5_mini_smoke_gate1_static_validation",
        "generated_at_utc": utc_now(),
        "status": "PASS" if not errors else "FAIL",
        "stage5_tool": {
            "path": str(tool_path),
            "version": str(tool.PROGRAM_VERSION),
            "schema": str(tool.SCHEMA_VERSION),
            "sha256": sha256_file(tool_path),
        },
        "parent_artifacts": {
            "candidates": str(candidates_path),
            "candidates_sha256": sha256_file(candidates_path),
            "parent_selection": str(parent_selection_path),
            "parent_selection_sha256": sha256_file(parent_selection_path),
            "parent_campaign": str(parent_campaign_path),
            "parent_campaign_sha256": sha256_file(parent_campaign_path),
        },
        "mini_selection": {
            "path": str(mini_selection_path),
            "sha256": sha256_file(mini_selection_path),
            "selection_digest_sha256": mini_selection.get("selection_digest_sha256"),
            "site_count": len(selected_sites),
            "fault_count": len(fault_instances),
            "by_class": dict(sorted(selected_class_counts.items())),
        },
        "mini_campaign": {
            "path": str(mini_campaign_path),
            "sha256": sha256_file(mini_campaign_path),
            "campaign_digest_sha256": mini_campaign.get("campaign_digest_sha256"),
            "site_count": len(campaign_selected),
            "fault_count": len(campaign_faults),
        },
        "fault_specs_and_patches": spec_records,
        "golden_monitor": golden_monitor_record,
        "golden_manifest": {
            "path": str(golden_manifest_path),
            "sha256": sha256_file(golden_manifest_path),
            "selected_site_count": golden_manifest.get("selected_site_count"),
            "bound_module_count": golden_manifest.get("bound_module_count"),
        },
        "fault_monitors": fault_monitor_records,
        "forbidden_artifacts": forbidden_files,
        "warnings": warnings,
        "errors": errors,
    }
    atomic_write_json(args.report.resolve(), report)

    print()
    print("=" * 78)
    print("Fault2Assertion Stage-5 Mini Smoke Gate 1")
    print("=" * 78)
    print(f"Selected sites        : {len(selected_sites)}")
    print(f"Fault instances       : {len(fault_instances)}")
    print(f"Fault specs/patches   : {len(spec_records)}")
    print(f"Golden monitors       : {1 if golden_monitor_path.is_file() else 0}")
    print(f"Fault monitors        : {len(fault_monitor_records)}")
    print(f"Trace files generated : {len(list(trace_dir.glob('*.tsv')))}")
    print(f"Forbidden artifacts   : {len(forbidden_files)}")
    print(f"Warnings              : {len(warnings)}")
    print(f"Errors                : {len(errors)}")
    print(f"Report                : {args.report.resolve()}")
    print(f"Result                : {report['status']}")
    print("=" * 78)

    if errors:
        print("\nErrors:")
        for error in errors[:50]:
            print(f"  - {error}")
        if len(errors) > 50:
            print(f"  ... {len(errors) - 50} additional errors omitted")
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Gate1Error as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print(f"ERROR: unexpected Gate-1 validation failure: {exc}", file=sys.stderr)
        raise SystemExit(1)
