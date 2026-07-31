#!/usr/bin/env python3
"""Create a deterministic four-class Stage-4 mini selection for Stage-5 smoke.

The script reads the canonical Stage-4 candidates/selection and the already
validated full Stage-5 campaign.  It chooses one dual-polarity, active,
single-instance, modest-receiver-count site from each primary fault class,
renumbers the four sites as TS000001..TS000004, expands both polarities as eight
TF records, and writes a self-consistent mini selection with a new digest.

It never modifies the canonical Stage-4 files, full Stage-5 campaign, fault
specs, patches, or mapped netlist.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
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
CANDIDATE_STAGE = "stage_04_fault_type_classification"
SELECTION_STAGE = "stage_04_targeted_fault_selection_plan"
CAMPAIGN_STAGE = "stage_05_fault_characterization_campaign"


class MiniSelectionError(RuntimeError):
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
        raise MiniSelectionError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise MiniSelectionError(f"invalid {label} JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise MiniSelectionError(f"{label} must be one JSON object")
    return payload


def atomic_write_json(path: Path, payload: Mapping[str, Any], force: bool) -> None:
    path = path.resolve()
    if path.exists() and not force:
        raise MiniSelectionError(f"output exists; use --force intentionally: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def resolve_reference(value: Any, base_dir: Path) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def primary_class(candidate: Mapping[str, Any]) -> str:
    classification = candidate.get("classification")
    if not isinstance(classification, dict):
        return ""
    return str(classification.get("primary_class", ""))


def activity_is_usable(activity: Any) -> bool:
    if not isinstance(activity, dict):
        return False
    return (
        int(activity.get("instance_count", 0)) == 1
        and activity.get("seen_0") is True
        and activity.get("seen_1") is True
        and activity.get("unknown_seen") is False
        and int(activity.get("value_change_count", 0)) > 0
    )


def safety_is_usable(candidate: Mapping[str, Any]) -> bool:
    safety = candidate.get("static_safety")
    if not isinstance(safety, dict):
        return False
    return (
        safety.get("clock_safe") is True
        and safety.get("reset_set_safe") is True
        and not bool(safety.get("touches_scan_structure", False))
    )


def selection_score(candidate: Mapping[str, Any]) -> float:
    scores = candidate.get("scores")
    if not isinstance(scores, dict):
        return 0.0
    try:
        return float(scores.get("selection_score", 0.0))
    except (TypeError, ValueError):
        return 0.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a deterministic four-class Stage-5 mini selection."
    )
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--parent-selection", type=Path, required=True)
    parser.add_argument("--parent-campaign", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-receivers", type=int, default=32)
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    candidates_path = args.candidates.resolve()
    selection_path = args.parent_selection.resolve()
    campaign_path = args.parent_campaign.resolve()
    output_path = args.output.resolve()

    if args.max_receivers < 1:
        raise MiniSelectionError("--max-receivers must be at least 1")

    candidates = load_json(candidates_path, "Stage-4 candidates")
    parent_selection = load_json(selection_path, "Stage-4 selection")
    parent_campaign = load_json(campaign_path, "Stage-5 campaign")

    if candidates.get("stage") != CANDIDATE_STAGE:
        raise MiniSelectionError("candidate stage marker mismatch")
    if parent_selection.get("stage") != SELECTION_STAGE:
        raise MiniSelectionError("parent selection stage marker mismatch")
    if parent_campaign.get("stage") != CAMPAIGN_STAGE:
        raise MiniSelectionError("parent campaign stage marker mismatch")
    if candidates.get("design") != parent_selection.get("design"):
        raise MiniSelectionError("candidate/selection design mismatch")
    if candidates.get("workload") != parent_selection.get("workload"):
        raise MiniSelectionError("candidate/selection workload mismatch")
    if parent_campaign.get("design") != parent_selection.get("design"):
        raise MiniSelectionError("campaign/selection design mismatch")
    if parent_campaign.get("workload") != parent_selection.get("workload"):
        raise MiniSelectionError("campaign/selection workload mismatch")

    source_candidates = parent_selection.get("source_candidates")
    if not isinstance(source_candidates, dict):
        raise MiniSelectionError("parent selection missing source_candidates")
    if resolve_reference(source_candidates.get("path"), selection_path.parent) != candidates_path:
        raise MiniSelectionError("parent selection points to a different candidate file")
    if source_candidates.get("sha256") != sha256_file(candidates_path):
        raise MiniSelectionError("parent selection candidate SHA mismatch")
    if source_candidates.get("candidate_digest_sha256") != candidates.get(
        "candidate_digest_sha256"
    ):
        raise MiniSelectionError("parent selection candidate digest mismatch")

    campaign_source = parent_campaign.get("source_stage4")
    if not isinstance(campaign_source, dict):
        raise MiniSelectionError("parent campaign missing source_stage4")
    if campaign_source.get("candidates_sha256") != sha256_file(candidates_path):
        raise MiniSelectionError("parent campaign candidate SHA mismatch")
    if campaign_source.get("selection_sha256") != sha256_file(selection_path):
        raise MiniSelectionError("parent campaign selection SHA mismatch")
    if campaign_source.get("selection_digest_sha256") != parent_selection.get(
        "selection_digest_sha256"
    ):
        raise MiniSelectionError("parent campaign selection digest mismatch")

    candidate_records = candidates.get("sites")
    selected_records = parent_selection.get("selected_sites")
    instance_records = parent_selection.get("fault_instances")
    campaign_faults = parent_campaign.get("faults")
    if not isinstance(candidate_records, list):
        raise MiniSelectionError("candidates missing sites array")
    if not isinstance(selected_records, list):
        raise MiniSelectionError("selection missing selected_sites array")
    if not isinstance(instance_records, list):
        raise MiniSelectionError("selection missing fault_instances array")
    if not isinstance(campaign_faults, list):
        raise MiniSelectionError("campaign missing faults array")

    candidate_by_id = {
        str(item["site_id"]): item
        for item in candidate_records
        if isinstance(item, dict) and item.get("site_id")
    }
    instances_by_selection: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in instance_records:
        if isinstance(item, dict):
            instances_by_selection[str(item.get("selection_id", ""))].append(item)
    campaign_by_selection: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in campaign_faults:
        if isinstance(item, dict):
            campaign_by_selection[str(item.get("selection_id", ""))].append(item)

    eligible_by_class: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rejection_counts: dict[str, Counter[str]] = {
        class_name: Counter() for class_name in EXPECTED_CLASSES
    }

    for selected in selected_records:
        if not isinstance(selected, dict):
            continue
        original_selection_id = str(selected.get("selection_id", ""))
        site_id = str(selected.get("site_id", ""))
        candidate = candidate_by_id.get(site_id)
        if candidate is None:
            continue
        class_name = primary_class(candidate)
        if class_name not in EXPECTED_CLASSES:
            continue
        rejected = rejection_counts[class_name]

        if candidate.get("stage4_status") != "classified_candidate":
            rejected["not_classified"] += 1
            continue
        if not safety_is_usable(candidate):
            rejected["safety_or_scan"] += 1
            continue
        if not activity_is_usable(candidate.get("activity")):
            rejected["activity_or_instance_count"] += 1
            continue

        polarities = sorted(str(item) for item in selected.get("eligible_polarities", []))
        if polarities != ["SA0", "SA1"]:
            rejected["not_dual_polarity"] += 1
            continue

        parent_instances = instances_by_selection.get(original_selection_id, [])
        parent_instance_by_polarity = {
            str(item.get("polarity")): item for item in parent_instances
        }
        if set(parent_instance_by_polarity) != {"SA0", "SA1"}:
            rejected["parent_instances_incomplete"] += 1
            continue

        fault_records = campaign_by_selection.get(original_selection_id, [])
        fault_by_polarity = {str(item.get("polarity")): item for item in fault_records}
        if set(fault_by_polarity) != {"SA0", "SA1"}:
            rejected["campaign_faults_incomplete"] += 1
            continue

        representative_record = fault_by_polarity["SA0"]
        spec_path = resolve_reference(
            representative_record.get("fault_spec"), campaign_path.parent
        )
        if not spec_path.is_file():
            rejected["representative_spec_missing"] += 1
            continue
        spec = load_json(spec_path, f"representative fault spec {site_id}")
        receivers = spec.get("receiver_signals")
        if not isinstance(receivers, list) or not receivers:
            rejected["no_receivers"] += 1
            continue
        if len(receivers) > args.max_receivers:
            rejected["too_many_receivers"] += 1
            continue
        if not activity_is_usable(spec.get("site", {}).get("activity")):
            rejected["spec_activity_mismatch"] += 1
            continue

        eligible_by_class[class_name].append(
            {
                "selected": selected,
                "candidate": candidate,
                "parent_instances": parent_instance_by_polarity,
                "receiver_count": len(receivers),
                "score": selection_score(candidate),
                "original_rank": int(selected.get("selection_rank", 10**9)),
                "representative_spec": str(spec_path),
            }
        )

    chosen: list[dict[str, Any]] = []
    for class_name in EXPECTED_CLASSES:
        options = eligible_by_class.get(class_name, [])
        if not options:
            raise MiniSelectionError(
                f"no usable mini-smoke site for class {class_name}; "
                f"rejections={dict(rejection_counts[class_name])}"
            )
        options.sort(
            key=lambda item: (
                -float(item["score"]),
                int(item["receiver_count"]),
                int(item["original_rank"]),
                str(item["candidate"]["site_id"]),
            )
        )
        chosen.append(options[0])

    mini_selected: list[dict[str, Any]] = []
    mini_instances: list[dict[str, Any]] = []
    chosen_report: list[dict[str, Any]] = []

    for new_rank, item in enumerate(chosen, start=1):
        selected = copy.deepcopy(item["selected"])
        candidate = item["candidate"]
        original_selection_id = str(selected["selection_id"])
        original_rank = int(selected["selection_rank"])
        new_selection_id = f"TS{new_rank:06d}"

        selected["selection_id"] = new_selection_id
        selected["selection_rank"] = new_rank
        selected["eligible_polarities"] = ["SA0", "SA1"]
        selected["activity_derived_fault_instance_count"] = 2
        selected["mini_smoke_origin"] = {
            "selection_id": original_selection_id,
            "selection_rank": original_rank,
            "representative_spec": item["representative_spec"],
        }
        mini_selected.append(selected)

        for polarity in ("SA0", "SA1"):
            parent_instance = copy.deepcopy(item["parent_instances"][polarity])
            stuck_at = 0 if polarity == "SA0" else 1
            parent_instance["fault_id"] = f"TF{new_rank:06d}_{polarity}"
            parent_instance["selection_id"] = new_selection_id
            parent_instance["selection_rank"] = new_rank
            parent_instance["polarity"] = polarity
            parent_instance["stuck_at"] = stuck_at
            parent_instance["mini_smoke_origin"] = {
                "fault_id": item["parent_instances"][polarity]["fault_id"],
                "selection_id": original_selection_id,
                "selection_rank": original_rank,
            }
            mini_instances.append(parent_instance)

        chosen_report.append(
            {
                "new_selection_id": new_selection_id,
                "original_selection_id": original_selection_id,
                "site_id": candidate["site_id"],
                "fault_class": primary_class(candidate),
                "source_kind": candidate.get("source_kind"),
                "module": candidate.get("module"),
                "source_net": candidate.get("source_net"),
                "selection_score": item["score"],
                "receiver_count": item["receiver_count"],
                "value_change_count": candidate["activity"]["value_change_count"],
            }
        )

    site_class_counts = Counter(
        primary_class(candidate_by_id[str(item["site_id"])])
        for item in mini_selected
    )
    fault_class_counts = Counter(str(item["fault_class"]) for item in mini_instances)
    polarity_counts = Counter(str(item["polarity"]) for item in mini_instances)

    summary = {
        "plan_kind": "stage5_mini_smoke_four_class_dual_polarity",
        "target_unique_site_count": len(EXPECTED_CLASSES),
        "selected_unique_site_count": len(mini_selected),
        "selected_fault_instance_count": len(mini_instances),
        "selected_single_polarity_site_count": 0,
        "selected_dual_polarity_site_count": len(mini_selected),
        "selected_sites_by_eligible_polarity_pattern": {
            "SA0+SA1": len(mini_selected)
        },
        "unique_site_quota_by_class": {
            class_name: 1 for class_name in EXPECTED_CLASSES
        },
        "selected_unique_sites_by_class": {
            class_name: site_class_counts[class_name]
            for class_name in EXPECTED_CLASSES
        },
        "selected_fault_instances_by_class": {
            class_name: fault_class_counts[class_name]
            for class_name in EXPECTED_CLASSES
        },
        "selected_fault_instances_by_polarity": {
            polarity: polarity_counts[polarity] for polarity in ("SA0", "SA1")
        },
        "scan_touch_selected_site_count": 0,
        "max_receiver_count": args.max_receivers,
    }

    payload = {
        "schema_version": parent_selection.get("schema_version"),
        "program_version": parent_selection.get("program_version"),
        "generated_at_utc": utc_now(),
        "stage": SELECTION_STAGE,
        "design": parent_selection["design"],
        "workload": parent_selection["workload"],
        "source_candidates": copy.deepcopy(parent_selection["source_candidates"]),
        "classification_policy": copy.deepcopy(
            parent_selection.get("classification_policy")
        ),
        "definitions": {
            "selection_id": "TSxxxxxx is local to this four-site mini smoke selection",
            "fault_id": "TFxxxxxx_SA0 or TFxxxxxx_SA1 is local to this mini smoke selection",
            "mini_smoke": (
                "one active, dual-polarity, single-instance site from each Stage-4 "
                "primary class; selected only to validate the Stage-5 pipeline"
            ),
            "materialization": "performed independently under runs/stage5_dev; canonical Stage-4 and full Stage-5 artifacts remain immutable",
        },
        "selection_summary": summary,
        "selection_digest_sha256": "",
        "selected_sites": mini_selected,
        "fault_instances": mini_instances,
        "mini_smoke_provenance": {
            "selector_script": str(Path(__file__).resolve()),
            "selector_script_sha256": sha256_file(Path(__file__).resolve()),
            "parent_candidates": str(candidates_path),
            "parent_candidates_sha256": sha256_file(candidates_path),
            "parent_candidate_digest_sha256": candidates.get(
                "candidate_digest_sha256"
            ),
            "parent_selection": str(selection_path),
            "parent_selection_sha256": sha256_file(selection_path),
            "parent_selection_digest_sha256": parent_selection.get(
                "selection_digest_sha256"
            ),
            "parent_campaign": str(campaign_path),
            "parent_campaign_sha256": sha256_file(campaign_path),
            "parent_campaign_digest_sha256": parent_campaign.get(
                "campaign_digest_sha256"
            ),
            "chosen_sites": chosen_report,
        },
    }
    payload["selection_digest_sha256"] = canonical_json_digest(
        {
            "selected_sites": mini_selected,
            "fault_instances": mini_instances,
        }
    )

    # Independent fail-closed validation before writing.
    if len(mini_selected) != 4 or len(mini_instances) != 8:
        raise MiniSelectionError("mini selection count invariant failed")
    if set(site_class_counts) != set(EXPECTED_CLASSES):
        raise MiniSelectionError(
            f"mini selection class coverage failed: {dict(site_class_counts)}"
        )
    expected_fault_ids = [
        f"TF{rank:06d}_{polarity}"
        for rank in range(1, 5)
        for polarity in ("SA0", "SA1")
    ]
    actual_fault_ids = [str(item["fault_id"]) for item in mini_instances]
    if actual_fault_ids != expected_fault_ids:
        raise MiniSelectionError(
            f"mini fault ordering/IDs failed: {actual_fault_ids}"
        )
    recomputed = canonical_json_digest(
        {
            "selected_sites": payload["selected_sites"],
            "fault_instances": payload["fault_instances"],
        }
    )
    if recomputed != payload["selection_digest_sha256"]:
        raise MiniSelectionError("internal mini selection digest mismatch")

    atomic_write_json(output_path, payload, force=args.force)

    print(f"Mini selection         : {output_path}")
    print(f"Selected sites         : {len(mini_selected)}")
    print(f"Fault instances        : {len(mini_instances)}")
    print(f"Selection digest       : {payload['selection_digest_sha256']}")
    for record in chosen_report:
        print(
            f"{record['new_selection_id']} {record['fault_class']:23s} "
            f"site={record['site_id']} receivers={record['receiver_count']} "
            f"toggles={record['value_change_count']}"
        )
    print("Mini selection build   : PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MiniSelectionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print(f"ERROR: unexpected mini-selection failure: {exc}", file=sys.stderr)
        raise SystemExit(1)
