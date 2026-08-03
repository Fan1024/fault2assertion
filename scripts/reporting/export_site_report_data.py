#!/usr/bin/env python3
"""Export site-selection and site-campaign data for Fault2Assertion reports.

This tool is intentionally read-only. It reads the frozen Stage 1-4 site
catalogs and, when available, one Stage-5 campaign manifest/state tree. It
writes compact JSON/JSONL/TXT artifacts under a caller-selected output folder.

Design goals:
- standard library only;
- manifest-driven campaign traversal;
- no VCD/netlist/log parsing in the normal path;
- exact reconstruction of the current Stage-4 score formula;
- explicit integrity warnings instead of silent guessing;
- no modification of any scientific experiment artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

PROGRAM_VERSION = "1.0.1"

STAGE_FILES = {
    "stage1": "stage_01_raw_sites.json",
    "stage2": "stage_02_static_sites.json",
    "stage3": "stage_03_activity.json",
    "stage4_candidates": "stage_04_candidates.json",
    "stage4_selection": "stage_04_selection.json",
}

# Reporting-only mapping. It does not change the scientific site class.
# Ordered first-match rules make the mapping deterministic and auditable.
FUNCTIONAL_BLOCK_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "IF_PREFETCH",
        (
            r"cv32e40p_if_stage",
            r"cv32e40p_prefetch_buffer",
            r"cv32e40p_prefetch_controller",
            r"cv32e40p_aligner",
            r"cv32e40p_fifo",
        ),
    ),
    (
        "ID_CONTROL",
        (
            r"cv32e40p_id_stage",
            r"cv32e40p_controller",
            r"cv32e40p_decoder",
            r"cv32e40p_compressed_decoder",
        ),
    ),
    (
        "EXECUTE",
        (
            r"cv32e40p_ex_stage",
            r"cv32e40p_alu(?:_|$)",
            r"cv32e40p_alu_div",
            r"cv32e40p_mult",
            r"cv32e40p_popcnt",
            r"cv32e40p_ff_one",
        ),
    ),
    (
        "LSU_MEMORY_INTERFACE",
        (
            r"cv32e40p_load_store_unit",
            r"cv32e40p_obi_interface",
        ),
    ),
    ("REGISTER_FILE", (r"cv32e40p_register_file",)),
    ("CSR_DEBUG", (r"cv32e40p_cs_registers",)),
    (
        "IRQ_SLEEP",
        (
            r"cv32e40p_int_controller",
            r"cv32e40p_sleep_unit",
        ),
    ),
    ("CLOCKING", (r"cv32e40p_clock_gate",)),
    (
        "CORE_INTEGRATION",
        (
            r"cv32e40p_core",
            r"cv32e40p_top",
        ),
    ),
)


class ExportError(RuntimeError):
    """Controlled export failure with a user-actionable message."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path, label: str, *, required: bool = True) -> dict[str, Any] | None:
    if not path.is_file():
        if required:
            raise ExportError(f"{label} not found: {path}")
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ExportError(f"invalid {label} JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ExportError(f"{label} must contain one JSON object: {path}")
    return value


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def write_json(path: Path, value: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
    )


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    count = 0
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(
                json.dumps(row, ensure_ascii=False, sort_keys=False, separators=(",", ":"))
                + "\n"
            )
            count += 1
    temporary.replace(path)
    return count


def canonical_json_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def git_output(root: Path, args: Sequence[str]) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except FileNotFoundError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def find_repo_root(explicit: Path | None) -> Path:
    if explicit is not None:
        root = explicit.expanduser().resolve()
        if not (root / ".git").exists():
            raise ExportError(f"--repo-root is not a Git repository: {root}")
        return root
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    raise ExportError("cannot find repository root; pass --repo-root")


def discover_stage_file(catalog_dir: Path, logical_name: str) -> Path:
    preferred = catalog_dir / STAGE_FILES[logical_name]
    if preferred.is_file():
        return preferred.resolve()

    patterns = {
        "stage1": ("stage_01*.json", "*stage1*.json"),
        "stage2": ("stage_02*.json", "*stage2*.json"),
        "stage3": ("stage_03*.json", "*stage3*.json"),
        "stage4_candidates": ("stage_04*candidate*.json", "*candidate*.json"),
        "stage4_selection": ("stage_04*selection*.json", "*selection*.json"),
    }[logical_name]

    matches: list[Path] = []
    for pattern in patterns:
        matches.extend(path.resolve() for path in catalog_dir.glob(pattern) if path.is_file())
    unique = sorted(set(matches))
    if not unique:
        raise ExportError(
            f"cannot discover {logical_name} JSON under {catalog_dir}; "
            f"expected {preferred.name} or a matching Stage file"
        )
    if len(unique) > 1:
        rendered = "\n  ".join(str(path) for path in unique)
        raise ExportError(
            f"ambiguous {logical_name} JSON; found multiple candidates:\n  {rendered}"
        )
    return unique[0]


def discover_policy(root: Path) -> Path:
    candidates = (
        root / "platform/cv32e40p/fault_classification_policy.json",
        root / "platform/cv32e40p/stage4_fault_classification_policy.json",
    )
    for path in candidates:
        if path.is_file():
            return path.resolve()
    matches = sorted((root / "platform/cv32e40p").glob("*classification*policy*.json"))
    if len(matches) == 1:
        return matches[0].resolve()
    raise ExportError("cannot resolve exactly one Stage-4 fault classification policy")


def discover_campaign_root(root: Path, explicit: Path | None) -> tuple[Path | None, list[str]]:
    if explicit is not None:
        candidate = explicit.expanduser().resolve()
        if not candidate.is_dir():
            raise ExportError(f"campaign root not found: {candidate}")
        return candidate, [str(candidate)]

    # Stage-5 campaigns have used several stable top-level run roots over the
    # course of development. Search all supported roots, deduplicate manifests,
    # and choose the most recently modified campaign. An explicit
    # --campaign-root still takes precedence over this discovery logic.
    campaign_patterns = (
        "runs/stage5/**/campaign_manifest.json",
        "runs/stage5_campaign*/**/campaign_manifest.json",
        "runs/stage5_dev/**/campaign_manifest.json",
    )
    legacy_patterns = (
        "runs/stage5/**/pilot_manifest.json",
        "runs/stage5_campaign*/**/pilot_manifest.json",
        "runs/stage5_dev/**/pilot_manifest.json",
    )

    manifest_set: set[Path] = set()
    for pattern in campaign_patterns:
        manifest_set.update(path.resolve() for path in root.glob(pattern) if path.is_file())

    if not manifest_set:
        for pattern in legacy_patterns:
            manifest_set.update(path.resolve() for path in root.glob(pattern) if path.is_file())

    manifests = sorted(
        manifest_set,
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    discovered = [str(path.parent.resolve()) for path in manifests]
    return (manifests[0].parent.resolve() if manifests else None), discovered


def classify_functional_block(module_name: str) -> str:
    for block, patterns in FUNCTIONAL_BLOCK_RULES:
        if any(re.search(pattern, module_name, re.IGNORECASE) for pattern in patterns):
            return block
    return "UNMAPPED"


def get_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def get_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def first_list(mapping: Mapping[str, Any], names: Sequence[str]) -> list[Any]:
    for name in names:
        value = mapping.get(name)
        if isinstance(value, list):
            return value
    return []


def safe_log2p(value: int | float) -> float:
    return math.log2(max(0.0, float(value)) + 1.0)


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def normalize_score(raw: float, maximum: float = 10.0) -> float:
    return round(clamp01(raw / maximum), 6)


def number(mapping: Mapping[str, Any], key: str, *, default: float = 0.0) -> float:
    value = mapping.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ExportError(f"score policy value is not numeric: {key}={value!r}")
    return float(value)


def score_breakdown(
    site: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    score_weights = get_mapping(policy.get("score_weights"))
    assertion_weights = get_mapping(score_weights.get("assertion_relevance"))
    impact_weights = get_mapping(score_weights.get("failure_impact"))
    selection_rules = get_mapping(policy.get("selection_rules"))
    selection_weights = get_mapping(selection_rules.get("selection_score_weights"))

    classification = get_mapping(site.get("classification"))
    class_name = str(classification.get("primary_class", ""))
    tags = {str(item) for item in get_list(classification.get("semantic_tags"))}
    activity = get_mapping(site.get("activity"))
    safety = get_mapping(site.get("static_safety"))
    observability = get_mapping(site.get("coarse_observability"))
    detailed = get_mapping(site.get("detailed_structure"))
    polarities = [str(item) for item in get_list(site.get("eligible_polarities"))]

    missing: list[str] = []
    required_keys = (
        ("classification.primary_class", bool(class_name)),
        ("score_weights.assertion_relevance", bool(assertion_weights)),
        ("score_weights.failure_impact", bool(impact_weights)),
        ("selection_score_weights", bool(selection_weights)),
    )
    for label, available in required_keys:
        if not available:
            missing.append(label)
    if missing:
        return {
            "status": "INCOMPLETE",
            "missing": missing,
            "site_id": site.get("site_id"),
        }

    class_base_key = f"{class_name}_base"
    if class_base_key not in assertion_weights:
        return {
            "status": "INCOMPLETE",
            "missing": [f"score_weights.assertion_relevance.{class_base_key}"],
            "site_id": site.get("site_id"),
        }

    state_site = bool(site.get("state_site"))
    semantic_name_available = "generated_name" not in tags
    both_polarities = len(set(polarities)) == 2
    toggle_count = int(activity.get("binary_toggle_count", 0) or 0)
    unknown_seen = bool(activity.get("unknown_seen"))
    scan_touch = bool(safety.get("touches_scan_structure"))
    nearest_distance = observability.get("nearest_checkpoint_distance")
    nearest_distance_value = (
        int(nearest_distance)
        if isinstance(nearest_distance, int) and not isinstance(nearest_distance, bool)
        else None
    )

    assertion_terms: dict[str, float] = {}
    assertion_terms["class_base"] = number(assertion_weights, class_base_key)
    assertion_terms["state_site_bonus"] = (
        number(assertion_weights, "state_site_bonus") if state_site else 0.0
    )
    assertion_terms["semantic_name_bonus"] = (
        number(assertion_weights, "semantic_name_bonus")
        if semantic_name_available
        else 0.0
    )
    assertion_terms["both_polarities_bonus"] = (
        number(assertion_weights, "both_polarities_bonus")
        if both_polarities
        else 0.0
    )
    assertion_terms["toggle_bonus"] = min(
        number(assertion_weights, "toggle_bonus_cap"),
        safe_log2p(toggle_count) / 4.0,
    )
    assertion_terms["near_checkpoint_bonus"] = (
        number(assertion_weights, "near_checkpoint_bonus")
        / (1.0 + nearest_distance_value)
        if nearest_distance_value is not None
        else 0.0
    )
    assertion_terms["unknown_seen_penalty"] = (
        -number(assertion_weights, "unknown_seen_penalty") if unknown_seen else 0.0
    )
    assertion_terms["scan_touch_penalty"] = (
        -number(assertion_weights, "scan_touch_penalty") if scan_touch else 0.0
    )
    assertion_raw = sum(assertion_terms.values())
    assertion_score = normalize_score(assertion_raw)

    reaches_top = bool(observability.get("reaches_top_level_output"))
    reaches_seq = bool(observability.get("reaches_sequential_data"))
    logic_fanout = int(site.get("logic_fanout", 0) or 0)
    tfo_nodes = int(detailed.get("tfo_node_count_bounded", 0) or 0)
    reachable_seq = int(
        detailed.get("reachable_sequential_checkpoint_count_bounded", 0) or 0
    )
    reachable_top = int(detailed.get("reachable_top_output_count_bounded", 0) or 0)
    total_checkpoints = reachable_seq + reachable_top
    reconvergent_nodes = int(detailed.get("reconvergent_node_count_bounded", 0) or 0)

    impact_terms: dict[str, float] = {}
    impact_terms["top_output_reachable_bonus"] = (
        number(impact_weights, "top_output_reachable_bonus") if reaches_top else 0.0
    )
    impact_terms["sequential_checkpoint_reachable_bonus"] = (
        number(impact_weights, "sequential_checkpoint_reachable_bonus")
        if reaches_seq
        else 0.0
    )
    impact_terms["fanout_log_bonus"] = min(
        number(impact_weights, "fanout_log_bonus_cap"),
        safe_log2p(logic_fanout) / 3.0,
    )
    impact_terms["tfo_log_bonus"] = min(
        number(impact_weights, "tfo_log_bonus_cap"),
        safe_log2p(tfo_nodes) / 5.0,
    )
    impact_terms["reachable_checkpoint_log_bonus"] = min(
        number(impact_weights, "reachable_checkpoint_log_bonus_cap"),
        safe_log2p(total_checkpoints) / 3.0,
    )
    impact_terms["reconvergence_log_bonus"] = min(
        number(impact_weights, "reconvergence_log_bonus_cap"),
        safe_log2p(reconvergent_nodes) / 4.0,
    )
    impact_terms["near_checkpoint_bonus"] = (
        number(impact_weights, "near_checkpoint_bonus")
        / (1.0 + nearest_distance_value)
        if nearest_distance_value is not None
        else 0.0
    )
    impact_terms["both_polarities_bonus"] = (
        number(impact_weights, "both_polarities_bonus") if both_polarities else 0.0
    )
    impact_terms["unknown_seen_penalty"] = (
        -number(impact_weights, "unknown_seen_penalty") if unknown_seen else 0.0
    )
    impact_raw = sum(impact_terms.values())
    impact_score = normalize_score(impact_raw)

    assertion_weight = number(selection_weights, "assertion_relevance")
    impact_weight = number(selection_weights, "failure_impact")
    selection_score = round(
        clamp01(assertion_weight * assertion_score + impact_weight * impact_score),
        6,
    )

    stored = get_mapping(site.get("scores"))
    stored_assertion = stored.get("assertion_relevance_score")
    stored_impact = stored.get("failure_impact_score")
    stored_selection = stored.get("selection_score")

    def close_or_none(actual: float, expected: Any) -> bool | None:
        if not isinstance(expected, (int, float)) or isinstance(expected, bool):
            return None
        return math.isclose(actual, float(expected), abs_tol=1e-6)

    return {
        "status": "COMPLETE",
        "site_id": site.get("site_id"),
        "site_key": site.get("site_key"),
        "module": site.get("module"),
        "functional_block": classify_functional_block(str(site.get("module", ""))),
        "primary_class": class_name,
        "input_features": {
            "state_site": state_site,
            "semantic_name_available": semantic_name_available,
            "semantic_tags": sorted(tags),
            "eligible_polarities": polarities,
            "both_polarities": both_polarities,
            "binary_toggle_count": toggle_count,
            "unknown_seen": unknown_seen,
            "scan_touch": scan_touch,
            "nearest_checkpoint_distance": nearest_distance_value,
            "reaches_top_level_output": reaches_top,
            "reaches_sequential_data": reaches_seq,
            "logic_fanout": logic_fanout,
            "tfo_node_count_bounded": tfo_nodes,
            "reachable_sequential_checkpoint_count_bounded": reachable_seq,
            "reachable_top_output_count_bounded": reachable_top,
            "reachable_checkpoint_count_bounded_total": total_checkpoints,
            "reconvergent_node_count_bounded": reconvergent_nodes,
            "tfo_truncated": bool(detailed.get("truncated")),
        },
        "assertion_relevance": {
            "terms": {key: round(value, 9) for key, value in assertion_terms.items()},
            "raw": round(assertion_raw, 9),
            "normalized": assertion_score,
            "normalization": "clamp(raw / 10, 0, 1)",
        },
        "failure_impact": {
            "terms": {key: round(value, 9) for key, value in impact_terms.items()},
            "raw": round(impact_raw, 9),
            "normalized": impact_score,
            "normalization": "clamp(raw / 10, 0, 1)",
        },
        "selection": {
            "assertion_weight": assertion_weight,
            "impact_weight": impact_weight,
            "assertion_weighted_contribution": round(
                assertion_weight * assertion_score, 9
            ),
            "impact_weighted_contribution": round(impact_weight * impact_score, 9),
            "score": selection_score,
            "formula": "0.55 * assertion_relevance_score + 0.45 * failure_impact_score",
        },
        "stored_scores": stored,
        "reconstruction_checks": {
            "assertion_matches_stored": close_or_none(assertion_score, stored_assertion),
            "impact_matches_stored": close_or_none(impact_score, stored_impact),
            "selection_matches_stored": close_or_none(selection_score, stored_selection),
        },
    }


def compact_site(site: Mapping[str, Any]) -> dict[str, Any]:
    classification = get_mapping(site.get("classification"))
    scores = get_mapping(site.get("scores"))
    activity = get_mapping(site.get("activity"))
    safety = get_mapping(site.get("static_safety"))
    observability = get_mapping(site.get("coarse_observability"))
    detailed = get_mapping(site.get("detailed_structure"))
    module = str(site.get("module", ""))
    return {
        "site_id": site.get("site_id"),
        "site_key": site.get("site_key"),
        "module": module,
        "functional_block": classify_functional_block(module),
        "source_key": site.get("source_key"),
        "source_net": site.get("source_net"),
        "source_kind": site.get("source_kind"),
        "state_site": site.get("state_site"),
        "logic_fanout": site.get("logic_fanout"),
        "fanout_bucket": site.get("fanout_bucket"),
        "stage2_status": site.get("stage2_status"),
        "stage3_status": site.get("stage3_status"),
        "stage4_status": site.get("stage4_status"),
        "eligible_polarities": site.get("eligible_polarities"),
        "primary_selection_eligible": site.get("primary_selection_eligible"),
        "primary_selection_exclusion_reasons": site.get(
            "primary_selection_exclusion_reasons"
        ),
        "primary_class": classification.get("primary_class"),
        "injection_kind": classification.get("injection_kind"),
        "semantic_tags": classification.get("semantic_tags"),
        "semantic_confidence": classification.get("semantic_confidence"),
        "assertion_relevance_score": scores.get("assertion_relevance_score"),
        "failure_impact_score": scores.get("failure_impact_score"),
        "selection_score": scores.get("selection_score"),
        "binary_toggle_count": activity.get("binary_toggle_count"),
        "seen_0": activity.get("seen_0"),
        "seen_1": activity.get("seen_1"),
        "unknown_seen": activity.get("unknown_seen"),
        "touches_scan_structure": safety.get("touches_scan_structure"),
        "reaches_top_level_output": observability.get("reaches_top_level_output"),
        "reaches_sequential_data": observability.get("reaches_sequential_data"),
        "nearest_checkpoint_distance": observability.get(
            "nearest_checkpoint_distance"
        ),
        "tfo_node_count_bounded": detailed.get("tfo_node_count_bounded"),
        "tfo_edge_count_bounded": detailed.get("tfo_edge_count_bounded"),
        "reachable_sequential_checkpoint_count_bounded": detailed.get(
            "reachable_sequential_checkpoint_count_bounded"
        ),
        "reachable_top_output_count_bounded": detailed.get(
            "reachable_top_output_count_bounded"
        ),
        "reconvergent_node_count_bounded": detailed.get(
            "reconvergent_node_count_bounded"
        ),
        "branching_node_count_bounded": detailed.get(
            "branching_node_count_bounded"
        ),
        "tfo_truncated": detailed.get("truncated"),
    }


def index_by_site_id(rows: Iterable[Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        site_id = raw.get("site_id")
        if isinstance(site_id, str) and site_id:
            result[site_id] = dict(raw)
    return result


def selected_site_rows(selection: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = first_list(
        selection,
        ("selected_sites", "sites", "selection", "site_selection"),
    )
    return [dict(row) for row in rows if isinstance(row, dict)]


def selected_fault_rows(selection: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = first_list(
        selection,
        ("faults", "fault_instances", "selected_faults", "generated_faults"),
    )
    return [dict(row) for row in rows if isinstance(row, dict)]


def load_campaign_manifest(campaign_root: Path) -> tuple[Path, dict[str, Any]]:
    for name in ("campaign_manifest.json", "pilot_manifest.json"):
        path = campaign_root / name
        payload = load_json(path, f"Stage-5 {name}", required=False)
        if payload is not None:
            return path.resolve(), payload
    raise ExportError(f"campaign manifest not found under {campaign_root}")


def load_optional_json(path: Path) -> dict[str, Any] | None:
    return load_json(path, path.name, required=False)


def fault_root_from_record(record: Mapping[str, Any], campaign_root: Path) -> Path | None:
    value = record.get("fault_root")
    if isinstance(value, str) and value:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = campaign_root / path
        return path.resolve()
    fault_id = record.get("fault_id")
    if isinstance(fault_id, str) and fault_id:
        candidates = list(campaign_root.glob(f"**/{fault_id}"))
        candidates = [path for path in candidates if path.is_dir()]
        if len(candidates) == 1:
            return candidates[0].resolve()
    return None


def result_summary(result: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if result is None:
        return None
    raw = get_mapping(result.get("raw_facts"))
    return {
        "status": result.get("status"),
        "reason": result.get("reason"),
        "valid_execution": result.get("valid_execution"),
        "completion": result.get("completion"),
        "workload_outcome": result.get("workload_outcome"),
        "architectural_outcome": result.get("architectural_outcome"),
        "tool_status": get_mapping(result.get("tool")).get("status"),
        "selected_terminal": get_mapping(raw.get("signature_resolution")).get(
            "selected_terminal"
        ),
    }


def collect_campaign(
    campaign_root: Path,
    output_dir: Path,
    warnings: list[str],
) -> dict[str, Any]:
    manifest_path, manifest = load_campaign_manifest(campaign_root)
    state = load_optional_json(campaign_root / "campaign_state.json")
    sites = [dict(row) for row in get_list(manifest.get("sites")) if isinstance(row, dict)]
    faults = [dict(row) for row in get_list(manifest.get("faults")) if isinstance(row, dict)]

    fault_export_rows: list[dict[str, Any]] = []
    state_counts: Counter[str] = Counter()
    native_counts: Counter[str] = Counter()
    route_counts: Counter[str] = Counter()
    observe_counts: Counter[str] = Counter()
    quarantine_counts: Counter[str] = Counter()

    for record in faults:
        fault_id = str(record.get("fault_id", ""))
        fault_root = fault_root_from_record(record, campaign_root)
        if fault_root is None:
            warnings.append(f"cannot resolve fault root for {fault_id}")
        status = load_optional_json(fault_root / "status.json") if fault_root else None
        routing = load_optional_json(fault_root / "routing.json") if fault_root else None
        native = (
            load_optional_json(fault_root / "native/run/result.json")
            if fault_root
            else None
        )
        observe = (
            load_optional_json(fault_root / "observe/run/result.json")
            if fault_root
            else None
        )
        quarantine = (
            load_optional_json(fault_root / "diagnostic_quarantine/run/result.json")
            if fault_root
            else None
        )
        oracle = (
            load_optional_json(fault_root / "oracle/oracle.json")
            if fault_root
            else None
        )
        validation = (
            load_optional_json(fault_root / "oracle/validation.json")
            if fault_root
            else None
        )
        cleanup = load_optional_json(fault_root / "cleanup.json") if fault_root else None

        status_state = str((status or {}).get("state", "MISSING"))
        native_status = str((native or {}).get("status", "NOT_RUN"))
        route = str((routing or {}).get("route", "NOT_ROUTED"))
        observe_status = str((observe or {}).get("status", "NOT_RUN"))
        quarantine_status = str((quarantine or {}).get("status", "NOT_RUN"))
        state_counts[status_state] += 1
        native_counts[native_status] += 1
        route_counts[route] += 1
        observe_counts[observe_status] += 1
        quarantine_counts[quarantine_status] += 1

        row = {
            "manifest_record": record,
            "fault_root": str(fault_root) if fault_root else None,
            "status": status,
            "routing": routing,
            "native_result": result_summary(native),
            "observe_result": result_summary(observe),
            "diagnostic_quarantine_result": result_summary(quarantine),
            "oracle_summary": {
                "exists": oracle is not None,
                "effect": (oracle or {}).get("effect"),
                "validated_capability": (oracle or {}).get("validated_capability"),
                "digest": (oracle or {}).get("oracle_digest_sha256"),
            },
            "oracle_validation": {
                "exists": validation is not None,
                "status": (validation or {}).get("status"),
                "validated_capability": (validation or {}).get(
                    "validated_capability"
                ),
            },
            "cleanup": cleanup,
        }
        fault_export_rows.append(row)

    fault_by_id = {
        str(row.get("fault_id")): row for row in faults if row.get("fault_id") is not None
    }
    site_export_rows: list[dict[str, Any]] = []
    site_state_counts: Counter[str] = Counter()
    for site in sites:
        fault_ids = [str(item) for item in get_list(site.get("fault_ids"))]
        site_fault_rows = []
        states = []
        for fault_id in fault_ids:
            record = fault_by_id.get(fault_id, {})
            fault_root = fault_root_from_record(record, campaign_root)
            status = load_optional_json(fault_root / "status.json") if fault_root else None
            state_name = str((status or {}).get("state", "MISSING"))
            states.append(state_name)
            site_fault_rows.append(
                {
                    "fault_id": fault_id,
                    "state": state_name,
                    "native_status": (status or {}).get("native_status"),
                    "route": (status or {}).get("route"),
                    "observe_status": (status or {}).get("observe_status"),
                    "diagnostic_quarantine_status": (status or {}).get(
                        "diagnostic_quarantine_status"
                    ),
                    "failure_reason": (status or {}).get("failure_reason"),
                }
            )
        if states and all(value == "ORACLE_VALIDATED_CLEANED" for value in states):
            site_state = "PASSED"
        elif any(value.startswith("BLOCKED_") for value in states):
            site_state = "BLOCKED"
        elif any(value == "FAILED" for value in states):
            site_state = "FAILED"
        else:
            site_state = "INCOMPLETE"
        site_state_counts[site_state] += 1
        site_export_rows.append(
            {
                "site": site,
                "functional_block": classify_functional_block(str(site.get("module", ""))),
                "site_state": site_state,
                "faults": site_fault_rows,
            }
        )

    write_jsonl(output_dir / "08_campaign_fault_status.jsonl", fault_export_rows)
    write_jsonl(output_dir / "09_campaign_site_status.jsonl", site_export_rows)

    return {
        "campaign_root": str(campaign_root),
        "manifest": {
            "path": str(manifest_path),
            "sha256": sha256_file(manifest_path),
            "kind": manifest.get("kind"),
            "schema_version": manifest.get("schema_version"),
            "program_version": manifest.get("program_version"),
            "universe": manifest.get("universe"),
        },
        "state": state,
        "counts": {
            "sites": len(sites),
            "faults": len(faults),
            "fault_state_counts": dict(sorted(state_counts.items())),
            "site_state_counts": dict(sorted(site_state_counts.items())),
            "native_status_counts": dict(sorted(native_counts.items())),
            "route_counts": dict(sorted(route_counts.items())),
            "observe_status_counts": dict(sorted(observe_counts.items())),
            "diagnostic_quarantine_status_counts": dict(
                sorted(quarantine_counts.items())
            ),
        },
    }


def copy_reports(catalog_dir: Path, output_dir: Path) -> list[str]:
    destination = output_dir / "source_reports"
    destination.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for path in sorted(catalog_dir.glob("*.txt")):
        target = destination / path.name
        shutil.copy2(path, target)
        copied.append(str(target.relative_to(output_dir)))
    return copied


def stage_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    for key in (
        "stage1_summary",
        "stage2_summary",
        "stage3_summary",
        "stage4_summary",
        "selection_summary",
    ):
        if isinstance(payload.get(key), dict):
            return dict(payload[key])
    return {}


def build_module_and_block_summaries(
    stage1_sites: Sequence[Any],
    stage2_sites: Sequence[Any],
    stage3_sites: Sequence[Any],
    stage4_sites: Sequence[Any],
    selected_ids: set[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    modules: dict[str, Counter[str]] = defaultdict(Counter)
    blocks: dict[str, Counter[str]] = defaultdict(Counter)

    def add(rows: Sequence[Any], label: str, predicate: Any = None) -> None:
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            if predicate is not None and not predicate(raw):
                continue
            module = str(raw.get("module", "UNKNOWN"))
            block = classify_functional_block(module)
            modules[module][label] += 1
            blocks[block][label] += 1

    add(stage1_sites, "stage1_raw")
    add(
        stage2_sites,
        "stage2_eligible",
        lambda row: row.get("stage2_status") == "eligible_for_activity_profile",
    )
    add(
        stage3_sites,
        "stage3_activity_eligible",
        lambda row: row.get("stage3_status") == "eligible_for_fault_classification",
    )
    add(
        stage4_sites,
        "stage4_classified",
        lambda row: row.get("stage4_status") == "classified_candidate",
    )
    add(stage4_sites, "selected", lambda row: str(row.get("site_id")) in selected_ids)

    class_counts_module: dict[str, Counter[str]] = defaultdict(Counter)
    class_counts_block: dict[str, Counter[str]] = defaultdict(Counter)
    polarity_module: dict[str, Counter[str]] = defaultdict(Counter)
    polarity_block: dict[str, Counter[str]] = defaultdict(Counter)
    for raw in stage4_sites:
        if not isinstance(raw, dict) or str(raw.get("site_id")) not in selected_ids:
            continue
        module = str(raw.get("module", "UNKNOWN"))
        block = classify_functional_block(module)
        class_name = str(get_mapping(raw.get("classification")).get("primary_class", "UNKNOWN"))
        class_counts_module[module][class_name] += 1
        class_counts_block[block][class_name] += 1
        polarities = [str(item) for item in get_list(raw.get("eligible_polarities"))]
        pattern = (
            "SA0_and_SA1"
            if set(polarities) == {"SA0", "SA1"}
            else "SA0_only"
            if polarities == ["SA0"]
            else "SA1_only"
            if polarities == ["SA1"]
            else "OTHER"
        )
        polarity_module[module][pattern] += 1
        polarity_block[block][pattern] += 1

    module_output = {
        module: {
            **dict(counter),
            "selected_by_class": dict(sorted(class_counts_module[module].items())),
            "selected_by_polarity_pattern": dict(
                sorted(polarity_module[module].items())
            ),
        }
        for module, counter in sorted(modules.items())
    }
    block_output = {
        block: {
            **dict(counter),
            "selected_by_class": dict(sorted(class_counts_block[block].items())),
            "selected_by_polarity_pattern": dict(
                sorted(polarity_block[block].items())
            ),
        }
        for block, counter in sorted(blocks.items())
    }
    return module_output, block_output


def chunked_jsonl(
    output_dir: Path,
    rows: Sequence[Mapping[str, Any]],
    chunk_size: int,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for start in range(0, len(rows), chunk_size):
        part = start // chunk_size + 1
        path = output_dir / f"11_all_candidates_compact.part{part:03d}.jsonl"
        count = write_jsonl(path, rows[start : start + chunk_size])
        result.append(
            {
                "path": path.name,
                "record_count": count,
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
        )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export Fault2Assertion site-selection and campaign report data."
    )
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument(
        "--catalog-dir",
        type=Path,
        help="Defaults to faults/cv32e40p/site_catalog under the repo root.",
    )
    parser.add_argument(
        "--campaign-root",
        type=Path,
        help="Optional Stage-5 campaign root. If omitted, newest manifest under runs/stage5 is used.",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--include-all-candidates",
        action="store_true",
        help="Write compact JSONL records for every Stage-4 candidate in chunks.",
    )
    parser.add_argument(
        "--candidate-chunk-size",
        type=int,
        default=5000,
        help="Records per all-candidate JSONL part (default: 5000).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = find_repo_root(args.repo_root)
    catalog_dir = (
        args.catalog_dir.expanduser().resolve()
        if args.catalog_dir is not None
        else (root / "faults/cv32e40p/site_catalog").resolve()
    )
    if not catalog_dir.is_dir():
        raise ExportError(f"site catalog directory not found: {catalog_dir}")

    output_dir = args.output_dir.expanduser()
    if not output_dir.is_absolute():
        output_dir = root / output_dir
    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ExportError(f"output directory already exists and is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    stage_paths = {
        name: discover_stage_file(catalog_dir, name) for name in STAGE_FILES
    }
    policy_path = discover_policy(root)

    print("Loading Stage-1 catalog...", flush=True)
    stage1 = load_json(stage_paths["stage1"], "Stage-1 catalog")
    print("Loading Stage-2 catalog...", flush=True)
    stage2 = load_json(stage_paths["stage2"], "Stage-2 catalog")
    print("Loading Stage-3 catalog...", flush=True)
    stage3 = load_json(stage_paths["stage3"], "Stage-3 catalog")
    print("Loading Stage-4 candidates (largest input)...", flush=True)
    stage4_candidates = load_json(
        stage_paths["stage4_candidates"], "Stage-4 candidates"
    )
    print("Loading Stage-4 selection...", flush=True)
    stage4_selection = load_json(
        stage_paths["stage4_selection"], "Stage-4 selection"
    )
    policy = load_json(policy_path, "Stage-4 policy")
    assert stage1 is not None
    assert stage2 is not None
    assert stage3 is not None
    assert stage4_candidates is not None
    assert stage4_selection is not None
    assert policy is not None

    stage1_sites = get_list(stage1.get("sites"))
    stage2_sites = get_list(stage2.get("sites"))
    stage3_sites = get_list(stage3.get("sites"))
    stage4_sites = get_list(stage4_candidates.get("sites"))
    selected_rows = selected_site_rows(stage4_selection)
    selected_faults = selected_fault_rows(stage4_selection)

    campaign_root, discovered_campaigns = discover_campaign_root(
        root, args.campaign_root
    )
    campaign_manifest: dict[str, Any] | None = None
    if campaign_root is not None:
        _, campaign_manifest = load_campaign_manifest(campaign_root)

    if not selected_rows and campaign_manifest is not None:
        selected_rows = [
            dict(row)
            for row in get_list(campaign_manifest.get("sites"))
            if isinstance(row, dict)
        ]
        warnings.append(
            "Stage-4 selection did not expose a selected-sites array; "
            "selected identity was recovered from the Stage-5 campaign manifest"
        )
    if not selected_faults and campaign_manifest is not None:
        selected_faults = [
            dict(row)
            for row in get_list(campaign_manifest.get("faults"))
            if isinstance(row, dict)
        ]

    selected_ids = {
        str(row.get("site_id"))
        for row in selected_rows
        if isinstance(row.get("site_id"), str)
    }
    if not selected_ids:
        raise ExportError("could not resolve any selected site IDs")

    candidate_by_id = index_by_site_id(stage4_sites)
    selection_by_id = index_by_site_id(selected_rows)
    manifest_site_by_id = (
        index_by_site_id(get_list(campaign_manifest.get("sites")))
        if campaign_manifest is not None
        else {}
    )

    missing_candidates = sorted(selected_ids - set(candidate_by_id))
    if missing_candidates:
        warnings.append(
            f"{len(missing_candidates)} selected sites were absent from Stage-4 candidates"
        )

    detailed_selected: list[dict[str, Any]] = []
    score_rows: list[dict[str, Any]] = []
    for site_id in sorted(selected_ids):
        candidate = candidate_by_id.get(site_id)
        if candidate is None:
            continue
        module = str(candidate.get("module", ""))
        detailed_selected.append(
            {
                "site_id": site_id,
                "functional_block": classify_functional_block(module),
                "candidate": candidate,
                "selection": selection_by_id.get(site_id),
                "campaign_site": manifest_site_by_id.get(site_id),
            }
        )
        score_rows.append(score_breakdown(candidate, policy))

    write_jsonl(output_dir / "05_selected_sites_detailed.jsonl", detailed_selected)
    write_jsonl(output_dir / "06_selected_site_score_breakdowns.jsonl", score_rows)
    write_jsonl(output_dir / "07_fault_instances.jsonl", selected_faults)

    module_summary, block_summary = build_module_and_block_summaries(
        stage1_sites,
        stage2_sites,
        stage3_sites,
        stage4_sites,
        selected_ids,
    )
    write_json(output_dir / "03_module_summary.json", module_summary)
    write_json(
        output_dir / "04_functional_block_summary.json",
        {
            "mapping_is_reporting_only": True,
            "mapping_rules": [
                {"block": block, "regexes": list(patterns)}
                for block, patterns in FUNCTIONAL_BLOCK_RULES
            ],
            "summary": block_summary,
        },
    )

    formula_export = {
        "policy_path": str(policy_path),
        "policy_sha256": sha256_file(policy_path),
        "policy": policy,
        "implemented_formula": {
            "normalization": "N(raw) = clamp(raw / 10, 0, 1)",
            "selection": "S_selection = w_assertion * S_assertion + w_impact * S_impact",
            "assertion_relevance_terms": [
                "class_base",
                "state_site_bonus",
                "semantic_name_bonus",
                "both_polarities_bonus",
                "toggle_bonus=min(cap, log2(1+binary_toggle_count)/4)",
                "near_checkpoint_bonus=weight/(1+nearest_checkpoint_distance)",
                "unknown_seen_penalty",
                "scan_touch_penalty",
            ],
            "failure_impact_terms": [
                "top_output_reachable_bonus",
                "sequential_checkpoint_reachable_bonus",
                "fanout_log_bonus=min(cap, log2(1+logic_fanout)/3)",
                "tfo_log_bonus=min(cap, log2(1+tfo_node_count_bounded)/5)",
                "reachable_checkpoint_log_bonus=min(cap, log2(1+checkpoint_count)/3)",
                "reconvergence_log_bonus=min(cap, log2(1+reconvergent_nodes)/4)",
                "near_checkpoint_bonus=weight/(1+nearest_checkpoint_distance)",
                "both_polarities_bonus",
                "unknown_seen_penalty",
            ],
        },
    }
    write_json(output_dir / "02_policy_and_formula.json", formula_export)

    pipeline_summary = {
        "stage1": {
            "path": str(stage_paths["stage1"]),
            "sha256": sha256_file(stage_paths["stage1"]),
            "summary": stage_summary(stage1),
        },
        "stage2": {
            "path": str(stage_paths["stage2"]),
            "sha256": sha256_file(stage_paths["stage2"]),
            "summary": stage_summary(stage2),
        },
        "stage3": {
            "path": str(stage_paths["stage3"]),
            "sha256": sha256_file(stage_paths["stage3"]),
            "summary": stage_summary(stage3),
        },
        "stage4_candidates": {
            "path": str(stage_paths["stage4_candidates"]),
            "sha256": sha256_file(stage_paths["stage4_candidates"]),
            "summary": stage_summary(stage4_candidates),
            "site_records": len(stage4_sites),
        },
        "stage4_selection": {
            "path": str(stage_paths["stage4_selection"]),
            "sha256": sha256_file(stage_paths["stage4_selection"]),
            "summary": stage_summary(stage4_selection),
            "selected_site_records_resolved": len(selected_ids),
            "fault_records_resolved": len(selected_faults),
        },
    }
    write_json(output_dir / "01_pipeline_summary.json", pipeline_summary)

    campaign_export = None
    if campaign_root is not None:
        print(f"Collecting Stage-5 campaign status from {campaign_root}...", flush=True)
        campaign_export = collect_campaign(campaign_root, output_dir, warnings)
    else:
        warnings.append(
            "no Stage-5 campaign manifest was discovered; campaign status files were not exported"
        )

    all_candidate_parts: list[dict[str, Any]] = []
    if args.include_all_candidates:
        if args.candidate_chunk_size <= 0:
            raise ExportError("--candidate-chunk-size must be positive")
        print("Writing compact records for all Stage-4 candidates...", flush=True)
        compact_rows = [compact_site(row) for row in stage4_sites if isinstance(row, dict)]
        all_candidate_parts = chunked_jsonl(
            output_dir, compact_rows, args.candidate_chunk_size
        )

    copied_reports = copy_reports(catalog_dir, output_dir)

    score_status_counts = Counter(str(row.get("status")) for row in score_rows)
    score_mismatch_counts = Counter()
    for row in score_rows:
        checks = get_mapping(row.get("reconstruction_checks"))
        for key, value in checks.items():
            if value is False:
                score_mismatch_counts[key] += 1

    export_manifest = {
        "schema_version": "1.0",
        "program_version": PROGRAM_VERSION,
        "generated_at_utc": utc_now(),
        "repo_root": str(root),
        "git": {
            "commit": git_output(root, ["rev-parse", "HEAD"]),
            "branch": git_output(root, ["branch", "--show-current"]),
            "status_porcelain": git_output(root, ["status", "--porcelain"]),
        },
        "catalog_dir": str(catalog_dir),
        "output_dir": str(output_dir),
        "source_files": {
            name: {
                "path": str(path),
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
            for name, path in stage_paths.items()
        },
        "policy": {
            "path": str(policy_path),
            "sha256": sha256_file(policy_path),
        },
        "campaign_auto_discovery": {
            "selected": str(campaign_root) if campaign_root else None,
            "all_discovered_newest_first": discovered_campaigns,
        },
        "record_counts": {
            "stage1_sites": len(stage1_sites),
            "stage2_sites": len(stage2_sites),
            "stage3_sites": len(stage3_sites),
            "stage4_candidate_sites": len(stage4_sites),
            "selected_sites": len(selected_ids),
            "selected_site_details_written": len(detailed_selected),
            "selected_fault_instances": len(selected_faults),
            "score_breakdowns": len(score_rows),
        },
        "score_reconstruction": {
            "status_counts": dict(sorted(score_status_counts.items())),
            "mismatch_counts": dict(sorted(score_mismatch_counts.items())),
        },
        "all_candidate_parts": all_candidate_parts,
        "copied_source_reports": copied_reports,
        "campaign": campaign_export,
        "warnings": warnings,
    }
    write_json(output_dir / "00_export_manifest.json", export_manifest)

    lines = [
        "Fault2Assertion Site Report Export",
        "=" * 80,
        f"Generated UTC       : {export_manifest['generated_at_utc']}",
        f"Repository          : {root}",
        f"Git commit          : {export_manifest['git']['commit']}",
        f"Catalog directory   : {catalog_dir}",
        f"Output directory    : {output_dir}",
        "",
        "Record counts",
        "-" * 80,
    ]
    for key, value in export_manifest["record_counts"].items():
        lines.append(f"{key:32s}: {value}")
    lines.extend(
        [
            "",
            "Score reconstruction",
            "-" * 80,
            f"Status counts       : {dict(score_status_counts)}",
            f"Mismatch counts     : {dict(score_mismatch_counts)}",
            "",
            "Campaign",
            "-" * 80,
            f"Selected root       : {campaign_root}",
            f"Discovered roots    : {len(discovered_campaigns)}",
        ]
    )
    if campaign_export is not None:
        for key, value in campaign_export["counts"].items():
            lines.append(f"{key:32s}: {value}")
    lines.extend(["", "Warnings", "-" * 80])
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("None")
    lines.extend(
        [
            "",
            "Important interpretation",
            "-" * 80,
            "Functional-block names are reporting-only mappings.",
            "They do not replace the Stage-4 scientific primary_class.",
            "The export is read-only and does not modify simulation artifacts.",
            "Large VCDs, netlists, simulator work directories, and logs are not copied.",
        ]
    )
    atomic_write_text(output_dir / "10_integrity_report.txt", "\n".join(lines) + "\n")

    print()
    print("=" * 80)
    print("Site report export: PASS")
    print("=" * 80)
    print(f"Output directory      : {output_dir}")
    print(f"Selected sites        : {len(selected_ids)}")
    print(f"Fault instances       : {len(selected_faults)}")
    print(f"Score breakdowns      : {len(score_rows)}")
    print(f"Score mismatches      : {sum(score_mismatch_counts.values())}")
    print(f"Warnings              : {len(warnings)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ExportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
