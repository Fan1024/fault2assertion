#!/usr/bin/env python3
"""Stage-4 fault-site classification and deterministic unique-site selection.

This program consumes the Stage-3 activity catalog and produces:

* stage_04_candidates.json
* stage_04_selection.json
* stage_04_report.txt

It does not modify a Verilog netlist, generate a faulty netlist, or create
per-fault directories.  Stage 4 performs bounded detailed structural analysis,
semantic classification, scoring, quota-controlled unique-site selection, and activity-derived fault-instance expansion.

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import re
import sys
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Sequence


PROGRAM_VERSION = "1.1.0"
SCHEMA_VERSION = "1.1"
CANDIDATE_STAGE = "stage_04_fault_type_classification"
SELECTION_STAGE = "stage_04_targeted_fault_selection_plan"

EXPECTED_STAGE3_STAGE = "stage_03_golden_activity_filtering"
EXPECTED_STAGE2_STAGE = "stage_02_static_safety_filtering"
EXPECTED_STAGE3_ELIGIBLE_STATUS = "eligible_for_fault_classification"
EXPECTED_STAGE2_ELIGIBLE_STATUS = "eligible_for_activity_profile"

CLASS_NAMES = (
    "sequential_state",
    "control_path",
    "architectural_data",
    "generic_observable",
)

POLARITY_ORDER = {"SA0": 0, "SA1": 1}


class Stage4Error(RuntimeError):
    """Controlled Stage-4 failure."""


@dataclass(frozen=True)
class ClassificationPolicy:
    path: Path
    sha256: str
    payload: Mapping[str, Any]
    name: str
    expected_design: str
    expected_stage2_digest: str
    quotas: Mapping[str, int]
    selection_rules: Mapping[str, Any]
    structural_analysis: Mapping[str, Any]
    semantic_rules: Mapping[str, tuple[re.Pattern[str], ...]]
    score_weights: Mapping[str, Mapping[str, float]]


@dataclass(frozen=True)
class GraphContext:
    module: Any
    stage2_payload: Mapping[str, Any]
    parsed: Any
    graph: Any
    source_path: Path
    source_sha256: str
    stage1_policy_path: Path
    stage1_policy_sha256: str
    role_distances: Mapping[str, Mapping[tuple[str, str], int]]
    indegree: Mapping[tuple[str, str], int]
    outdegree: Mapping[tuple[str, str], int]


@dataclass(frozen=True)
class SelectionCandidate:
    """One workload-eligible unique site considered for Stage-4 selection."""

    site_id: str
    site_key: str
    module: str
    fault_class: str
    eligible_polarities: tuple[str, ...]
    score: float
    assertion_relevance_score: float
    failure_impact_score: float
    semantic_confidence: float
    source_kind: str
    state_site: bool
    scan_touch: bool


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


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


def load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise Stage4Error(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise Stage4Error(f"invalid {label} JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise Stage4Error(f"{label} must contain one JSON object: {path}")
    return payload


def atomic_write_text(path: Path, text: str, force: bool) -> None:
    path = path.resolve()
    if path.exists() and not force:
        raise Stage4Error(
            f"refusing to overwrite existing file without --force: {path}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{hashlib.sha256(text.encode()).hexdigest()[:12]}")
    temporary.write_text(text, encoding="utf-8")
    temporary.chmod(0o644)
    temporary.replace(path)


def write_json(path: Path, payload: Mapping[str, Any], force: bool) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, indent=2, sort_keys=False, ensure_ascii=False) + "\n",
        force,
    )


def safe_log2p(value: int | float) -> float:
    return math.log2(max(0.0, float(value)) + 1.0)


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def normalize_score(raw: float, maximum: float = 10.0) -> float:
    return round(clamp01(raw / maximum), 6)


def compile_regexes(values: Any, label: str) -> tuple[re.Pattern[str], ...]:
    if not isinstance(values, list) or not values:
        raise Stage4Error(f"{label} must be a non-empty list")
    compiled: list[re.Pattern[str]] = []
    for index, value in enumerate(values):
        if not isinstance(value, str) or not value:
            raise Stage4Error(f"{label}[{index}] must be a non-empty string")
        try:
            compiled.append(re.compile(value, re.IGNORECASE))
        except re.error as exc:
            raise Stage4Error(f"invalid regex {label}[{index}]: {exc}") from exc
    return tuple(compiled)


def require_number(mapping: Mapping[str, Any], key: str, label: str) -> float:
    value = mapping.get(key)
    if not isinstance(value, (int, float)):
        raise Stage4Error(f"{label}.{key} must be numeric")
    return float(value)


def require_bool(mapping: Mapping[str, Any], key: str, label: str) -> bool:
    value = mapping.get(key)
    if not isinstance(value, bool):
        raise Stage4Error(f"{label}.{key} must be boolean")
    return value


def require_positive_int(mapping: Mapping[str, Any], key: str, label: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or value <= 0:
        raise Stage4Error(f"{label}.{key} must be a positive integer")
    return value


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def load_policy(path: Path) -> ClassificationPolicy:
    path = path.resolve()
    payload = load_json_object(path, "Stage-4 policy")

    if payload.get("schema_version") != SCHEMA_VERSION:
        raise Stage4Error(
            "Stage-4 policy schema mismatch: "
            f"expected={SCHEMA_VERSION}, actual={payload.get('schema_version')!r}"
        )

    classes = payload.get("classification_classes")
    if classes != list(CLASS_NAMES):
        raise Stage4Error(
            "classification_classes must exactly equal: " + ", ".join(CLASS_NAMES)
        )

    quotas_payload = payload.get("selection_quotas_unique_sites")
    if not isinstance(quotas_payload, dict):
        raise Stage4Error("selection_quotas_unique_sites must be an object")
    quotas: dict[str, int] = {}
    for class_name in CLASS_NAMES:
        value = quotas_payload.get(class_name)
        if not isinstance(value, int) or value <= 0:
            raise Stage4Error(
                f"unique-site selection quota for {class_name} must be a positive integer"
            )
        quotas[class_name] = value
    extra_quota_keys = sorted(set(quotas_payload) - set(CLASS_NAMES))
    if extra_quota_keys:
        raise Stage4Error(
            "unknown unique-site quota classes: " + ", ".join(extra_quota_keys)
        )

    selection_rules = payload.get("selection_rules")
    structural_analysis = payload.get("structural_analysis")
    semantic_payload = payload.get("semantic_rules")
    score_weights = payload.get("score_weights")
    if not isinstance(selection_rules, dict):
        raise Stage4Error("selection_rules must be an object")
    if not isinstance(structural_analysis, dict):
        raise Stage4Error("structural_analysis must be an object")
    if not isinstance(semantic_payload, dict):
        raise Stage4Error("semantic_rules must be an object")
    if not isinstance(score_weights, dict):
        raise Stage4Error("score_weights must be an object")

    require_bool(
        selection_rules,
        "exclude_scan_touch_from_primary_selection",
        "selection_rules",
    )
    require_bool(
        selection_rules,
        "exclude_unknown_seen_only",
        "selection_rules",
    )
    require_bool(
        selection_rules,
        "expand_all_workload_eligible_polarities",
        "selection_rules",
    )
    if not selection_rules["expand_all_workload_eligible_polarities"]:
        raise Stage4Error(
            "Stage-4 policy must expand all workload-eligible polarities"
        )
    initial_fraction = require_number(
        selection_rules,
        "initial_max_fraction_per_module_per_class",
        "selection_rules",
    )
    relaxed_fraction = require_number(
        selection_rules,
        "relaxed_max_fraction_per_module_per_class",
        "selection_rules",
    )
    if not (0.0 < initial_fraction <= relaxed_fraction <= 1.0):
        raise Stage4Error(
            "module fractions must satisfy 0 < initial <= relaxed <= 1"
        )
    minimum_confidence = require_number(
        selection_rules,
        "minimum_semantic_confidence",
        "selection_rules",
    )
    if not (0.0 <= minimum_confidence <= 1.0):
        raise Stage4Error("minimum_semantic_confidence must be within [0, 1]")

    selection_score_weights = selection_rules.get("selection_score_weights")
    if not isinstance(selection_score_weights, dict):
        raise Stage4Error("selection_score_weights must be an object")
    assertion_weight = require_number(
        selection_score_weights,
        "assertion_relevance",
        "selection_score_weights",
    )
    impact_weight = require_number(
        selection_score_weights,
        "failure_impact",
        "selection_score_weights",
    )
    if not math.isclose(assertion_weight + impact_weight, 1.0, abs_tol=1e-9):
        raise Stage4Error("selection score weights must sum to 1.0")

    require_positive_int(
        structural_analysis,
        "bounded_tfo_max_depth",
        "structural_analysis",
    )
    require_positive_int(
        structural_analysis,
        "bounded_tfo_max_nodes",
        "structural_analysis",
    )
    require_positive_int(
        structural_analysis,
        "control_role_distance_cap",
        "structural_analysis",
    )

    semantic_rules = {
        key: compile_regexes(semantic_payload.get(key), f"semantic_rules.{key}")
        for key in (
            "control_name_regexes",
            "data_name_regexes",
            "control_module_regexes",
            "data_module_regexes",
        )
    }
    semantic_rules["generated_name_regexes"] = compile_regexes(
        structural_analysis.get("generated_name_regexes"),
        "structural_analysis.generated_name_regexes",
    )

    for group_name in ("semantic", "assertion_relevance", "failure_impact"):
        group = score_weights.get(group_name)
        if not isinstance(group, dict) or not group:
            raise Stage4Error(f"score_weights.{group_name} must be a non-empty object")
        for key, value in group.items():
            if not isinstance(value, (int, float)):
                raise Stage4Error(
                    f"score_weights.{group_name}.{key} must be numeric"
                )

    expected_design = payload.get("expected_design")
    expected_stage2_digest = payload.get("expected_stage2_digest")
    if not isinstance(expected_design, str) or not expected_design:
        raise Stage4Error("expected_design must be a non-empty string")
    if not isinstance(expected_stage2_digest, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_stage2_digest
    ):
        raise Stage4Error("expected_stage2_digest must be a lowercase SHA-256")

    return ClassificationPolicy(
        path=path,
        sha256=sha256_file(path),
        payload=payload,
        name=str(payload.get("policy_name", "")),
        expected_design=expected_design,
        expected_stage2_digest=expected_stage2_digest,
        quotas=quotas,
        selection_rules=selection_rules,
        structural_analysis=structural_analysis,
        semantic_rules=semantic_rules,
        score_weights=score_weights,
    )


# ---------------------------------------------------------------------------
# Stage-3 and graph loading
# ---------------------------------------------------------------------------


def validate_stage3_payload(
    path: Path,
    payload: Mapping[str, Any],
    policy: ClassificationPolicy,
) -> None:
    if payload.get("stage") != EXPECTED_STAGE3_STAGE:
        raise Stage4Error(
            "Stage-3 input stage mismatch: "
            f"expected={EXPECTED_STAGE3_STAGE}, actual={payload.get('stage')!r}"
        )
    if payload.get("design") != policy.expected_design:
        raise Stage4Error(
            "Stage-3 design mismatch: "
            f"expected={policy.expected_design}, actual={payload.get('design')!r}"
        )
    definitions = payload.get("definitions")
    if not isinstance(definitions, dict) or definitions.get("vcd_generated") is not False:
        raise Stage4Error("Stage-3 input must record vcd_generated=false")

    source_stage2 = payload.get("source_stage2")
    if not isinstance(source_stage2, dict):
        raise Stage4Error("Stage-3 source_stage2 metadata is missing")
    if source_stage2.get("static_filter_digest_sha256") != policy.expected_stage2_digest:
        raise Stage4Error(
            "Stage-2 digest mismatch in Stage-3 provenance: "
            f"expected={policy.expected_stage2_digest}, "
            f"actual={source_stage2.get('static_filter_digest_sha256')!r}"
        )

    sites = payload.get("sites")
    summary = payload.get("stage3_summary")
    if not isinstance(sites, list) or not isinstance(summary, dict):
        raise Stage4Error("Stage-3 input is missing sites or stage3_summary")
    if len(sites) != int(summary.get("raw_site_count", -1)):
        raise Stage4Error("Stage-3 raw-site count does not match sites length")

    ids: set[str] = set()
    keys: set[str] = set()
    eligible_count = 0
    for site in sites:
        if not isinstance(site, dict):
            raise Stage4Error("Stage-3 sites must contain JSON objects")
        site_id = str(site.get("site_id", ""))
        site_key = str(site.get("site_key", ""))
        if not re.fullmatch(r"RS\d{6}", site_id):
            raise Stage4Error(f"invalid Stage-3 site ID: {site_id!r}")
        if site_id in ids or site_key in keys:
            raise Stage4Error(f"duplicate Stage-3 site: {site_id} / {site_key}")
        ids.add(site_id)
        keys.add(site_key)

        if site.get("stage3_status") == EXPECTED_STAGE3_ELIGIBLE_STATUS:
            eligible_count += 1
            polarities = site.get("eligible_polarities")
            if not isinstance(polarities, list) or not polarities:
                raise Stage4Error(f"eligible site lacks polarities: {site_id}")
            if any(polarity not in POLARITY_ORDER for polarity in polarities):
                raise Stage4Error(f"invalid polarity at site {site_id}: {polarities}")
            safety = site.get("static_safety")
            if not isinstance(safety, dict):
                raise Stage4Error(f"missing static_safety at site {site_id}")
            if not safety.get("clock_safe") or not safety.get("reset_set_safe"):
                raise Stage4Error(
                    f"unsafe Stage-3 site cannot enter Stage 4: {site_id}"
                )
            if site.get("stage2_status") != EXPECTED_STAGE2_ELIGIBLE_STATUS:
                raise Stage4Error(
                    f"Stage-3 eligible site was not Stage-2 eligible: {site_id}"
                )
            if not isinstance(site.get("activity"), dict):
                raise Stage4Error(f"eligible site lacks activity: {site_id}")

    if eligible_count != int(summary.get("activity_eligible_site_count", -1)):
        raise Stage4Error(
            "Stage-3 eligible-site count does not match stage3_summary"
        )

    digest = payload.get("activity_digest_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise Stage4Error("Stage-3 activity digest is missing or malformed")

    if sha256_file(path) == "":  # pragma: no cover - defensive only
        raise Stage4Error("unreachable Stage-3 SHA failure")


def import_site_catalog(path: Path) -> Any:
    path = path.resolve()
    if not path.is_file():
        raise Stage4Error(f"site_catalog.py not found: {path}")
    spec = importlib.util.spec_from_file_location("f2a_stage4_site_catalog", path)
    if spec is None or spec.loader is None:
        raise Stage4Error(f"cannot load site_catalog.py: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_graph_context(
    stage3_payload: Mapping[str, Any],
    site_catalog_tool: Path,
) -> GraphContext:
    source_stage2 = stage3_payload["source_stage2"]
    stage2_path = Path(str(source_stage2["path"])).resolve()
    stage2_payload = load_json_object(stage2_path, "Stage-2 catalog")
    if stage2_payload.get("stage") != EXPECTED_STAGE2_STAGE:
        raise Stage4Error("source Stage-2 file has an unexpected stage marker")
    expected_stage2_sha = str(source_stage2.get("sha256", ""))
    if expected_stage2_sha and sha256_file(stage2_path) != expected_stage2_sha:
        raise Stage4Error("source Stage-2 file SHA changed since Stage 3")

    source = stage2_payload.get("source")
    stage1_policy_meta = stage2_payload.get("stage1_policy")
    summary = stage2_payload.get("stage2_summary")
    if not isinstance(source, dict) or not isinstance(stage1_policy_meta, dict):
        raise Stage4Error("Stage-2 source or Stage-1 policy metadata is missing")
    if not isinstance(summary, dict):
        raise Stage4Error("Stage-2 summary is missing")

    source_path = Path(str(source.get("path", ""))).resolve()
    stage1_policy_path = Path(str(stage1_policy_meta.get("path", ""))).resolve()
    if not source_path.is_file():
        raise Stage4Error(f"mapped netlist not found: {source_path}")
    if not stage1_policy_path.is_file():
        raise Stage4Error(f"Stage-1 policy not found: {stage1_policy_path}")

    source_sha = sha256_file(source_path)
    policy_sha = sha256_file(stage1_policy_path)
    if source_sha != source.get("sha256"):
        raise Stage4Error("mapped netlist SHA changed since Stage 2")
    if policy_sha != stage1_policy_meta.get("sha256"):
        raise Stage4Error("Stage-1 policy SHA changed since Stage 2")

    module = import_site_catalog(site_catalog_tool)
    stage1_policy = module.load_policy(stage1_policy_path)
    netlist_text = source_path.read_text(encoding="utf-8", errors="strict")
    parsed = module.parse_design(netlist_text, stage1_policy)
    graph = module.build_dependency_graph(
        parsed,
        stage1_policy,
        str(summary.get("top_module")),
        frozenset({"sequential_data"}),
    )

    role_seed_map: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for node, roles in graph.direct_sink_roles.items():
        for role in roles:
            role_seed_map[str(role)].add(node)

    role_distances = {
        role: module.reverse_distances(graph.reverse, seeds)
        for role, seeds in sorted(role_seed_map.items())
    }
    indegree = {node: len(graph.reverse.get(node, ())) for node in graph.nodes}
    outdegree = {node: len(graph.forward.get(node, ())) for node in graph.nodes}

    return GraphContext(
        module=module,
        stage2_payload=stage2_payload,
        parsed=parsed,
        graph=graph,
        source_path=source_path,
        source_sha256=source_sha,
        stage1_policy_path=stage1_policy_path,
        stage1_policy_sha256=policy_sha,
        role_distances=role_distances,
        indegree=indegree,
        outdegree=outdegree,
    )


# ---------------------------------------------------------------------------
# Structural and semantic analysis
# ---------------------------------------------------------------------------


def matches_any(value: str, patterns: Sequence[re.Pattern[str]]) -> int:
    return sum(1 for pattern in patterns if pattern.search(value))


def bounded_tfo_features(
    graph_context: GraphContext,
    source_node: tuple[str, str],
    max_depth: int,
    max_nodes: int,
) -> dict[str, Any]:
    graph = graph_context.graph
    queue: deque[tuple[tuple[str, str], int]] = deque([(source_node, 0)])
    visited: set[tuple[str, str]] = {source_node}
    edge_count = 0
    maximum_depth_reached = 0
    truncated = False
    sequential_nodes: set[tuple[str, str]] = set()
    top_output_nodes: set[tuple[str, str]] = set()
    role_counts: Counter[str] = Counter()
    reconvergent_nodes: set[tuple[str, str]] = set()
    branching_nodes: set[tuple[str, str]] = set()

    while queue:
        node, depth = queue.popleft()
        maximum_depth_reached = max(maximum_depth_reached, depth)
        if node in graph.sequential_checkpoint_nodes:
            sequential_nodes.add(node)
        if node in graph.top_output_nodes:
            top_output_nodes.add(node)
        for role in graph.direct_sink_roles.get(node, ()):  # role at this net
            role_counts[str(role)] += 1
        if graph_context.indegree.get(node, 0) > 1:
            reconvergent_nodes.add(node)
        successors = tuple(graph.forward.get(node, ()))
        if len(successors) > 1:
            branching_nodes.add(node)
        if depth >= max_depth:
            if successors:
                truncated = True
            continue
        for successor in successors:
            edge_count += 1
            if successor in visited:
                continue
            if len(visited) >= max_nodes:
                truncated = True
                continue
            visited.add(successor)
            queue.append((successor, depth + 1))

    visited_without_source = visited - {source_node}
    return {
        "max_depth_limit": max_depth,
        "max_node_limit": max_nodes,
        "truncated": truncated,
        "maximum_depth_reached": maximum_depth_reached,
        "tfo_node_count_bounded": len(visited_without_source),
        "tfo_edge_count_bounded": edge_count,
        "reachable_sequential_checkpoint_count_bounded": len(sequential_nodes),
        "reachable_top_output_count_bounded": len(top_output_nodes),
        "reconvergent_node_count_bounded": len(reconvergent_nodes),
        "branching_node_count_bounded": len(branching_nodes),
        "reachable_sink_role_counts_bounded": dict(sorted(role_counts.items())),
    }


def role_distance(
    graph_context: GraphContext,
    role: str,
    node: tuple[str, str],
    cap: int,
) -> int | None:
    distance = graph_context.role_distances.get(role, {}).get(node)
    if distance is None or distance > cap:
        return None
    return int(distance)


def classify_semantics(
    site: Mapping[str, Any],
    detailed: Mapping[str, Any],
    graph_context: GraphContext,
    policy: ClassificationPolicy,
) -> dict[str, Any]:
    semantic_weights = policy.score_weights["semantic"]
    source_name = str(site["source_key"])
    semantic_source_name = source_name.lstrip("\\")
    module_name = str(site["module"])
    combined_name = f"{source_name} {module_name}"

    control_name_matches = matches_any(
        semantic_source_name,
        policy.semantic_rules["control_name_regexes"],
    )
    data_name_matches = matches_any(
        semantic_source_name,
        policy.semantic_rules["data_name_regexes"],
    )
    control_module_matches = matches_any(
        module_name,
        policy.semantic_rules["control_module_regexes"],
    )
    data_module_matches = matches_any(
        module_name,
        policy.semantic_rules["data_module_regexes"],
    )
    generated_name_matches = matches_any(
        source_name,
        policy.semantic_rules["generated_name_regexes"],
    )

    sink_roles = {
        str(role)
        for role, count in dict(site.get("sink_role_counts", {})).items()
        if int(count) > 0
    }
    reachable_roles = set(
        dict(detailed.get("reachable_sink_role_counts_bounded", {}))
    )
    direct_mux = "mux_select" in sink_roles
    near_mux_distance = detailed.get("distance_to_mux_select")
    arithmetic_signal = bool(
        site.get("source_kind") == "arithmetic_output"
        or "arithmetic_input" in sink_roles
        or "arithmetic_carry_input" in sink_roles
        or "arithmetic_input" in reachable_roles
        or "arithmetic_carry_input" in reachable_roles
    )
    state_site = bool(site.get("state_site"))

    control_score = 0.0
    control_score += min(2, control_name_matches) * float(
        semantic_weights["control_name_match"]
    )
    control_score += min(1, control_module_matches) * float(
        semantic_weights["control_module_match"]
    )
    if direct_mux:
        control_score += float(semantic_weights["direct_mux_select_consumer"])
    elif isinstance(near_mux_distance, int) and near_mux_distance <= 3:
        control_score += float(semantic_weights["near_mux_select_consumer"])
    if state_site:
        control_score += float(semantic_weights["state_site"])

    data_score = 0.0
    data_score += min(2, data_name_matches) * float(
        semantic_weights["data_name_match"]
    )
    data_score += min(1, data_module_matches) * float(
        semantic_weights["data_module_match"]
    )
    if arithmetic_signal:
        data_score += float(semantic_weights["arithmetic_driver_or_consumer"])
    if state_site:
        data_score += 0.5 * float(semantic_weights["state_site"])

    generated_penalty = (
        float(semantic_weights["generated_name_penalty"])
        if generated_name_matches
        else 0.0
    )
    control_adjusted = max(0.0, control_score - generated_penalty)
    data_adjusted = max(0.0, data_score - generated_penalty)

    if state_site:
        primary_class = "sequential_state"
    elif control_adjusted >= 4.0 and control_adjusted >= data_adjusted:
        primary_class = "control_path"
    elif data_adjusted >= 3.0:
        primary_class = "architectural_data"
    else:
        primary_class = "generic_observable"

    strongest = max(control_adjusted, data_adjusted)
    margin = abs(control_adjusted - data_adjusted)
    confidence = clamp01((strongest + 0.5 * margin) / 12.0)
    if primary_class == "generic_observable":
        confidence = max(0.25, 1.0 - clamp01(strongest / 8.0))
    if primary_class == "sequential_state":
        confidence = max(0.50, confidence)

    tags: list[str] = []
    if state_site:
        tags.append("sequential_state")
        if control_adjusted >= data_adjusted and control_adjusted >= 3.0:
            tags.append("control_state_semantics")
        elif data_adjusted >= 3.0:
            tags.append("architectural_state_semantics")
        else:
            tags.append("generic_state_semantics")
    if control_name_matches:
        tags.append("control_name")
    if data_name_matches:
        tags.append("architectural_data_name")
    if control_module_matches:
        tags.append("control_module")
    if data_module_matches:
        tags.append("data_module")
    if direct_mux or near_mux_distance is not None:
        tags.append("mux_control_reachable")
    if arithmetic_signal:
        tags.append("arithmetic_path")
    if generated_name_matches:
        tags.append("generated_name")
    if site.get("coarse_observability", {}).get("reaches_top_level_output"):
        tags.append("top_output_reachable")
    if site.get("static_safety", {}).get("touches_scan_structure"):
        tags.append("scan_touch")

    injection_kind = (
        "state_output_stuck_at"
        if state_site
        else "net_stuck_at"
    )

    return {
        "primary_class": primary_class,
        "injection_kind": injection_kind,
        "semantic_tags": sorted(set(tags)),
        "control_semantic_score_raw": round(control_adjusted, 6),
        "data_semantic_score_raw": round(data_adjusted, 6),
        "semantic_confidence": round(confidence, 6),
        "evidence": {
            "control_name_match_count": control_name_matches,
            "data_name_match_count": data_name_matches,
            "control_module_match_count": control_module_matches,
            "data_module_match_count": data_module_matches,
            "generated_name_match_count": generated_name_matches,
            "direct_mux_select_consumer": direct_mux,
            "distance_to_mux_select": near_mux_distance,
            "arithmetic_signal": arithmetic_signal,
            "name_and_module_text": combined_name,
        },
    }


def score_site(
    site: Mapping[str, Any],
    detailed: Mapping[str, Any],
    classification: Mapping[str, Any],
    policy: ClassificationPolicy,
) -> dict[str, float]:
    assertion_weights = policy.score_weights["assertion_relevance"]
    impact_weights = policy.score_weights["failure_impact"]
    class_name = str(classification["primary_class"])
    activity = dict(site.get("activity") or {})
    polarities = list(site.get("eligible_polarities", []))
    safety = dict(site.get("static_safety", {}))
    observability = dict(site.get("coarse_observability", {}))

    assertion_raw = float(assertion_weights[f"{class_name}_base"])
    if site.get("state_site"):
        assertion_raw += float(assertion_weights["state_site_bonus"])
    if "generated_name" not in classification.get("semantic_tags", []):
        assertion_raw += float(assertion_weights["semantic_name_bonus"])
    if len(polarities) == 2:
        assertion_raw += float(assertion_weights["both_polarities_bonus"])
    assertion_raw += min(
        float(assertion_weights["toggle_bonus_cap"]),
        safe_log2p(int(activity.get("binary_toggle_count", 0))) / 4.0,
    )
    nearest_distance = observability.get("nearest_checkpoint_distance")
    if isinstance(nearest_distance, int):
        assertion_raw += float(assertion_weights["near_checkpoint_bonus"]) / (
            1.0 + nearest_distance
        )
    if activity.get("unknown_seen"):
        assertion_raw -= float(assertion_weights["unknown_seen_penalty"])
    if safety.get("touches_scan_structure"):
        assertion_raw -= float(assertion_weights["scan_touch_penalty"])

    impact_raw = 0.0
    if observability.get("reaches_top_level_output"):
        impact_raw += float(impact_weights["top_output_reachable_bonus"])
    if observability.get("reaches_sequential_data"):
        impact_raw += float(
            impact_weights["sequential_checkpoint_reachable_bonus"]
        )
    impact_raw += min(
        float(impact_weights["fanout_log_bonus_cap"]),
        safe_log2p(int(site.get("logic_fanout", 0))) / 3.0,
    )
    impact_raw += min(
        float(impact_weights["tfo_log_bonus_cap"]),
        safe_log2p(int(detailed.get("tfo_node_count_bounded", 0))) / 5.0,
    )
    total_checkpoints = int(
        detailed.get("reachable_sequential_checkpoint_count_bounded", 0)
    ) + int(detailed.get("reachable_top_output_count_bounded", 0))
    impact_raw += min(
        float(impact_weights["reachable_checkpoint_log_bonus_cap"]),
        safe_log2p(total_checkpoints) / 3.0,
    )
    impact_raw += min(
        float(impact_weights["reconvergence_log_bonus_cap"]),
        safe_log2p(int(detailed.get("reconvergent_node_count_bounded", 0))) / 4.0,
    )
    if isinstance(nearest_distance, int):
        impact_raw += float(impact_weights["near_checkpoint_bonus"]) / (
            1.0 + nearest_distance
        )
    if len(polarities) == 2:
        impact_raw += float(impact_weights["both_polarities_bonus"])
    if activity.get("unknown_seen"):
        impact_raw -= float(impact_weights["unknown_seen_penalty"])

    assertion_score = normalize_score(assertion_raw)
    impact_score = normalize_score(impact_raw)
    selection_weights = policy.selection_rules["selection_score_weights"]
    combined = (
        float(selection_weights["assertion_relevance"]) * assertion_score
        + float(selection_weights["failure_impact"]) * impact_score
    )

    return {
        "assertion_relevance_score": assertion_score,
        "failure_impact_score": impact_score,
        "selection_score": round(clamp01(combined), 6),
    }


def analyze_sites(
    stage3_payload: Mapping[str, Any],
    graph_context: GraphContext,
    policy: ClassificationPolicy,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    max_depth = int(policy.structural_analysis["bounded_tfo_max_depth"])
    max_nodes = int(policy.structural_analysis["bounded_tfo_max_nodes"])
    role_cap = int(policy.structural_analysis["control_role_distance_cap"])

    output_sites: list[dict[str, Any]] = []
    class_counts: Counter[str] = Counter()
    class_fault_instance_counts: Counter[str] = Counter()
    injection_kind_counts: Counter[str] = Counter()
    class_by_module: dict[str, Counter[str]] = defaultdict(Counter)
    class_by_source_kind: dict[str, Counter[str]] = defaultdict(Counter)
    scan_touch_by_class: Counter[str] = Counter()
    tfo_truncated_count = 0
    semantic_low_confidence_count = 0
    warnings: list[str] = []

    for original in stage3_payload["sites"]:
        site = dict(original)
        if site.get("stage3_status") != EXPECTED_STAGE3_ELIGIBLE_STATUS:
            site["stage4_status"] = "not_classified_stage3_ineligible"
            site["detailed_structure"] = None
            site["classification"] = None
            site["scores"] = None
            site["primary_selection_eligible"] = False
            site["primary_selection_exclusion_reasons"] = [
                "stage3_ineligible"
            ]
            output_sites.append(site)
            continue

        node = graph_context.module.node_of(
            str(site["module"]),
            str(site["source_key"]),
        )
        detailed = bounded_tfo_features(
            graph_context,
            node,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )
        detailed["distance_to_mux_select"] = role_distance(
            graph_context,
            "mux_select",
            node,
            role_cap,
        )
        detailed["distance_to_sequential_data"] = role_distance(
            graph_context,
            "sequential_data",
            node,
            role_cap,
        )
        detailed["distance_to_arithmetic_input"] = role_distance(
            graph_context,
            "arithmetic_input",
            node,
            role_cap,
        )
        detailed["distance_to_arithmetic_carry_input"] = role_distance(
            graph_context,
            "arithmetic_carry_input",
            node,
            role_cap,
        )
        if detailed["truncated"]:
            tfo_truncated_count += 1

        classification = classify_semantics(
            site,
            detailed,
            graph_context,
            policy,
        )
        scores = score_site(site, detailed, classification, policy)
        class_name = str(classification["primary_class"])
        class_counts[class_name] += 1
        class_fault_instance_counts[class_name] += len(
            site.get("eligible_polarities", [])
        )
        injection_kind_counts[str(classification["injection_kind"])] += 1
        class_by_module[class_name][str(site["module"])] += 1
        class_by_source_kind[class_name][str(site["source_kind"])] += 1
        if site.get("static_safety", {}).get("touches_scan_structure"):
            scan_touch_by_class[class_name] += 1
        if float(classification["semantic_confidence"]) < float(
            policy.selection_rules["minimum_semantic_confidence"]
        ):
            semantic_low_confidence_count += 1

        selection_reasons: list[str] = []
        if (
            policy.selection_rules[
                "exclude_scan_touch_from_primary_selection"
            ]
            and site.get("static_safety", {}).get("touches_scan_structure")
        ):
            selection_reasons.append("scan_touch_excluded_from_primary_selection")
        if float(classification["semantic_confidence"]) < float(
            policy.selection_rules["minimum_semantic_confidence"]
        ):
            selection_reasons.append("semantic_confidence_below_minimum")
        if (
            policy.selection_rules["exclude_unknown_seen_only"]
            and site.get("activity", {}).get("unknown_seen")
        ):
            selection_reasons.append("unknown_seen_excluded_by_policy")

        site["stage4_status"] = "classified_candidate"
        site["detailed_structure"] = detailed
        site["classification"] = classification
        site["scores"] = scores
        site["primary_selection_eligible"] = not selection_reasons
        site["primary_selection_exclusion_reasons"] = selection_reasons
        output_sites.append(site)

    output_sites.sort(key=lambda item: str(item["site_id"]))

    eligible_count = sum(class_counts.values())
    stage3_expected = int(
        stage3_payload["stage3_summary"]["activity_eligible_site_count"]
    )
    if eligible_count != stage3_expected:
        raise Stage4Error(
            "Stage-4 classified-site count does not equal Stage-3 eligible count"
        )

    if tfo_truncated_count:
        warnings.append(
            f"{tfo_truncated_count} classified sites reached the bounded-TFO "
            "depth/node limit; truncated=true is retained as a feature"
        )
    scan_total = sum(scan_touch_by_class.values())
    if scan_total:
        warnings.append(
            f"{scan_total} Stage-3 eligible sites touch scan structure; they are "
            "classified but excluded from the primary 600-unique-site selection"
        )
    if semantic_low_confidence_count:
        warnings.append(
            f"{semantic_low_confidence_count} classified sites fall below the "
            "primary-selection semantic-confidence threshold"
        )

    summary = {
        "raw_site_count": len(output_sites),
        "stage3_activity_eligible_site_count": eligible_count,
        "classified_site_count": eligible_count,
        "classified_fault_instance_population_count": sum(
            class_fault_instance_counts.values()
        ),
        "by_primary_class_site_count": {
            name: class_counts[name] for name in CLASS_NAMES
        },
        "by_primary_class_fault_instance_population_count": {
            name: class_fault_instance_counts[name] for name in CLASS_NAMES
        },
        "by_injection_kind_site_count": dict(
            sorted(injection_kind_counts.items())
        ),
        "scan_touch_by_class_site_count": {
            name: scan_touch_by_class[name] for name in CLASS_NAMES
        },
        "bounded_tfo_truncated_site_count": tfo_truncated_count,
        "semantic_low_confidence_site_count": semantic_low_confidence_count,
        "primary_selection_eligible_site_count": sum(
            1
            for site in output_sites
            if site.get("primary_selection_eligible")
        ),
        "class_by_module_site_count": {
            class_name: dict(sorted(counter.items()))
            for class_name, counter in sorted(class_by_module.items())
        },
        "class_by_source_kind_site_count": {
            class_name: dict(sorted(counter.items()))
            for class_name, counter in sorted(class_by_source_kind.items())
        },
    }
    return output_sites, summary, warnings


# ---------------------------------------------------------------------------
# Deterministic quota selection
# ---------------------------------------------------------------------------


def normalized_eligible_polarities(site: Mapping[str, Any]) -> tuple[str, ...]:
    """Return a deterministic, validated workload-eligible polarity tuple."""

    values = site.get("eligible_polarities", [])
    if not isinstance(values, list):
        raise Stage4Error(
            f"eligible_polarities must be a list: {site.get('site_id')}"
        )
    normalized = tuple(
        sorted({str(value) for value in values}, key=lambda item: POLARITY_ORDER[item])
    )
    if not normalized:
        raise Stage4Error(
            f"selection-eligible site has no workload-eligible polarity: "
            f"{site.get('site_id')}"
        )
    unknown = sorted(set(normalized) - set(POLARITY_ORDER))
    if unknown:
        raise Stage4Error(
            f"unknown eligible polarities for {site.get('site_id')}: "
            + ", ".join(unknown)
        )
    return normalized


def build_selection_candidates(
    sites: Sequence[Mapping[str, Any]],
) -> dict[str, list[SelectionCandidate]]:
    """Build exactly one selection candidate per eligible physical/logical site."""

    by_class: dict[str, list[SelectionCandidate]] = {
        name: [] for name in CLASS_NAMES
    }
    seen_site_ids: set[str] = set()
    for site in sites:
        if not site.get("primary_selection_eligible"):
            continue
        classification = site.get("classification")
        scores = site.get("scores")
        if not isinstance(classification, dict) or not isinstance(scores, dict):
            continue
        site_id = str(site["site_id"])
        if site_id in seen_site_ids:
            raise Stage4Error(f"duplicate candidate site ID: {site_id}")
        seen_site_ids.add(site_id)
        class_name = str(classification["primary_class"])
        if class_name not in by_class:
            raise Stage4Error(f"unknown candidate class: {class_name}")
        by_class[class_name].append(
            SelectionCandidate(
                site_id=site_id,
                site_key=str(site["site_key"]),
                module=str(site["module"]),
                fault_class=class_name,
                eligible_polarities=normalized_eligible_polarities(site),
                score=float(scores["selection_score"]),
                assertion_relevance_score=float(
                    scores["assertion_relevance_score"]
                ),
                failure_impact_score=float(
                    scores["failure_impact_score"]
                ),
                semantic_confidence=float(
                    classification["semantic_confidence"]
                ),
                source_kind=str(site["source_kind"]),
                state_site=bool(site["state_site"]),
                scan_touch=bool(
                    site.get("static_safety", {}).get(
                        "touches_scan_structure"
                    )
                ),
            )
        )
    for class_name in CLASS_NAMES:
        by_class[class_name].sort(
            key=lambda item: (
                -item.score,
                -item.assertion_relevance_score,
                -item.failure_impact_score,
                -item.semantic_confidence,
                item.module,
                item.site_id,
            )
        )
    return by_class


def round_robin_select_sites(
    candidates: Sequence[SelectionCandidate],
    quota: int,
    module_cap: int,
    already_selected: Sequence[SelectionCandidate] = (),
) -> list[SelectionCandidate]:
    """Select distinct sites while distributing them across design modules."""

    selected: list[SelectionCandidate] = list(already_selected)
    selected_site_ids = {item.site_id for item in selected}
    module_counts = Counter(item.module for item in selected)

    buckets: dict[str, deque[SelectionCandidate]] = defaultdict(deque)
    for item in candidates:
        if item.site_id in selected_site_ids:
            continue
        buckets[item.module].append(item)

    modules = sorted(
        buckets,
        key=lambda module: (
            -buckets[module][0].score if buckets[module] else 0.0,
            module,
        ),
    )

    made_progress = True
    while len(selected) < quota and made_progress:
        made_progress = False
        for module in modules:
            if len(selected) >= quota:
                break
            if module_counts[module] >= module_cap:
                continue
            bucket = buckets[module]
            while bucket:
                item = bucket.popleft()
                if item.site_id in selected_site_ids:
                    continue
                selected.append(item)
                selected_site_ids.add(item.site_id)
                module_counts[module] += 1
                made_progress = True
                break
    return selected


def select_class_site_quota(
    candidates: Sequence[SelectionCandidate],
    quota: int,
    policy: ClassificationPolicy,
) -> list[SelectionCandidate]:
    """Select an exact unique-site quota for one semantic fault class."""

    unique_population = len({item.site_id for item in candidates})
    if unique_population < quota:
        raise Stage4Error(
            f"insufficient eligible unique sites: population={unique_population}, "
            f"quota={quota}"
        )

    initial_cap = max(
        1,
        math.ceil(
            quota
            * float(
                policy.selection_rules[
                    "initial_max_fraction_per_module_per_class"
                ]
            )
        ),
    )
    relaxed_cap = max(
        initial_cap,
        math.ceil(
            quota
            * float(
                policy.selection_rules[
                    "relaxed_max_fraction_per_module_per_class"
                ]
            )
        ),
    )

    selected = round_robin_select_sites(
        candidates,
        quota,
        module_cap=initial_cap,
    )
    if len(selected) < quota:
        selected = round_robin_select_sites(
            candidates,
            quota,
            module_cap=relaxed_cap,
            already_selected=selected,
        )
    if len(selected) < quota:
        selected = round_robin_select_sites(
            candidates,
            quota,
            module_cap=quota,
            already_selected=selected,
        )
    if len(selected) != quota:
        raise Stage4Error(
            f"unique-site selection could not fill quota: "
            f"selected={len(selected)}, quota={quota}"
        )
    if len({item.site_id for item in selected}) != quota:
        raise Stage4Error("unique-site selector returned duplicate site IDs")
    return selected


def polarity_pattern(polarities: Sequence[str]) -> str:
    normalized = tuple(sorted(set(polarities), key=lambda p: POLARITY_ORDER[p]))
    if normalized == ("SA0",):
        return "SA0_only"
    if normalized == ("SA1",):
        return "SA1_only"
    if normalized == ("SA0", "SA1"):
        return "SA0_and_SA1"
    raise Stage4Error(f"unsupported polarity pattern: {normalized}")


def build_selection(
    sites: Sequence[Mapping[str, Any]],
    policy: ClassificationPolicy,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Select 600 unique sites, then expand every eligible site polarity."""

    candidate_map = build_selection_candidates(sites)
    selected_all: list[SelectionCandidate] = []
    class_site_population_counts: dict[str, int] = {}
    class_fault_population_counts: dict[str, int] = {}

    for class_name in CLASS_NAMES:
        candidates = candidate_map[class_name]
        class_site_population_counts[class_name] = len(candidates)
        class_fault_population_counts[class_name] = sum(
            len(item.eligible_polarities) for item in candidates
        )
        quota = int(policy.quotas[class_name])
        selected_all.extend(
            select_class_site_quota(candidates, quota, policy)
        )

    selected_all.sort(
        key=lambda item: (
            CLASS_NAMES.index(item.fault_class),
            -item.score,
            item.module,
            item.site_id,
        )
    )

    expected_site_count = sum(policy.quotas.values())
    if len(selected_all) != expected_site_count:
        raise Stage4Error("selected unique-site count does not equal policy quota")
    if len({item.site_id for item in selected_all}) != expected_site_count:
        raise Stage4Error("selection contains duplicate sites across classes")

    selected_sites: list[dict[str, Any]] = []
    fault_instances: list[dict[str, Any]] = []

    for rank, item in enumerate(selected_all, start=1):
        selection_id = f"TS{rank:06d}"
        polarities = list(item.eligible_polarities)
        selected_site_record = {
            "selection_id": selection_id,
            "selection_rank": rank,
            "site_id": item.site_id,
            "site_key": item.site_key,
            "fault_class": item.fault_class,
            "eligible_polarities": polarities,
            "activity_derived_fault_instance_count": len(polarities),
            "module": item.module,
            "source_kind": item.source_kind,
            "state_site": item.state_site,
            "injection_kind": (
                "state_output_stuck_at"
                if item.state_site
                else "net_stuck_at"
            ),
            "selection_score": round(item.score, 6),
            "assertion_relevance_score": round(
                item.assertion_relevance_score, 6
            ),
            "failure_impact_score": round(
                item.failure_impact_score, 6
            ),
            "semantic_confidence": round(item.semantic_confidence, 6),
            "scan_touch": item.scan_touch,
        }
        selected_sites.append(selected_site_record)

        for polarity in polarities:
            fault_instances.append(
                {
                    "fault_id": f"TF{rank:06d}_{polarity}",
                    "selection_id": selection_id,
                    "selection_rank": rank,
                    "site_id": item.site_id,
                    "site_key": item.site_key,
                    "fault_class": item.fault_class,
                    "polarity": polarity,
                    "stuck_at": 0 if polarity == "SA0" else 1,
                    "module": item.module,
                    "source_kind": item.source_kind,
                    "state_site": item.state_site,
                    "injection_kind": (
                        "state_output_stuck_at"
                        if item.state_site
                        else "net_stuck_at"
                    ),
                    "selection_score": round(item.score, 6),
                    "assertion_relevance_score": round(
                        item.assertion_relevance_score, 6
                    ),
                    "failure_impact_score": round(
                        item.failure_impact_score, 6
                    ),
                    "semantic_confidence": round(
                        item.semantic_confidence, 6
                    ),
                    "scan_touch": item.scan_touch,
                }
            )

    selected_site_by_class = Counter(
        item["fault_class"] for item in selected_sites
    )
    selected_fault_by_class = Counter(
        item["fault_class"] for item in fault_instances
    )
    selected_by_polarity = Counter(
        item["polarity"] for item in fault_instances
    )
    selected_site_by_module = Counter(
        item["module"] for item in selected_sites
    )
    selected_fault_by_module = Counter(
        item["module"] for item in fault_instances
    )
    selected_pattern_counts = Counter(
        polarity_pattern(item["eligible_polarities"])
        for item in selected_sites
    )
    dual_polarity_site_count = selected_pattern_counts["SA0_and_SA1"]

    summary = {
        "target_unique_site_count": expected_site_count,
        "selected_unique_site_count": len(selected_sites),
        "selected_single_polarity_site_count": (
            len(selected_sites) - dual_polarity_site_count
        ),
        "selected_dual_polarity_site_count": dual_polarity_site_count,
        "selected_fault_instance_count": len(fault_instances),
        "candidate_unique_site_population_by_class": (
            class_site_population_counts
        ),
        "candidate_fault_instance_population_by_class": (
            class_fault_population_counts
        ),
        "unique_site_quota_by_class": dict(policy.quotas),
        "selected_unique_sites_by_class": {
            name: selected_site_by_class[name] for name in CLASS_NAMES
        },
        "selected_fault_instances_by_class": {
            name: selected_fault_by_class[name] for name in CLASS_NAMES
        },
        "selected_fault_instances_by_polarity": {
            name: selected_by_polarity[name] for name in ("SA0", "SA1")
        },
        "selected_sites_by_eligible_polarity_pattern": {
            name: selected_pattern_counts[name]
            for name in ("SA0_only", "SA1_only", "SA0_and_SA1")
        },
        "selected_unique_sites_by_module": dict(
            sorted(selected_site_by_module.items())
        ),
        "selected_fault_instances_by_module": dict(
            sorted(selected_fault_by_module.items())
        ),
        "scan_touch_selected_site_count": sum(
            1 for item in selected_sites if item["scan_touch"]
        ),
        "fault_count_policy": (
            "unbounded_by_quota; exactly all workload-eligible polarities "
            "for each of the 600 selected unique sites"
        ),
    }

    if summary["selected_unique_site_count"] != summary[
        "target_unique_site_count"
    ]:
        raise Stage4Error("selected unique-site count does not equal target")
    if summary["scan_touch_selected_site_count"] != 0:
        raise Stage4Error(
            "primary unique-site selection unexpectedly contains scan-touch sites"
        )
    for class_name in CLASS_NAMES:
        if selected_site_by_class[class_name] != policy.quotas[class_name]:
            raise Stage4Error(
                f"unique-site quota mismatch for class {class_name}"
            )

    expected_fault_count = sum(
        len(item["eligible_polarities"]) for item in selected_sites
    )
    if len(fault_instances) != expected_fault_count:
        raise Stage4Error(
            "fault-instance count does not equal activity-derived expansion"
        )
    if not (len(selected_sites) <= len(fault_instances) <= 2 * len(selected_sites)):
        raise Stage4Error("activity-derived fault count is outside valid bounds")
    return selected_sites, fault_instances, summary


# ---------------------------------------------------------------------------
# Payloads, reports, and validation
# ---------------------------------------------------------------------------


def candidate_digest(sites: Sequence[Mapping[str, Any]]) -> str:
    records = []
    for site in sites:
        if site.get("stage4_status") != "classified_candidate":
            continue
        records.append(
            {
                "site_id": site["site_id"],
                "site_key": site["site_key"],
                "eligible_polarities": site["eligible_polarities"],
                "detailed_structure": site["detailed_structure"],
                "classification": site["classification"],
                "scores": site["scores"],
                "primary_selection_eligible": site[
                    "primary_selection_eligible"
                ],
                "primary_selection_exclusion_reasons": site[
                    "primary_selection_exclusion_reasons"
                ],
            }
        )
    return canonical_json_digest(records)


def selection_digest(
    selected_sites: Sequence[Mapping[str, Any]],
    instances: Sequence[Mapping[str, Any]],
) -> str:
    return canonical_json_digest(
        {
            "selected_sites": list(selected_sites),
            "fault_instances": list(instances),
        }
    )


def make_candidate_payload(
    *,
    stage3_path: Path,
    stage3_payload: Mapping[str, Any],
    policy: ClassificationPolicy,
    graph_context: GraphContext,
    sites: list[dict[str, Any]],
    summary: Mapping[str, Any],
    warnings: Sequence[str],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "generated_at_utc": utc_now(),
        "stage": CANDIDATE_STAGE,
        "design": stage3_payload["design"],
        "workload": stage3_payload["workload"],
        "source_stage3": {
            "path": str(stage3_path.resolve()),
            "sha256": sha256_file(stage3_path),
            "activity_digest_sha256": stage3_payload[
                "activity_digest_sha256"
            ],
        },
        "source_stage2": dict(stage3_payload["source_stage2"]),
        "mapped_netlist": {
            "path": str(graph_context.source_path),
            "sha256": graph_context.source_sha256,
        },
        "stage1_policy": {
            "path": str(graph_context.stage1_policy_path),
            "sha256": graph_context.stage1_policy_sha256,
        },
        "classification_policy": {
            "path": str(policy.path),
            "sha256": policy.sha256,
            "name": policy.name,
            "schema_version": SCHEMA_VERSION,
        },
        "definitions": {
            "sequential_state": "workload-active sequential Q/QN state-output site; semantic tags distinguish control, architectural, and generic state",
            "control_path": "workload-active combinational or hierarchy-driven site with control/select/protocol semantic evidence",
            "architectural_data": "workload-active state or datapath site associated with PC, address, instruction, register-file, ALU, memory, CSR, or writeback data",
            "generic_observable": "workload-active and structurally observable site lacking sufficiently strong control or architectural-data semantics",
            "bounded_tfo": "forward dependency traversal bounded by policy depth and node limits; truncation is explicitly recorded",
            "primary_selection": "deterministic 600-unique-site plan; every workload-eligible polarity of each selected site is expanded into a Stage-5 fault instance",
        },
        "graph_summary": {
            "node_count": len(graph_context.graph.nodes),
            "edge_count": graph_context.graph.edge_count,
            "combinational_cell_edge_count": graph_context.graph.combinational_cell_edge_count,
            "hierarchy_edge_count": graph_context.graph.hierarchy_edge_count,
            "continuous_assign_edge_count": graph_context.graph.continuous_assign_edge_count,
            "skipped_non_simple_count": graph_context.graph.skipped_non_simple_count,
        },
        "stage4_summary": dict(summary),
        "warnings": list(warnings),
        "candidate_digest_sha256": candidate_digest(sites),
        "sites": sites,
    }


def make_selection_payload(
    *,
    candidate_path: Path,
    candidate_payload: Mapping[str, Any],
    policy: ClassificationPolicy,
    selected_sites: list[dict[str, Any]],
    instances: list[dict[str, Any]],
    summary: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "generated_at_utc": utc_now(),
        "stage": SELECTION_STAGE,
        "design": candidate_payload["design"],
        "workload": candidate_payload["workload"],
        "source_candidates": {
            "path": str(candidate_path.resolve()),
            "sha256": sha256_file(candidate_path),
            "candidate_digest_sha256": candidate_payload[
                "candidate_digest_sha256"
            ],
        },
        "classification_policy": dict(candidate_payload["classification_policy"]),
        "definitions": {
            "selection_id": "TSxxxxxx identifies one of exactly 600 selected unique sites",
            "fault_id": "TFxxxxxx_SA0 or TFxxxxxx_SA1; the numeric part equals the selected-site rank",
            "fault_instance": "one selected unique site and one workload-eligible stuck-at polarity",
            "polarity_expansion": "SA0 is generated only when golden activity observed logic 1; SA1 is generated only when golden activity observed logic 0; both are generated when both values were observed",
            "fault_count": "not quota-limited; it equals the sum of eligible polarity counts across the 600 selected sites",
            "materialization": "deferred to Stage 5; no per-fault directory or faulty netlist exists yet",
        },
        "selection_summary": dict(summary),
        "selection_digest_sha256": selection_digest(
            selected_sites, instances
        ),
        "selected_sites": selected_sites,
        "fault_instances": instances,
    }


def render_report(
    candidate_payload: Mapping[str, Any],
    selection_payload: Mapping[str, Any],
) -> str:
    summary = candidate_payload["stage4_summary"]
    selection = selection_payload["selection_summary"]
    lines: list[str] = []
    lines.append("Fault2Assertion Stage 4 Report")
    lines.append("=" * 80)
    lines.append("")
    lines.append(f"Design              : {candidate_payload['design']}")
    lines.append(f"Workload            : {candidate_payload['workload']}")
    lines.append(
        "Stage-3 digest      : "
        + candidate_payload["source_stage3"]["activity_digest_sha256"]
    )
    lines.append(
        "Candidate digest    : "
        + candidate_payload["candidate_digest_sha256"]
    )
    lines.append(
        "Selection digest    : "
        + selection_payload["selection_digest_sha256"]
    )
    lines.append("")
    lines.append("Classification population")
    lines.append("-" * 80)
    lines.append(
        f"Stage-3 eligible sites              : {summary['stage3_activity_eligible_site_count']}"
    )
    lines.append(
        f"Classified fault-instance population: {summary['classified_fault_instance_population_count']}"
    )
    for class_name in CLASS_NAMES:
        site_count = summary["by_primary_class_site_count"][class_name]
        fault_count = summary[
            "by_primary_class_fault_instance_population_count"
        ][class_name]
        scan_count = summary["scan_touch_by_class_site_count"][class_name]
        lines.append(
            f"{class_name:24s}: sites={site_count:6d} "
            f"fault_instances={fault_count:6d} scan_touch={scan_count:5d}"
        )
    lines.append("")
    lines.append("Detailed structural analysis")
    lines.append("-" * 80)
    lines.append(
        "Bounded-TFO truncated sites : "
        f"{summary['bounded_tfo_truncated_site_count']}"
    )
    lines.append(
        "Low-confidence sites        : "
        f"{summary['semantic_low_confidence_site_count']}"
    )
    lines.append(
        "Primary-selection sites     : "
        f"{summary['primary_selection_eligible_site_count']}"
    )
    lines.append("")
    lines.append("Primary unique-site plan")
    lines.append("-" * 80)
    lines.append(
        f"Target unique sites         : {selection['target_unique_site_count']}"
    )
    lines.append(
        f"Selected unique sites       : {selection['selected_unique_site_count']}"
    )
    lines.append(
        f"Generated fault instances   : {selection['selected_fault_instance_count']}"
    )
    lines.append(
        f"Single-polarity sites       : {selection['selected_single_polarity_site_count']}"
    )
    lines.append(
        f"Dual-polarity sites         : {selection['selected_dual_polarity_site_count']}"
    )
    for class_name in CLASS_NAMES:
        lines.append(
            f"{class_name:24s}: "
            f"site_population={selection['candidate_unique_site_population_by_class'][class_name]:6d} "
            f"site_quota={selection['unique_site_quota_by_class'][class_name]:4d} "
            f"selected_sites={selection['selected_unique_sites_by_class'][class_name]:4d} "
            f"generated_faults={selection['selected_fault_instances_by_class'][class_name]:4d}"
        )
    lines.append("")
    lines.append("Selected-site activity polarity patterns")
    lines.append("-" * 80)
    for pattern, count in selection[
        "selected_sites_by_eligible_polarity_pattern"
    ].items():
        lines.append(f"{pattern:16s}: {count}")
    lines.append("")
    lines.append("Generated fault-instance polarity counts")
    lines.append("-" * 80)
    for polarity, count in selection[
        "selected_fault_instances_by_polarity"
    ].items():
        lines.append(f"{polarity:8s}: {count}")
    lines.append("")
    lines.append("Warnings")
    lines.append("-" * 80)
    if candidate_payload["warnings"]:
        lines.extend(f"- {warning}" for warning in candidate_payload["warnings"])
    else:
        lines.append("- none")
    lines.append("")
    lines.append("Stage-4 interpretation")
    lines.append("-" * 80)
    lines.append("Exactly 600 unique workload-active sites were selected.")
    lines.append("Fault count was not capped or balanced by polarity.")
    lines.append("Every workload-eligible polarity of every selected site was expanded.")
    lines.append("No fault.json has been materialized yet.")
    lines.append("No mapped netlist has been modified.")
    lines.append("No faulty netlist or per-fault run directory has been generated.")
    lines.append("The selected TF IDs become the Stage-5 materialization input.")
    lines.append("")
    return "\n".join(lines)


def build_stage4(
    stage3_path: Path,
    policy_path: Path,
    site_catalog_tool: Path,
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    stage3_path = stage3_path.resolve()
    stage3_payload = load_json_object(stage3_path, "Stage-3 catalog")
    policy = load_policy(policy_path)
    validate_stage3_payload(stage3_path, stage3_payload, policy)
    graph_context = load_graph_context(stage3_payload, site_catalog_tool)
    sites, summary, warnings = analyze_sites(
        stage3_payload,
        graph_context,
        policy,
    )
    candidate_payload = make_candidate_payload(
        stage3_path=stage3_path,
        stage3_payload=stage3_payload,
        policy=policy,
        graph_context=graph_context,
        sites=sites,
        summary=summary,
        warnings=warnings,
    )
    selected_sites, instances, selection_summary = build_selection(
        sites, policy
    )
    return candidate_payload, selected_sites, instances, selection_summary


def validate_candidate_payload(payload: Mapping[str, Any]) -> None:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise Stage4Error("candidate schema mismatch")
    if payload.get("stage") != CANDIDATE_STAGE:
        raise Stage4Error("candidate stage marker mismatch")
    sites = payload.get("sites")
    summary = payload.get("stage4_summary")
    if not isinstance(sites, list) or not isinstance(summary, dict):
        raise Stage4Error("candidate payload missing sites or summary")
    if len(sites) != int(summary.get("raw_site_count", -1)):
        raise Stage4Error("candidate raw-site count mismatch")
    if candidate_digest(sites) != payload.get("candidate_digest_sha256"):
        raise Stage4Error("candidate digest mismatch")

    classified = [
        site for site in sites if site.get("stage4_status") == "classified_candidate"
    ]
    if len(classified) != int(summary.get("classified_site_count", -1)):
        raise Stage4Error("candidate classified-site count mismatch")
    class_counts = Counter(
        site["classification"]["primary_class"] for site in classified
    )
    for class_name in CLASS_NAMES:
        if class_counts[class_name] != summary["by_primary_class_site_count"][class_name]:
            raise Stage4Error(f"candidate class count mismatch: {class_name}")
    for site in classified:
        safety = site.get("static_safety", {})
        if not safety.get("clock_safe") or not safety.get("reset_set_safe"):
            raise Stage4Error(f"classified unsafe site: {site.get('site_id')}")
        if site.get("classification", {}).get("primary_class") not in CLASS_NAMES:
            raise Stage4Error(f"unknown classification: {site.get('site_id')}")


def validate_selection_payload(
    payload: Mapping[str, Any],
    candidate_payload: Mapping[str, Any],
) -> None:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise Stage4Error("selection schema mismatch")
    if payload.get("stage") != SELECTION_STAGE:
        raise Stage4Error("selection stage marker mismatch")
    selected_sites = payload.get("selected_sites")
    instances = payload.get("fault_instances")
    summary = payload.get("selection_summary")
    if (
        not isinstance(selected_sites, list)
        or not isinstance(instances, list)
        or not isinstance(summary, dict)
    ):
        raise Stage4Error(
            "selection payload missing selected_sites, fault_instances, or summary"
        )
    if selection_digest(selected_sites, instances) != payload.get(
        "selection_digest_sha256"
    ):
        raise Stage4Error("selection digest mismatch")

    target_sites = int(summary.get("target_unique_site_count", -1))
    if target_sites != 600:
        raise Stage4Error(
            f"expected a 600-unique-site target, got {target_sites}"
        )
    if len(selected_sites) != 600:
        raise Stage4Error(
            f"expected exactly 600 selected unique sites, got {len(selected_sites)}"
        )
    if len(selected_sites) != int(
        summary.get("selected_unique_site_count", -1)
    ):
        raise Stage4Error("selected unique-site count mismatch")
    if len(instances) != int(
        summary.get("selected_fault_instance_count", -1)
    ):
        raise Stage4Error("selected fault-instance count mismatch")

    candidate_by_id = {
        site["site_id"]: site for site in candidate_payload["sites"]
    }
    selected_site_ids: set[str] = set()
    selection_ids: set[str] = set()
    site_class_counts: Counter[str] = Counter()
    expected_fault_records: list[tuple[str, str, int]] = []

    for rank, selected in enumerate(selected_sites, start=1):
        selection_id = str(selected.get("selection_id", ""))
        if selection_id != f"TS{rank:06d}":
            raise Stage4Error(
                f"non-deterministic selected-site ordering: {selection_id}"
            )
        if int(selected.get("selection_rank", -1)) != rank:
            raise Stage4Error(
                f"selected-site rank mismatch: {selection_id}"
            )
        if selection_id in selection_ids:
            raise Stage4Error(f"duplicate selection ID: {selection_id}")
        selection_ids.add(selection_id)

        site_id = str(selected.get("site_id"))
        if site_id in selected_site_ids:
            raise Stage4Error(f"duplicate selected unique site: {site_id}")
        selected_site_ids.add(site_id)
        site = candidate_by_id.get(site_id)
        if site is None:
            raise Stage4Error(
                f"selected site missing from candidates: {site_id}"
            )
        if not site.get("primary_selection_eligible"):
            raise Stage4Error(
                f"selected site was not selection eligible: {site_id}"
            )
        if selected.get("scan_touch"):
            raise Stage4Error(
                f"scan-touch site selected unexpectedly: {site_id}"
            )
        class_name = str(selected.get("fault_class"))
        if class_name != site["classification"]["primary_class"]:
            raise Stage4Error(f"selected class mismatch: {site_id}")
        site_class_counts[class_name] += 1

        expected_polarities = list(normalized_eligible_polarities(site))
        if selected.get("eligible_polarities") != expected_polarities:
            raise Stage4Error(
                f"selected-site polarity metadata mismatch: {site_id}"
            )
        if int(selected.get("activity_derived_fault_instance_count", -1)) != len(
            expected_polarities
        ):
            raise Stage4Error(
                f"selected-site fault count mismatch: {site_id}"
            )
        for polarity in expected_polarities:
            expected_fault_records.append((site_id, polarity, rank))

    quotas = summary.get("unique_site_quota_by_class")
    if not isinstance(quotas, dict):
        raise Stage4Error("selection summary missing unique-site quotas")
    for class_name in CLASS_NAMES:
        expected = int(quotas.get(class_name, -1))
        if site_class_counts[class_name] != expected:
            raise Stage4Error(
                f"selected unique-site quota mismatch for {class_name}"
            )

    if len(instances) != len(expected_fault_records):
        raise Stage4Error(
            "fault-instance list does not contain every eligible site polarity"
        )
    fault_ids: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    polarity_counts: Counter[str] = Counter()
    fault_class_counts: Counter[str] = Counter()

    for instance, expected in zip(instances, expected_fault_records):
        expected_site_id, expected_polarity, expected_rank = expected
        expected_fault_id = f"TF{expected_rank:06d}_{expected_polarity}"
        fault_id = str(instance.get("fault_id", ""))
        if fault_id != expected_fault_id:
            raise Stage4Error(
                f"non-deterministic fault-instance ordering: {fault_id}"
            )
        if fault_id in fault_ids:
            raise Stage4Error(f"duplicate fault ID: {fault_id}")
        fault_ids.add(fault_id)
        site_id = str(instance.get("site_id"))
        polarity = str(instance.get("polarity"))
        if site_id != expected_site_id or polarity != expected_polarity:
            raise Stage4Error(
                f"fault expansion mismatch: {fault_id}"
            )
        pair = (site_id, polarity)
        if pair in pairs:
            raise Stage4Error(
                f"duplicate selected site/polarity: {pair}"
            )
        pairs.add(pair)
        if instance.get("scan_touch"):
            raise Stage4Error(
                f"scan-touch fault selected unexpectedly: {fault_id}"
            )
        candidate_site = candidate_by_id[site_id]
        if polarity not in candidate_site.get("eligible_polarities", []):
            raise Stage4Error(
                f"fault polarity is not workload eligible: {fault_id}"
            )
        polarity_counts[polarity] += 1
        fault_class_counts[str(instance.get("fault_class"))] += 1

    reported_polarities = summary.get(
        "selected_fault_instances_by_polarity", {}
    )
    for polarity in ("SA0", "SA1"):
        if polarity_counts[polarity] != int(
            reported_polarities.get(polarity, -1)
        ):
            raise Stage4Error(
                f"fault polarity count mismatch: {polarity}"
            )
    reported_fault_classes = summary.get(
        "selected_fault_instances_by_class", {}
    )
    for class_name in CLASS_NAMES:
        if fault_class_counts[class_name] != int(
            reported_fault_classes.get(class_name, -1)
        ):
            raise Stage4Error(
                f"fault-instance class count mismatch: {class_name}"
            )

    dual_count = sum(
        1
        for selected in selected_sites
        if len(selected["eligible_polarities"]) == 2
    )
    if len(instances) != len(selected_sites) + dual_count:
        raise Stage4Error(
            "fault-instance count must equal selected sites plus dual-polarity sites"
        )
    if summary.get("scan_touch_selected_site_count") != 0:
        raise Stage4Error("summary reports selected scan-touch sites")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def run_self_test(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    if sum(policy.quotas.values()) != 600:
        raise Stage4Error("self-test policy must select exactly 600 unique sites")

    synthetic_sites: list[dict[str, Any]] = []
    for class_index, class_name in enumerate(CLASS_NAMES):
        for item in range(policy.quotas[class_name] + 40):
            site_id = f"RS{class_index * 1000 + item + 1:06d}"
            state_site = class_name == "sequential_state"
            if item % 3 == 0:
                polarities = ["SA0"]
            elif item % 3 == 1:
                polarities = ["SA1"]
            else:
                polarities = ["SA0", "SA1"]
            classification = {
                "primary_class": class_name,
                "injection_kind": (
                    "state_output_stuck_at"
                    if state_site
                    else "net_stuck_at"
                ),
                "semantic_tags": [],
                "semantic_confidence": 0.8,
            }
            synthetic_sites.append(
                {
                    "site_id": site_id,
                    "site_key": f"m{item % 8}|s{item}",
                    "module": f"module_{item % 8}",
                    "source_kind": (
                        "sequential_output"
                        if state_site
                        else "combinational_output"
                    ),
                    "state_site": state_site,
                    "eligible_polarities": polarities,
                    "classification": classification,
                    "scores": {
                        "selection_score": round(
                            1.0 - item / 10000.0, 6
                        ),
                        "assertion_relevance_score": 0.8,
                        "failure_impact_score": 0.7,
                    },
                    "static_safety": {
                        "touches_scan_structure": False,
                    },
                    "primary_selection_eligible": True,
                }
            )

    selected_sites, instances, summary = build_selection(
        synthetic_sites, policy
    )
    if len(selected_sites) != 600:
        raise Stage4Error("self-test did not select 600 unique sites")
    if len({item["site_id"] for item in selected_sites}) != 600:
        raise Stage4Error("self-test unique-site selection contains duplicates")
    expected_faults = sum(
        len(item["eligible_polarities"]) for item in selected_sites
    )
    if len(instances) != expected_faults:
        raise Stage4Error("self-test fault expansion is incomplete")
    if summary["scan_touch_selected_site_count"] != 0:
        raise Stage4Error("self-test selected scan-touch sites")

    print(f"Policy                 : {policy.path}")
    print(f"Target unique sites    : {sum(policy.quotas.values())}")
    for class_name in CLASS_NAMES:
        print(
            f"{class_name:23s}: "
            f"sites={summary['selected_unique_sites_by_class'][class_name]} "
            f"faults={summary['selected_fault_instances_by_class'][class_name]}"
        )
    print(f"Generated faults       : {len(instances)}")
    print(
        "Fault polarities       : "
        f"{summary['selected_fault_instances_by_polarity']}"
    )
    print("Stage-4 self-test      : PASS")
    return 0


def run_classify_select(args: argparse.Namespace) -> int:
    candidate_output = args.candidates_output.resolve()
    selection_output = args.selection_output.resolve()
    report_output = args.report_output.resolve()

    for path in (candidate_output, selection_output, report_output):
        if path.exists() and not args.force:
            raise Stage4Error(
                f"refusing to overwrite existing file without --force: {path}"
            )

    (
        candidate_payload,
        selected_sites,
        instances,
        selection_summary,
    ) = build_stage4(
        args.stage3_json,
        args.policy,
        args.site_catalog_tool,
    )
    write_json(candidate_output, candidate_payload, force=args.force)
    selection_payload = make_selection_payload(
        candidate_path=candidate_output,
        candidate_payload=candidate_payload,
        policy=load_policy(args.policy),
        selected_sites=selected_sites,
        instances=instances,
        summary=selection_summary,
    )
    write_json(selection_output, selection_payload, force=args.force)
    report = render_report(candidate_payload, selection_payload)
    atomic_write_text(report_output, report, force=args.force)

    print(f"Stage-3 input          : {args.stage3_json.resolve()}")
    print(f"Candidates output      : {candidate_output}")
    print(f"Selection output       : {selection_output}")
    print(f"Report output          : {report_output}")
    print(
        "Classified sites       : "
        f"{candidate_payload['stage4_summary']['classified_site_count']}"
    )
    for class_name in CLASS_NAMES:
        print(
            f"{class_name:23s}: "
            f"population={candidate_payload['stage4_summary']['by_primary_class_site_count'][class_name]} "
            f"selected_sites={selection_summary['selected_unique_sites_by_class'][class_name]} "
            f"generated_faults={selection_summary['selected_fault_instances_by_class'][class_name]}"
        )
    print(
        "Selected unique sites : "
        f"{selection_summary['selected_unique_site_count']}"
    )
    print(
        "Generated faults      : "
        f"{selection_summary['selected_fault_instance_count']}"
    )
    print(
        "Fault polarities      : "
        f"{selection_summary['selected_fault_instances_by_polarity']}"
    )
    print(
        "Candidate digest      : "
        f"{candidate_payload['candidate_digest_sha256']}"
    )
    print(
        "Selection digest      : "
        f"{selection_payload['selection_digest_sha256']}"
    )
    print("Stage-4 classify/select: PASS")
    return 0


def run_validate_output(args: argparse.Namespace) -> int:
    candidate_path = args.candidates_json.resolve()
    selection_path = args.selection_json.resolve()
    candidate_payload = load_json_object(
        candidate_path, "Stage-4 candidates"
    )
    selection_payload = load_json_object(
        selection_path, "Stage-4 selection"
    )

    validate_candidate_payload(candidate_payload)
    validate_selection_payload(selection_payload, candidate_payload)

    source_candidates = selection_payload.get("source_candidates", {})
    if source_candidates.get("path") != str(candidate_path):
        raise Stage4Error("selection source-candidate path mismatch")
    if source_candidates.get("sha256") != sha256_file(candidate_path):
        raise Stage4Error("selection source-candidate SHA mismatch")

    stage3_path = Path(candidate_payload["source_stage3"]["path"])
    policy_path = Path(candidate_payload["classification_policy"]["path"])
    (
        rebuilt_candidates,
        rebuilt_selected_sites,
        rebuilt_instances,
        rebuilt_summary,
    ) = build_stage4(
        stage3_path,
        policy_path,
        args.site_catalog_tool,
    )
    if rebuilt_candidates["candidate_digest_sha256"] != candidate_payload[
        "candidate_digest_sha256"
    ]:
        raise Stage4Error("rebuilt candidate digest mismatch")
    if selection_digest(
        rebuilt_selected_sites, rebuilt_instances
    ) != selection_payload["selection_digest_sha256"]:
        raise Stage4Error("rebuilt selection digest mismatch")
    if rebuilt_summary != selection_payload["selection_summary"]:
        raise Stage4Error("rebuilt selection summary mismatch")

    print(f"Candidates JSON        : {candidate_path}")
    print(f"Selection JSON         : {selection_path}")
    print(
        "Classified sites       : "
        f"{candidate_payload['stage4_summary']['classified_site_count']}"
    )
    print(
        "Selected unique sites : "
        f"{selection_payload['selection_summary']['selected_unique_site_count']}"
    )
    print(
        "Generated faults      : "
        f"{selection_payload['selection_summary']['selected_fault_instance_count']}"
    )
    print(
        "Candidate digest      : "
        f"{candidate_payload['candidate_digest_sha256']}"
    )
    print(
        "Selection digest      : "
        f"{selection_payload['selection_digest_sha256']}"
    )
    print("Stage-4 validation     : PASS")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Classify Stage-3 activity-eligible fault sites, select exactly "
            "600 unique sites, and expand every workload-eligible polarity."
        )
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {PROGRAM_VERSION}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    self_test = subparsers.add_parser(
        "self-test",
        help="validate policy, unique-site quotas, and polarity expansion",
    )
    self_test.add_argument("--policy", type=Path, required=True)
    self_test.set_defaults(func=run_self_test)

    classify = subparsers.add_parser(
        "classify-select",
        help="build Stage-4 candidates and exact unique-site quota selection",
    )
    classify.add_argument("--stage3-json", type=Path, required=True)
    classify.add_argument("--policy", type=Path, required=True)
    classify.add_argument("--site-catalog-tool", type=Path, required=True)
    classify.add_argument("--candidates-output", type=Path, required=True)
    classify.add_argument("--selection-output", type=Path, required=True)
    classify.add_argument("--report-output", type=Path, required=True)
    classify.add_argument("--force", action="store_true")
    classify.set_defaults(func=run_classify_select)

    validate = subparsers.add_parser(
        "validate-output",
        help="fully rebuild and validate Stage-4 candidates and selection",
    )
    validate.add_argument("--candidates-json", type=Path, required=True)
    validate.add_argument("--selection-json", type=Path, required=True)
    validate.add_argument("--site-catalog-tool", type=Path, required=True)
    validate.set_defaults(func=run_validate_output)

    return parser

def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Stage4Error as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # fail closed, but identify unexpected defects
        print(f"ERROR: unexpected Stage-4 failure: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
