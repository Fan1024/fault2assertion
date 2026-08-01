#!/usr/bin/env python3
"""Immutable-manifest, site-bounded Stage-5 batch campaign orchestrator.

This program keeps the established Stage-5 execution path intact:

* Native-first execution;
* verdict-selected detector routing;
* OBSERVE and DIAGNOSTIC_QUARANTINE only for supported detectors;
* generic oracle construction and independent validation;
* work-directory cleanup only after oracle validation PASS;
* durable per-fault status and result reuse.

The batch layer adds only the state needed for a resumable full campaign:

* one immutable full campaign manifest built from stage_05_campaign.json;
* one mutable campaign_state.json containing the through_site control and
  derived progress;
* fixed-seed, fault-class-stratified ordering of selected sites;
* site-level execution boundaries without assuming two faults per site;
* fail-fast retention for non-scientific errors;
* reconstruction of campaign progress from per-fault status files.

No database, scheduler, checkpoint database, or additional runner script is
required.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = "2.0"
PROGRAM_VERSION = "2.0.0"

SOURCE_STAGE5_CAMPAIGN_MARKER = "stage_05_fault_characterization_campaign"
SOURCE_STAGE5_FAULT_MARKER = "stage_05_fault_materialization"

MANIFEST_FILENAME = "campaign_manifest.json"
LEGACY_MANIFEST_FILENAME = "pilot_manifest.json"
STATE_FILENAME = "campaign_state.json"

PASS_STATE = "ORACLE_VALIDATED_CLEANED"
BLOCKED_STATES = {
    "BLOCKED_UNREGISTERED_DETECTOR",
    "BLOCKED_AMBIGUOUS_DETECTOR",
    "BLOCKED_UNSUPPORTED_REGISTERED_DETECTOR",
}
NATIVE_SCIENTIFIC = {
    "OUTPUT_MATCH",
    "OUTPUT_MISMATCH",
    "TIMEOUT",
    "EXISTING_ASSERTION_DETECTED",
}
DIAGNOSTIC_SCIENTIFIC = {
    "DIAGNOSTIC_OUTPUT_MATCH",
    "DIAGNOSTIC_OUTPUT_MISMATCH",
    "DIAGNOSTIC_TIMEOUT",
}
FAULT_ID_RE = re.compile(r"^TF(?P<rank>\d{6})_SA(?P<sa>[01])$")
SELECTION_ID_RE = re.compile(r"^TS(?P<rank>\d{6})$")

_KEEP = object()


class BatchError(RuntimeError):
    """Controlled campaign failure with a user-actionable message."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BatchError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BatchError(f"invalid {label} JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BatchError(f"{label} must contain one JSON object: {path}")
    return value


def write_json(path: Path, value: Mapping[str, Any], overwrite: bool = True) -> None:
    path = path.expanduser().resolve()
    if path.exists() and not overwrite:
        raise BatchError(f"refusing to overwrite: {path}")
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


def canonical_json_digest(value: Any) -> str:
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


def command(args: Sequence[str], *, env: Mapping[str, str] | None = None) -> int:
    print("+ " + " ".join(str(item) for item in args), flush=True)
    completed = subprocess.run(
        [str(item) for item in args],
        env=dict(env) if env is not None else None,
        check=False,
    )
    return int(completed.returncode)


def resolve_recorded_path(value: Any, base_dir: Path, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise BatchError(f"{label} must be a non-empty path string")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def archive_path(path: Path, reason: str) -> Path:
    if not path.exists():
        return path
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = path.with_name(f"{path.name}_{reason}_{stamp}")
    counter = 1
    while target.exists():
        target = path.with_name(f"{path.name}_{reason}_{stamp}_{counter}")
        counter += 1
    path.rename(target)
    return target


def require_checkpoint(path: Path) -> dict[str, Any]:
    """Validate the frozen Phase2-G5 independent smoke report."""

    report = load_json(path, "Stage5 Phase2-G5 validation report")
    if report.get("status") != "PASS":
        raise BatchError("Phase2-G5 validation status is not PASS")
    if report.get("gate") != "stage5_phase2_g5_minimal_oracle_validation":
        raise BatchError(
            "unexpected Phase2-G5 validation gate: "
            f"{report.get('gate')!r}"
        )
    fault_id = str(report.get("fault_id", ""))
    if FAULT_ID_RE.fullmatch(fault_id) is None:
        raise BatchError(f"invalid Phase2-G5 validation fault ID: {fault_id!r}")

    claims = report.get("gate_claims")
    if not isinstance(claims, dict):
        raise BatchError("Phase2-G5 validation report has no gate_claims object")
    required_true = (
        "g2_g3_g4_merged",
        "raw_facts_preserved",
        "derived_conclusions_validated",
        "exact_injection_signal_stored_privately",
        "exact_detector_cycle_stored_privately",
        "prompt_exact_labels_hidden",
        "oracle_digest_valid",
        "prompt_context_digest_valid",
    )
    missing = [name for name in required_true if claims.get(name) is not True]
    if missing:
        raise BatchError(
            "Phase2-G5 validation claims are incomplete: " + ", ".join(missing)
        )
    if claims.get("sva_generated") is not False:
        raise BatchError("Phase2-G5 smoke unexpectedly generated SVA")
    return report


# ---------------------------------------------------------------------------
# Canonical Stage-5 campaign loading and immutable manifest construction
# ---------------------------------------------------------------------------


def validate_fault_id(fault_id: str) -> re.Match[str]:
    match = FAULT_ID_RE.fullmatch(fault_id)
    if match is None:
        raise BatchError(f"invalid Stage-5 fault ID: {fault_id!r}")
    return match


def validate_selection_id(selection_id: str) -> re.Match[str]:
    match = SELECTION_ID_RE.fullmatch(selection_id)
    if match is None:
        raise BatchError(f"invalid Stage-4 selection ID: {selection_id!r}")
    return match


def stable_site_key(
    seed: int,
    source_campaign_digest: str,
    site_key: str,
) -> str:
    text = f"{seed}:{source_campaign_digest}:{site_key}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_campaign_digest(payload: Mapping[str, Any]) -> str:
    stable = {
        "source_stage4": payload.get("source_stage4"),
        "mapped_netlist": payload.get("mapped_netlist"),
        "selected_sites": payload.get("selected_sites"),
        "faults": payload.get("faults"),
    }
    actual = canonical_json_digest(stable)
    recorded = payload.get("campaign_digest_sha256")
    if recorded is not None and recorded != actual:
        raise BatchError(
            "canonical Stage-5 campaign digest mismatch\n"
            f"  recorded: {recorded}\n"
            f"  actual:   {actual}"
        )
    return actual


def active_stage5_program_version(root: Path) -> str:
    tool = root / "scripts/fault_characterization/stage5_faults.py"
    if not tool.is_file():
        raise BatchError(f"active Stage-5 fault tool not found: {tool}")
    module_name = "f2a_stage5_batch_version_probe"
    spec = importlib.util.spec_from_file_location(module_name, tool)
    if spec is None or spec.loader is None:
        raise BatchError(f"cannot import active Stage-5 fault tool: {tool}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise BatchError(f"cannot load active Stage-5 fault tool {tool}: {exc}") from exc
    version = getattr(module, "PROGRAM_VERSION", None)
    if not isinstance(version, str) or not version:
        raise BatchError(f"active Stage-5 fault tool has no PROGRAM_VERSION: {tool}")
    return version


def load_and_validate_source_campaign(
    path: Path,
    *,
    expected_program_version: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    path = path.resolve()
    payload = load_json(path, "canonical Stage-5 campaign")
    if payload.get("stage") != SOURCE_STAGE5_CAMPAIGN_MARKER:
        raise BatchError(
            "source campaign stage mismatch: "
            f"{payload.get('stage')!r}"
        )
    source_version = payload.get("program_version")
    if source_version != expected_program_version:
        raise BatchError(
            "canonical Stage-5 campaign is incompatible with the active materializer\n"
            f"  campaign version: {source_version!r}\n"
            f"  active version:   {expected_program_version!r}\n"
            "Regenerate the canonical Stage-5 campaign before preparing the batch."
        )

    selected_sites = payload.get("selected_sites")
    source_faults = payload.get("faults")
    if not isinstance(selected_sites, list) or not selected_sites:
        raise BatchError("source Stage-5 campaign has no selected_sites array")
    if not isinstance(source_faults, list) or not source_faults:
        raise BatchError("source Stage-5 campaign has no faults array")

    summary = payload.get("campaign_summary")
    if not isinstance(summary, dict):
        raise BatchError("source Stage-5 campaign has no campaign_summary")
    if int(summary.get("selected_unique_site_count", -1)) != len(selected_sites):
        raise BatchError("source campaign selected-site count mismatch")
    if int(summary.get("fault_instance_count", -1)) != len(source_faults):
        raise BatchError("source campaign fault-instance count mismatch")

    site_by_selection: dict[str, dict[str, Any]] = {}
    site_id_seen: set[str] = set()
    site_key_seen: set[str] = set()
    for raw in selected_sites:
        if not isinstance(raw, dict):
            raise BatchError("source selected_sites contains a non-object")
        site = dict(raw)
        selection_id = str(site.get("selection_id", ""))
        selection_match = validate_selection_id(selection_id)
        selection_rank = int(site.get("selection_rank", -1))
        if selection_rank != int(selection_match.group("rank")):
            raise BatchError(
                f"selection rank mismatch for {selection_id}: {selection_rank}"
            )
        site_id = str(site.get("site_id", ""))
        site_key = str(site.get("site_key", ""))
        if not site_id or not site_key:
            raise BatchError(f"selected site identity is incomplete: {selection_id}")
        if selection_id in site_by_selection:
            raise BatchError(f"duplicate selection_id: {selection_id}")
        if site_id in site_id_seen:
            raise BatchError(f"duplicate selected site_id: {site_id}")
        if site_key in site_key_seen:
            raise BatchError(f"duplicate selected site_key: {site_key}")
        site_id_seen.add(site_id)
        site_key_seen.add(site_key)

        polarities = site.get("eligible_polarities")
        if not isinstance(polarities, list) or not polarities:
            raise BatchError(f"eligible_polarities missing for {selection_id}")
        normalized = []
        for value in polarities:
            polarity = str(value)
            if polarity not in {"SA0", "SA1"}:
                raise BatchError(
                    f"unsupported eligible polarity for {selection_id}: {polarity}"
                )
            if polarity not in normalized:
                normalized.append(polarity)
        normalized.sort(key=lambda item: int(item[-1]))
        site["eligible_polarities"] = normalized
        expected_count = int(site.get("activity_derived_fault_instance_count", -1))
        if expected_count != len(normalized):
            raise BatchError(
                f"activity-derived fault count mismatch for {selection_id}: "
                f"recorded={expected_count}, polarities={len(normalized)}"
            )
        site_by_selection[selection_id] = site

    faults_by_selection: dict[str, list[dict[str, Any]]] = defaultdict(list)
    fault_id_seen: set[str] = set()
    validated_source_faults: list[dict[str, Any]] = []

    for raw in source_faults:
        if not isinstance(raw, dict):
            raise BatchError("source faults contains a non-object")
        record = dict(raw)
        fault_id = str(record.get("fault_id", ""))
        fault_match = validate_fault_id(fault_id)
        if fault_id in fault_id_seen:
            raise BatchError(f"duplicate source fault ID: {fault_id}")
        fault_id_seen.add(fault_id)

        selection_id = str(record.get("selection_id", ""))
        selection_match = validate_selection_id(selection_id)
        site = site_by_selection.get(selection_id)
        if site is None:
            raise BatchError(
                f"source fault references unknown selection_id: {fault_id} -> {selection_id}"
            )
        if fault_match.group("rank") != selection_match.group("rank"):
            raise BatchError(
                f"fault/selection numeric rank mismatch: {fault_id} / {selection_id}"
            )
        if str(record.get("site_id", "")) != str(site["site_id"]):
            raise BatchError(f"fault/site_id mismatch for {fault_id}")

        polarity = str(record.get("polarity", ""))
        stuck_at = record.get("stuck_at")
        if polarity not in {"SA0", "SA1"}:
            raise BatchError(f"invalid polarity for {fault_id}: {polarity!r}")
        if stuck_at not in (0, 1) or polarity != f"SA{stuck_at}":
            raise BatchError(f"polarity/stuck-at mismatch for {fault_id}")
        if polarity != f"SA{fault_match.group('sa')}":
            raise BatchError(f"fault ID/polarity mismatch for {fault_id}")
        if polarity not in site["eligible_polarities"]:
            raise BatchError(
                f"fault polarity is not eligible for its site: {fault_id}"
            )

        spec_path = resolve_recorded_path(
            record.get("fault_spec"),
            path.parent,
            f"fault_spec for {fault_id}",
        )
        if not spec_path.is_file():
            raise BatchError(f"canonical fault spec not found: {spec_path}")
        spec = load_json(spec_path, f"canonical fault spec {fault_id}")
        if spec.get("stage") != SOURCE_STAGE5_FAULT_MARKER:
            raise BatchError(f"fault spec stage mismatch for {fault_id}")
        if spec.get("program_version") != expected_program_version:
            raise BatchError(
                f"fault spec/materializer version mismatch for {fault_id}: "
                f"spec={spec.get('program_version')!r}, "
                f"active={expected_program_version!r}"
            )
        exact_checks = {
            "fault_id": fault_id,
            "selection_id": selection_id,
            "site_id": str(site["site_id"]),
            "site_key": str(site["site_key"]),
            "polarity": polarity,
            "stuck_at": stuck_at,
        }
        for key, expected in exact_checks.items():
            actual = spec.get(key)
            if actual != expected:
                raise BatchError(
                    f"fault spec mismatch for {fault_id}: "
                    f"{key}={actual!r}, expected={expected!r}"
                )

        recorded_digest = record.get("fault_spec_digest_sha256")
        spec_digest = spec.get("fault_spec_digest_sha256")
        if recorded_digest != spec_digest:
            raise BatchError(f"fault-spec digest mismatch for {fault_id}")

        record["resolved_fault_spec"] = str(spec_path)
        record["fault_spec_file_sha256"] = sha256_file(spec_path)
        record["fault_spec_digest_sha256"] = spec_digest
        record["site_key"] = str(site["site_key"])
        record["selection_rank"] = int(site["selection_rank"])
        record["module"] = site.get("module")
        record["source_kind"] = site.get("source_kind")
        record["state_site"] = site.get("state_site")
        faults_by_selection[selection_id].append(record)
        validated_source_faults.append(record)

    for selection_id, site in site_by_selection.items():
        records = faults_by_selection.get(selection_id, [])
        records.sort(key=lambda item: (int(item["stuck_at"]), str(item["fault_id"])))
        actual_polarities = [str(item["polarity"]) for item in records]
        if actual_polarities != list(site["eligible_polarities"]):
            raise BatchError(
                f"site/fault polarity expansion mismatch for {selection_id}: "
                f"site={site['eligible_polarities']}, faults={actual_polarities}"
            )
        if len(records) != int(site["activity_derived_fault_instance_count"]):
            raise BatchError(f"site/fault count mismatch for {selection_id}")

    normalized_sites = [
        dict(site_by_selection[str(item["selection_id"])])
        for item in selected_sites
    ]
    return payload, normalized_sites, validated_source_faults


def order_sites(
    selected_sites: Sequence[Mapping[str, Any]],
    *,
    seed: int,
    source_digest: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    class_order: list[str] = []
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in selected_sites:
        site = dict(raw)
        class_name = str(site.get("fault_class", "UNKNOWN"))
        if class_name not in class_order:
            class_order.append(class_name)
        buckets[class_name].append(site)

    for class_name in class_order:
        buckets[class_name].sort(
            key=lambda item: (
                stable_site_key(seed, source_digest, str(item["site_key"])),
                str(item["site_id"]),
            )
        )

    ordered: list[dict[str, Any]] = []
    while True:
        progress = False
        for class_name in class_order:
            bucket = buckets[class_name]
            if not bucket:
                continue
            ordered.append(bucket.pop(0))
            progress = True
        if not progress:
            break

    if len(ordered) != len(selected_sites):
        raise BatchError("seeded site ordering lost or duplicated selected sites")
    return ordered, class_order


def manifest_digest_payload(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": manifest.get("schema_version"),
        "program_version": manifest.get("program_version"),
        "kind": manifest.get("kind"),
        "source_stage5_campaign": manifest.get("source_stage5_campaign"),
        "source_stage4": manifest.get("source_stage4"),
        "mapped_netlist": manifest.get("mapped_netlist"),
        "ordering": manifest.get("ordering"),
        "universe": manifest.get("universe"),
        "sites": manifest.get("sites"),
        "faults": manifest.get("faults"),
    }


def build_full_manifest(
    *,
    root: Path,
    campaign_root: Path,
    checkpoint: Path,
    source_campaign_path: Path,
    seed: int,
) -> dict[str, Any]:
    active_version = active_stage5_program_version(root)
    source, selected_sites, source_faults = load_and_validate_source_campaign(
        source_campaign_path,
        expected_program_version=active_version,
    )
    source_digest = source_campaign_digest(source)
    ordered_sites, class_order = order_sites(
        selected_sites,
        seed=seed,
        source_digest=source_digest,
    )

    faults_by_selection: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in source_faults:
        faults_by_selection[str(record["selection_id"])].append(record)
    for records in faults_by_selection.values():
        records.sort(key=lambda item: (int(item["stuck_at"]), str(item["fault_id"])))

    site_records: list[dict[str, Any]] = []
    fault_records: list[dict[str, Any]] = []
    fault_order = 0

    for site_order, site in enumerate(ordered_sites, start=1):
        selection_id = str(site["selection_id"])
        records = faults_by_selection[selection_id]
        fault_ids = [str(item["fault_id"]) for item in records]
        site_record = {
            "site_order": site_order,
            "selection_id": selection_id,
            "selection_rank": int(site["selection_rank"]),
            "site_id": str(site["site_id"]),
            "site_key": str(site["site_key"]),
            "fault_class": str(site.get("fault_class", "UNKNOWN")),
            "module": site.get("module"),
            "source_kind": site.get("source_kind"),
            "state_site": site.get("state_site"),
            "eligible_polarities": list(site["eligible_polarities"]),
            "fault_ids": fault_ids,
            "fault_instance_count": len(fault_ids),
            "selection_score": site.get("selection_score"),
            "assertion_relevance_score": site.get("assertion_relevance_score"),
            "failure_impact_score": site.get("failure_impact_score"),
            "semantic_confidence": site.get("semantic_confidence"),
        }
        site_records.append(site_record)

        site_dir = campaign_root / "sites" / f"{site_order:04d}_{selection_id}"
        for source_fault in records:
            fault_order += 1
            fault_id = str(source_fault["fault_id"])
            fault_dir = site_dir / fault_id
            original_spec = Path(str(source_fault["resolved_fault_spec"])).resolve()
            fault_records.append(
                {
                    "fault_order": fault_order,
                    "site_order": site_order,
                    "selection_id": selection_id,
                    "selection_rank": int(site["selection_rank"]),
                    "site_id": str(site["site_id"]),
                    "site_key": str(site["site_key"]),
                    "fault_id": fault_id,
                    "polarity": str(source_fault["polarity"]),
                    "stuck_at": int(source_fault["stuck_at"]),
                    "fault_class": str(site.get("fault_class", "UNKNOWN")),
                    "module": site.get("module"),
                    "source_kind": site.get("source_kind"),
                    "state_site": site.get("state_site"),
                    "fault_root": str(fault_dir.resolve()),
                    "fault_json": str((fault_dir / "fault.json").resolve()),
                    "original_fault_json": str(original_spec),
                    "original_fault_json_sha256": str(
                        source_fault["fault_spec_file_sha256"]
                    ),
                    "fault_spec_digest_sha256": source_fault.get(
                        "fault_spec_digest_sha256"
                    ),
                }
            )

    polarity_patterns = Counter(
        (
            "SA0_and_SA1"
            if site["eligible_polarities"] == ["SA0", "SA1"]
            else f"{site['eligible_polarities'][0]}_only"
        )
        for site in site_records
    )
    universe = {
        "site_count": len(site_records),
        "fault_count": len(fault_records),
        "single_polarity_site_count": sum(
            1 for site in site_records if site["fault_instance_count"] == 1
        ),
        "dual_polarity_site_count": sum(
            1 for site in site_records if site["fault_instance_count"] == 2
        ),
        "sites_by_fault_class": dict(
            Counter(str(site["fault_class"]) for site in site_records)
        ),
        "faults_by_fault_class": dict(
            Counter(str(fault["fault_class"]) for fault in fault_records)
        ),
        "faults_by_polarity": dict(
            Counter(str(fault["polarity"]) for fault in fault_records)
        ),
        "sites_by_eligible_polarity_pattern": dict(polarity_patterns),
    }

    source_stage4 = source.get("source_stage4")
    mapped_netlist = source.get("mapped_netlist")
    if not isinstance(source_stage4, dict) or not isinstance(mapped_netlist, dict):
        raise BatchError("source Stage-5 campaign provenance is incomplete")

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "kind": "stage5_full_site_campaign_manifest",
        "generated_at_utc": utc_now(),
        "repository_root": str(root.resolve()),
        "campaign_root": str(campaign_root.resolve()),
        "smoke_validation_report": str(checkpoint.resolve()),
        "source_stage5_campaign": {
            "path": str(source_campaign_path.resolve()),
            "sha256": sha256_file(source_campaign_path.resolve()),
            "campaign_digest_sha256": source_digest,
            "program_version": source.get("program_version"),
            "active_materializer_program_version": active_version,
        },
        "source_stage4": dict(source_stage4),
        "mapped_netlist": dict(mapped_netlist),
        "ordering": {
            "unit": "selected_site",
            "method": "fault_class_round_robin_seeded_sha256_v1",
            "seed": seed,
            "hash_identity": "seed:source_campaign_digest:site_key",
            "class_order": class_order,
            "membership_policy": "all canonical selected sites and all activity-derived fault instances",
        },
        "universe": universe,
        "sites": site_records,
        "faults": fault_records,
    }
    manifest["manifest_digest_sha256"] = canonical_json_digest(
        manifest_digest_payload(manifest)
    )
    return manifest


def validate_new_manifest(manifest: Mapping[str, Any], path: Path) -> None:
    if manifest.get("kind") != "stage5_full_site_campaign_manifest":
        raise BatchError(f"unexpected campaign manifest kind: {manifest.get('kind')!r}")
    expected_digest = manifest.get("manifest_digest_sha256")
    actual_digest = canonical_json_digest(manifest_digest_payload(manifest))
    if expected_digest != actual_digest:
        raise BatchError(
            "immutable campaign manifest digest mismatch\n"
            f"  expected: {expected_digest}\n"
            f"  actual:   {actual_digest}\n"
            f"  path:     {path}"
        )

    sites = manifest.get("sites")
    faults = manifest.get("faults")
    universe = manifest.get("universe")
    if not isinstance(sites, list) or not isinstance(faults, list):
        raise BatchError("campaign manifest is missing sites or faults arrays")
    if not isinstance(universe, dict):
        raise BatchError("campaign manifest is missing universe summary")
    if len(sites) != int(universe.get("site_count", -1)):
        raise BatchError("campaign manifest site count mismatch")
    if len(faults) != int(universe.get("fault_count", -1)):
        raise BatchError("campaign manifest fault count mismatch")

    site_orders = [int(item.get("site_order", -1)) for item in sites if isinstance(item, dict)]
    if site_orders != list(range(1, len(sites) + 1)):
        raise BatchError("campaign site_order is not contiguous from 1")

    fault_by_id: dict[str, Mapping[str, Any]] = {}
    for record in faults:
        if not isinstance(record, dict):
            raise BatchError("campaign faults contains a non-object")
        fault_id = str(record.get("fault_id", ""))
        validate_fault_id(fault_id)
        if fault_id in fault_by_id:
            raise BatchError(f"duplicate campaign fault ID: {fault_id}")
        fault_by_id[fault_id] = record

    seen_selection: set[str] = set()
    for site in sites:
        if not isinstance(site, dict):
            raise BatchError("campaign sites contains a non-object")
        selection_id = str(site.get("selection_id", ""))
        validate_selection_id(selection_id)
        if selection_id in seen_selection:
            raise BatchError(f"duplicate campaign selection ID: {selection_id}")
        seen_selection.add(selection_id)
        fault_ids = site.get("fault_ids")
        polarities = site.get("eligible_polarities")
        if not isinstance(fault_ids, list) or not fault_ids:
            raise BatchError(f"site has no fault_ids: {selection_id}")
        if not isinstance(polarities, list) or not polarities:
            raise BatchError(f"site has no eligible_polarities: {selection_id}")
        if len(fault_ids) != int(site.get("fault_instance_count", -1)):
            raise BatchError(f"site fault count mismatch: {selection_id}")
        actual = []
        for fault_id in fault_ids:
            record = fault_by_id.get(str(fault_id))
            if record is None:
                raise BatchError(f"site references unknown fault: {fault_id}")
            if record.get("selection_id") != selection_id:
                raise BatchError(f"fault/site selection mismatch: {fault_id}")
            if int(record.get("site_order", -1)) != int(site["site_order"]):
                raise BatchError(f"fault/site order mismatch: {fault_id}")
            actual.append(str(record.get("polarity")))
        if actual != list(polarities):
            raise BatchError(
                f"site polarity/fault mapping mismatch: {selection_id}: "
                f"{polarities} != {actual}"
            )


def manifest_path_for(campaign_root: Path) -> Path:
    new_path = campaign_root / MANIFEST_FILENAME
    if new_path.is_file():
        return new_path
    legacy = campaign_root / LEGACY_MANIFEST_FILENAME
    if legacy.is_file():
        return legacy
    raise BatchError(
        f"no {MANIFEST_FILENAME} or {LEGACY_MANIFEST_FILENAME} found: {campaign_root}"
    )


def normalize_legacy_manifest(
    payload: Mapping[str, Any],
    path: Path,
) -> dict[str, Any]:
    raw_faults = payload.get("faults")
    if not isinstance(raw_faults, list) or not raw_faults:
        raise BatchError("legacy pilot manifest has no faults array")

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    first_order: dict[str, int] = {}
    normalized_faults: list[dict[str, Any]] = []
    for raw in raw_faults:
        if not isinstance(raw, dict):
            raise BatchError("legacy pilot manifest contains invalid fault record")
        record = dict(raw)
        fault_id = str(record.get("fault_id", ""))
        match = validate_fault_id(fault_id)
        base = str(record.get("base_fault_id") or f"TF{match.group('rank')}")
        record["fault_id"] = fault_id
        record["legacy_base_fault_id"] = base
        grouped[base].append(record)
        order = int(record.get("order", 10**9))
        first_order[base] = min(first_order.get(base, order), order)
        normalized_faults.append(record)

    ordered_bases = sorted(grouped, key=lambda base: (first_order[base], base))
    sites: list[dict[str, Any]] = []
    fault_order = 0
    final_faults: list[dict[str, Any]] = []
    for site_order, base in enumerate(ordered_bases, start=1):
        records = sorted(
            grouped[base],
            key=lambda item: (
                int(item.get("stuck_at", int(validate_fault_id(str(item["fault_id"])).group("sa")))),
                str(item["fault_id"]),
            ),
        )
        fault_ids = [str(item["fault_id"]) for item in records]
        sites.append(
            {
                "site_order": site_order,
                "selection_id": base,
                "selection_rank": site_order,
                "site_id": base,
                "site_key": base,
                "fault_class": records[0].get("fault_class", "UNKNOWN"),
                "module": records[0].get("module"),
                "eligible_polarities": [
                    f"SA{int(item.get('stuck_at', int(validate_fault_id(str(item['fault_id'])).group('sa'))))}"
                    for item in records
                ],
                "fault_ids": fault_ids,
                "fault_instance_count": len(fault_ids),
                "legacy": True,
            }
        )
        for record in records:
            fault_order += 1
            record["fault_order"] = fault_order
            record["site_order"] = site_order
            record["selection_id"] = base
            final_faults.append(record)

    return {
        "schema_version": "legacy",
        "program_version": payload.get("program_version"),
        "kind": "legacy_stage5_site_based_batch_pilot",
        "campaign_root": str(path.parent.resolve()),
        "manifest_digest_sha256": sha256_file(path),
        "universe": {
            "site_count": len(sites),
            "fault_count": len(final_faults),
        },
        "sites": sites,
        "faults": final_faults,
        "legacy_source_manifest": str(path.resolve()),
    }


def load_runtime_manifest(campaign_root: Path) -> tuple[dict[str, Any], Path]:
    path = manifest_path_for(campaign_root.resolve())
    payload = load_json(path, "campaign manifest")
    if path.name == MANIFEST_FILENAME:
        validate_new_manifest(payload, path)
        return payload, path
    return normalize_legacy_manifest(payload, path), path


def prepare_campaign(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    campaign_root = args.campaign_root.resolve()
    checkpoint = args.checkpoint.resolve()
    source_campaign = args.source_campaign.resolve()
    require_checkpoint(checkpoint)

    if campaign_root.exists():
        manifest = campaign_root / MANIFEST_FILENAME
        if manifest.is_file():
            payload = load_json(manifest, "existing immutable campaign manifest")
            validate_new_manifest(payload, manifest)
            print(f"Campaign already prepared: {campaign_root}")
            print(f"Manifest                 : {manifest}")
            print("The immutable manifest was not modified.")
            ensure_campaign_state(campaign_root, payload, default_through_site=args.through_site)
            return 0
        raise BatchError(
            "campaign root already exists without an immutable manifest; "
            f"review or remove it manually: {campaign_root}"
        )

    manifest = build_full_manifest(
        root=root,
        campaign_root=campaign_root,
        checkpoint=checkpoint,
        source_campaign_path=source_campaign,
        seed=args.seed,
    )
    campaign_root.mkdir(parents=True, exist_ok=False)
    write_json(campaign_root / MANIFEST_FILENAME, manifest, overwrite=False)
    state = ensure_campaign_state(
        campaign_root,
        manifest,
        default_through_site=args.through_site,
    )

    universe = manifest["universe"]
    target = state["target"]
    print()
    print("=" * 70)
    print("Stage-5 immutable full campaign preparation: PASS")
    print("=" * 70)
    print(f"Campaign root       : {campaign_root}")
    print(f"Manifest            : {campaign_root / MANIFEST_FILENAME}")
    print(f"Manifest digest     : {manifest['manifest_digest_sha256']}")
    print(f"Seed                : {manifest['ordering']['seed']}")
    print(f"Universe sites      : {universe['site_count']}")
    print(f"Universe faults     : {universe['fault_count']}")
    print(f"Through site        : {state['control']['through_site']}")
    print(f"Target sites        : {target['site_count']}")
    print(f"Target faults       : {target['fault_count']}")
    print("Per-fault directories and monitors are generated lazily at execution time.")
    return 0


# ---------------------------------------------------------------------------
# Mutable campaign state and site-level progress reconstruction
# ---------------------------------------------------------------------------


def parse_through_site(value: Any, total_sites: int) -> int | str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "all":
            return "all"
        try:
            value = int(normalized)
        except ValueError as exc:
            raise BatchError(
                f"through_site must be an integer within 1..{total_sites} or 'all'"
            ) from exc
    if not isinstance(value, int) or isinstance(value, bool):
        raise BatchError(
            f"through_site must be an integer within 1..{total_sites} or 'all'"
        )
    if not 1 <= value <= total_sites:
        raise BatchError(
            f"through_site is outside 1..{total_sites}: {value}"
        )
    return value


def resolved_through_site(value: int | str, total_sites: int) -> int:
    return total_sites if value == "all" else int(value)


def fault_record_map(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for record in manifest.get("faults", []):
        if not isinstance(record, dict):
            raise BatchError("campaign manifest contains invalid fault record")
        fault_id = str(record.get("fault_id", ""))
        if fault_id in result:
            raise BatchError(f"duplicate runtime fault ID: {fault_id}")
        result[fault_id] = record
    return result


def current_status(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "schema_version": SCHEMA_VERSION,
            "state": "PREPARED",
            "updated_at_utc": utc_now(),
        }
    return load_json(path, "fault status")


def update_status(
    path: Path,
    *,
    remove_keys: Iterable[str] = (),
    **values: Any,
) -> None:
    status = current_status(path)
    for key in remove_keys:
        status.pop(key, None)
    status.update(values)
    status["schema_version"] = SCHEMA_VERSION
    status["updated_at_utc"] = utc_now()
    write_json(path, status)


def state_path(campaign_root: Path) -> Path:
    return campaign_root / STATE_FILENAME


def default_state(
    campaign_root: Path,
    manifest: Mapping[str, Any],
    through_site: int | str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "kind": "stage5_campaign_state",
        "campaign_root": str(campaign_root.resolve()),
        "manifest": {
            "path": str(manifest_path_for(campaign_root.resolve()).resolve()),
            "digest_sha256": str(manifest["manifest_digest_sha256"]),
        },
        "control": {
            "through_site": through_site,
            "error_policy": "fail_fast",
        },
        "run": {
            "state": "READY",
            "last_started_at_utc": None,
            "last_finished_at_utc": None,
            "last_error": None,
        },
        "milestones": [],
        "generated_at_utc": utc_now(),
        "updated_at_utc": utc_now(),
    }


def ensure_campaign_state(
    campaign_root: Path,
    manifest: Mapping[str, Any],
    *,
    default_through_site: Any = _KEEP,
) -> dict[str, Any]:
    path = state_path(campaign_root)
    total_sites = int(manifest["universe"]["site_count"])
    if path.is_file():
        state = load_json(path, "campaign state")
        recorded = state.get("manifest")
        if not isinstance(recorded, dict) or recorded.get("digest_sha256") != manifest.get(
            "manifest_digest_sha256"
        ):
            raise BatchError(
                "campaign_state.json does not belong to the immutable manifest"
            )
        control = state.get("control")
        if not isinstance(control, dict):
            raise BatchError("campaign state has no control object")
        parse_through_site(control.get("through_site"), total_sites)
        return refresh_campaign_state(campaign_root, manifest)

    if default_through_site is _KEEP:
        if manifest.get("kind") == "legacy_stage5_site_based_batch_pilot":
            default_through_site = "all"
        else:
            raise BatchError(
                "campaign_state.json is missing; set an explicit through_site "
                "with prepare or set-through-site before running"
            )
    through_site = parse_through_site(default_through_site, total_sites)
    state = default_state(campaign_root, manifest, through_site)
    write_json(path, state, overwrite=False)
    return refresh_campaign_state(campaign_root, manifest)


def site_progress(
    site: Mapping[str, Any],
    faults: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    fault_rows: list[dict[str, Any]] = []
    states: list[str] = []
    for fault_id in site["fault_ids"]:
        record = faults[str(fault_id)]
        fault_dir = Path(str(record["fault_root"])).resolve()
        status = current_status(fault_dir / "status.json")
        state = str(status.get("state", "PREPARED"))
        states.append(state)
        fault_rows.append(
            {
                "fault_id": str(fault_id),
                "state": state,
                "native_status": status.get("native_status"),
                "route": status.get("route"),
                "observe_status": status.get("observe_status"),
                "diagnostic_quarantine_status": status.get(
                    "diagnostic_quarantine_status"
                ),
                "failure_reason": status.get("failure_reason"),
            }
        )

    if states and all(state == PASS_STATE for state in states):
        site_state = "PASSED"
    elif any(state in BLOCKED_STATES for state in states):
        site_state = "BLOCKED"
    elif any(state == "FAILED" for state in states):
        site_state = "FAILED"
    else:
        site_state = "INCOMPLETE"

    return {
        "site_order": int(site["site_order"]),
        "selection_id": site.get("selection_id"),
        "site_id": site.get("site_id"),
        "site_key": site.get("site_key"),
        "fault_class": site.get("fault_class"),
        "fault_count": len(fault_rows),
        "site_state": site_state,
        "faults": fault_rows,
    }


def append_completed_milestone(state: dict[str, Any]) -> None:
    run_state = state["run"]["state"]
    if run_state not in {"BOUNDARY_COMPLETE", "ALL_COMPLETE"}:
        return
    target = state["target"]
    key = {
        "manifest_digest_sha256": state["manifest"]["digest_sha256"],
        "through_site_resolved": target["through_site_resolved"],
    }
    milestones = state.setdefault("milestones", [])
    if not isinstance(milestones, list):
        milestones = []
        state["milestones"] = milestones
    for item in milestones:
        if not isinstance(item, dict):
            continue
        if (
            item.get("manifest_digest_sha256") == key["manifest_digest_sha256"]
            and item.get("through_site_resolved") == key["through_site_resolved"]
        ):
            return
    milestones.append(
        {
            **key,
            "through_site": state["control"]["through_site"],
            "site_count": target["site_count"],
            "fault_count": target["fault_count"],
            "passed_sites": state["progress"]["target"]["passed_sites"],
            "passed_faults": state["progress"]["target"]["passed_faults"],
            "completed_at_utc": utc_now(),
        }
    )


def refresh_campaign_state(
    campaign_root: Path,
    manifest: Mapping[str, Any],
    *,
    through_site: Any = _KEEP,
    run_state: str | None = None,
    last_error: Any = _KEEP,
    append_milestone: bool = True,
) -> dict[str, Any]:
    path = state_path(campaign_root)
    if path.is_file():
        state = load_json(path, "campaign state")
    else:
        default_value = "all" if through_site is _KEEP else through_site
        state = default_state(
            campaign_root,
            manifest,
            parse_through_site(default_value, int(manifest["universe"]["site_count"])),
        )

    recorded_manifest = state.get("manifest")
    if not isinstance(recorded_manifest, dict) or recorded_manifest.get(
        "digest_sha256"
    ) != manifest.get("manifest_digest_sha256"):
        raise BatchError("campaign state/manifest digest mismatch")

    total_sites = int(manifest["universe"]["site_count"])
    total_faults = int(manifest["universe"]["fault_count"])
    control = state.setdefault("control", {})
    if through_site is not _KEEP:
        control["through_site"] = parse_through_site(through_site, total_sites)
    current_through = parse_through_site(control.get("through_site"), total_sites)
    control["through_site"] = current_through
    control["error_policy"] = "fail_fast"
    resolved_limit = resolved_through_site(current_through, total_sites)

    sites = manifest["sites"]
    fault_map = fault_record_map(manifest)
    all_site_rows = [site_progress(site, fault_map) for site in sites]
    target_rows = all_site_rows[:resolved_limit]
    outside_rows = all_site_rows[resolved_limit:]

    target_fault_count = sum(int(row["fault_count"]) for row in target_rows)
    target_fault_states = Counter(
        fault["state"]
        for row in target_rows
        for fault in row["faults"]
    )
    global_fault_states = Counter(
        fault["state"]
        for row in all_site_rows
        for fault in row["faults"]
    )
    target_site_states = Counter(str(row["site_state"]) for row in target_rows)
    global_site_states = Counter(str(row["site_state"]) for row in all_site_rows)

    passed_target_faults = target_fault_states[PASS_STATE]
    passed_target_sites = target_site_states["PASSED"]
    contiguous_passed = 0
    last_contiguous_selection = None
    for row in target_rows:
        if row["site_state"] != "PASSED":
            break
        contiguous_passed += 1
        last_contiguous_selection = row["selection_id"]

    state["universe"] = {
        "site_count": total_sites,
        "fault_count": total_faults,
    }
    state["target"] = {
        "through_site": current_through,
        "through_site_resolved": resolved_limit,
        "site_count": len(target_rows),
        "fault_count": target_fault_count,
    }
    state["progress"] = {
        "target": {
            "passed_sites": passed_target_sites,
            "site_count": len(target_rows),
            "passed_faults": passed_target_faults,
            "fault_count": target_fault_count,
            "contiguous_passed_sites": contiguous_passed,
            "last_contiguous_passed_selection_id": last_contiguous_selection,
            "site_state_counts": dict(target_site_states),
            "fault_state_counts": dict(target_fault_states),
        },
        "global": {
            "passed_sites": global_site_states["PASSED"],
            "site_count": total_sites,
            "passed_faults": global_fault_states[PASS_STATE],
            "fault_count": total_faults,
            "site_state_counts": dict(global_site_states),
            "fault_state_counts": dict(global_fault_states),
        },
        "outside_target": {
            "site_count": len(outside_rows),
            "fault_count": total_faults - target_fault_count,
        },
    }

    run = state.setdefault("run", {})
    if last_error is not _KEEP:
        run["last_error"] = last_error
    if run_state is not None:
        run["state"] = run_state
    elif passed_target_sites == len(target_rows) and passed_target_faults == target_fault_count:
        run["state"] = (
            "ALL_COMPLETE" if resolved_limit == total_sites else "BOUNDARY_COMPLETE"
        )
        run["last_error"] = None
    elif target_site_states["FAILED"] or target_site_states["BLOCKED"]:
        run["state"] = "REVIEW_REQUIRED"
    elif run.get("state") != "RUNNING":
        run["state"] = "READY"

    if "last_started_at_utc" not in run:
        run["last_started_at_utc"] = None
    if "last_finished_at_utc" not in run:
        run["last_finished_at_utc"] = None

    if append_milestone:
        append_completed_milestone(state)
    state["updated_at_utc"] = utc_now()
    write_json(path, state)
    return state


def print_state_summary(state: Mapping[str, Any]) -> None:
    universe = state["universe"]
    target = state["target"]
    progress = state["progress"]["target"]
    run = state["run"]
    print("=" * 70)
    print("Stage-5 campaign state")
    print("=" * 70)
    print(f"Run state          : {run.get('state')}")
    print(f"Through site       : {state['control']['through_site']}")
    print(f"Target sites       : {progress['passed_sites']} / {target['site_count']}")
    print(f"Target faults      : {progress['passed_faults']} / {target['fault_count']}")
    print(
        "Contiguous sites  : "
        f"{progress['contiguous_passed_sites']} / {target['site_count']}"
    )
    print(f"Universe sites     : {universe['site_count']}")
    print(f"Universe faults    : {universe['fault_count']}")
    print(f"Site states        : {progress['site_state_counts']}")
    print(f"Fault states       : {progress['fault_state_counts']}")
    if run.get("last_error"):
        print(f"Last error         : {run['last_error']}")


def set_through_site(args: argparse.Namespace) -> int:
    campaign_root = args.campaign_root.resolve()
    manifest, _ = load_runtime_manifest(campaign_root)
    ensure_campaign_state(
        campaign_root,
        manifest,
        default_through_site=args.value,
    )
    state = refresh_campaign_state(
        campaign_root,
        manifest,
        through_site=args.value,
    )
    print_state_summary(state)
    return 0


# ---------------------------------------------------------------------------
# Lazy per-fault preparation and established Stage-5 execution path
# ---------------------------------------------------------------------------


def ensure_fault_prepared(
    *,
    root: Path,
    record: Mapping[str, Any],
) -> tuple[Path, Path]:
    fault_id = str(record["fault_id"])
    fault_dir = Path(str(record["fault_root"])).resolve()
    local_fault = Path(str(record["fault_json"])).resolve()

    original_value = record.get("original_fault_json")
    if isinstance(original_value, str) and original_value:
        original_fault = Path(original_value).resolve()
    elif local_fault.is_file():
        original_fault = local_fault
    else:
        raise BatchError(f"no source fault JSON is available for {fault_id}")

    if not original_fault.is_file():
        raise BatchError(f"source fault JSON not found for {fault_id}: {original_fault}")
    expected_file_sha = record.get("original_fault_json_sha256")
    if isinstance(expected_file_sha, str) and expected_file_sha:
        actual = sha256_file(original_fault)
        if actual != expected_file_sha:
            raise BatchError(
                f"source fault JSON SHA changed for {fault_id}\n"
                f"  expected: {expected_file_sha}\n"
                f"  actual:   {actual}"
            )

    fault_dir.mkdir(parents=True, exist_ok=True)
    if local_fault.is_file():
        if sha256_file(local_fault) != sha256_file(original_fault):
            raise BatchError(f"local/source fault JSON mismatch for {fault_id}")
    else:
        local_fault.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original_fault, local_fault)

    spec = load_json(local_fault, f"local fault spec {fault_id}")
    checks = {
        "fault_id": fault_id,
        "selection_id": record.get("selection_id"),
        "site_id": record.get("site_id"),
        "site_key": record.get("site_key"),
        "polarity": record.get("polarity"),
        "stuck_at": record.get("stuck_at"),
    }
    for key, expected in checks.items():
        if expected is None:
            continue
        actual = spec.get(key)
        if actual != expected:
            raise BatchError(
                f"local fault spec identity mismatch for {fault_id}: "
                f"{key}={actual!r}, expected={expected!r}"
            )

    base_dir = fault_dir / "base"
    base_monitor = base_dir / "base_monitor.sv"
    base_manifest = base_dir / "base_manifest.json"
    if not (base_monitor.is_file() and base_manifest.is_file()):
        if base_dir.exists():
            archive_path(base_dir, "incomplete")
        base_dir.mkdir(parents=True, exist_ok=False)
        unused_trace = base_dir / "base_unused.trace.tsv"
        stage5_tool = root / "scripts/fault_characterization/stage5_faults.py"
        if not stage5_tool.is_file():
            raise BatchError(f"Stage5 fault tool not found: {stage5_tool}")
        result = command(
            [
                sys.executable,
                str(stage5_tool),
                "make-fault-monitor",
                "--fault-json",
                str(local_fault),
                "--trace-output",
                str(unused_trace),
                "--output",
                str(base_monitor),
                "--manifest",
                str(base_manifest),
            ]
        )
        if result != 0 or not base_monitor.is_file() or not base_manifest.is_file():
            raise BatchError(f"base monitor generation failed for {fault_id}")

    status_path = fault_dir / "status.json"
    if not status_path.is_file():
        update_status(
            status_path,
            state="PREPARED",
            fault_id=fault_id,
            selection_id=record.get("selection_id"),
            selection_rank=record.get("selection_rank"),
            site_id=record.get("site_id"),
            site_key=record.get("site_key"),
            site_order=record.get("site_order"),
            polarity=record.get("polarity"),
            stuck_at=record.get("stuck_at"),
            fault_order=record.get("fault_order"),
        )
    return fault_dir, local_fault


def preserve_incomplete_mode(mode_dir: Path) -> None:
    if not mode_dir.exists():
        return
    result = mode_dir / "run" / "result.json"
    if result.is_file():
        return
    archive_path(mode_dir, "incomplete")


def run_mode(
    *,
    root: Path,
    fault_dir: Path,
    fault_json: Path,
    mode_name: str,
    compose_mode: str,
    run_purpose: str,
    mm_ram_profile: str,
    maxcycles: int,
    reusable_statuses: set[str],
) -> tuple[str, Path]:
    mode_dir = fault_dir / mode_name
    run_dir = mode_dir / "run"
    result_path = run_dir / "result.json"
    if result_path.is_file():
        result = load_json(result_path, f"existing {mode_name} result")
        existing_status = str(result.get("status"))
        if existing_status in reusable_statuses:
            print(f"Reuse {mode_name} result: {existing_status}")
            return existing_status, run_dir
        archived = archive_path(mode_dir, "non_scientific")
        print(
            f"Archived non-scientific {mode_name} result "
            f"({existing_status}) -> {archived}"
        )

    preserve_incomplete_mode(mode_dir)
    mode_dir.mkdir(parents=True, exist_ok=True)

    base_monitor = fault_dir / "base" / "base_monitor.sv"
    base_manifest = fault_dir / "base" / "base_manifest.json"
    if not base_monitor.is_file() or not base_manifest.is_file():
        raise BatchError(f"base monitor inputs are missing: {fault_dir}")

    monitor = mode_dir / "monitor.sv"
    metadata = mode_dir / "mode_metadata.json"
    trace = mode_dir / "trace.tsv"
    mode_tool = root / "scripts/fault_characterization/stage5_phase2_modes.py"
    policy = root / "platform/cv32e40p/stage5_phase2_execution_policy_v1.json"
    wrapper = root / "scripts/run_xrun_stage5_fault.sh"

    compose_status = command(
        [
            sys.executable,
            str(mode_tool),
            "compose",
            "--policy",
            str(policy),
            "--base-monitor",
            str(base_monitor),
            "--base-manifest",
            str(base_manifest),
            "--mode",
            compose_mode,
            "--trace-output",
            str(trace),
            "--output-monitor",
            str(monitor),
            "--output-metadata",
            str(metadata),
        ]
    )
    if compose_status != 0:
        raise BatchError(f"mode composition failed: {mode_name}")

    env = os.environ.copy()
    env.update(
        {
            "F2A_ROOT": str(root),
            "STAGE5_PHASE": "run",
            "STAGE5_RUN_PURPOSE": run_purpose,
            "STAGE5_MM_RAM_PROFILE": mm_ram_profile,
            "STAGE5_TRACE_OUTPUT": str(trace),
            "MAXCYCLES": str(maxcycles),
            "VCD": "0",
            "KEEP_WORK": "0",
        }
    )
    wrapper_status = command(
        [str(wrapper), str(fault_json), str(monitor), str(run_dir)],
        env=env,
    )
    if not result_path.is_file():
        raise BatchError(
            f"{mode_name} produced no result.json; wrapper status={wrapper_status}"
        )
    result = load_json(result_path, f"{mode_name} result")
    status = str(result.get("status"))
    print(f"{mode_name} wrapper status: {wrapper_status}")
    print(f"{mode_name} result        : {status}")
    return status, run_dir


def collect_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from collect_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from collect_strings(item)


def match_registered_detector(
    registry: Mapping[str, Any], native_run: Path
) -> tuple[str, dict[str, Any] | None]:
    """Resolve routing only from the verdict-selected detector identity."""

    detectors = registry.get("detectors")
    if not isinstance(detectors, list):
        raise BatchError("assertion registry has no detectors array")

    result = load_json(native_run / "result.json", "Native result")
    raw = result.get("raw_facts")
    if not isinstance(raw, dict):
        return "BLOCKED_UNREGISTERED_DETECTOR", None
    resolution = raw.get("signature_resolution")
    if not isinstance(resolution, dict):
        return "BLOCKED_UNREGISTERED_DETECTOR", None
    selected = resolution.get("selected_terminal")
    if not isinstance(selected, dict):
        return "BLOCKED_UNREGISTERED_DETECTOR", None
    if selected.get("kind") != "REGISTERED_DETECTOR_TERMINATION":
        return "BLOCKED_UNREGISTERED_DETECTOR", None
    evidence = selected.get("evidence")
    if not isinstance(evidence, dict):
        return "BLOCKED_UNREGISTERED_DETECTOR", None
    detector_id = evidence.get("detector_id")
    if not isinstance(detector_id, str) or not detector_id:
        return "BLOCKED_UNREGISTERED_DETECTOR", None

    matches = [
        dict(item)
        for item in detectors
        if isinstance(item, dict) and item.get("detector_id") == detector_id
    ]
    if not matches:
        return "BLOCKED_UNREGISTERED_DETECTOR", None
    if len(matches) != 1:
        return "BLOCKED_AMBIGUOUS_DETECTOR", None

    detector = matches[0]
    supported = (
        detector.get("diagnostic_adapter") == "MM_RAM_STAGE5_OVERLAY_V2"
        and detector.get("diagnostic_modes_supported")
        == ["observe", "diagnostic_quarantine"]
        and detector.get("quarantine_action")
        in {
            "ACKNOWLEDGE_AND_DROP_WRITE",
            "RETURN_ZERO_AND_CONTINUE",
        }
    )
    if not supported:
        return "BLOCKED_UNSUPPORTED_REGISTERED_DETECTOR", detector
    return "DIAGNOSTIC_THREE_MODE", detector


def create_routing(
    *,
    fault_id: str,
    native_status: str,
    registry: Mapping[str, Any],
    native_run: Path,
) -> dict[str, Any]:
    if native_status in {"OUTPUT_MATCH", "OUTPUT_MISMATCH", "TIMEOUT"}:
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at_utc": utc_now(),
            "fault_id": fault_id,
            "native_status": native_status,
            "route": "NATIVE_ONLY",
            "detector_id": None,
            "detector_leaf_name": None,
            "diagnostic_modes_required": False,
            "reason": "Native execution directly defines the natural outcome.",
        }
    route, detector = match_registered_detector(registry, native_run)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": utc_now(),
        "fault_id": fault_id,
        "native_status": native_status,
        "route": route,
        "detector_id": detector.get("detector_id") if detector else None,
        "detector_leaf_name": (
            detector.get("assertion_leaf_name") if detector else None
        ),
        "diagnostic_modes_required": route == "DIAGNOSTIC_THREE_MODE",
        "reason": (
            "Registered detector supports OBSERVE and DIAGNOSTIC_QUARANTINE."
            if route == "DIAGNOSTIC_THREE_MODE"
            else "Automatic diagnostic continuation is fail-closed."
        ),
    }


def cleanup_validated_work(fault_dir: Path) -> dict[str, Any]:
    candidates = [
        fault_dir / "native" / "run" / "work",
        fault_dir / "observe" / "run" / "work",
        fault_dir / "diagnostic_quarantine" / "run" / "work",
    ]
    removed: list[dict[str, Any]] = []
    total = 0
    for path in candidates:
        if not path.exists():
            continue
        size = directory_bytes(path)
        shutil.rmtree(path)
        removed.append({"path": str(path), "bytes": size})
        total += size
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": utc_now(),
        "cleanup_condition": "ORACLE_INDEPENDENT_VALIDATION_PASS",
        "removed": removed,
        "bytes_freed": total,
        "human_freed": human_bytes(total),
        "vcd_retained": False,
    }
    write_json(fault_dir / "cleanup.json", report)
    return report


def build_and_validate_oracle(
    *,
    root: Path,
    fault_dir: Path,
    fault_json: Path,
    registry_path: Path,
    routing_path: Path,
    native_run: Path,
    observe_run: Path | None,
    quarantine_run: Path | None,
) -> Path:
    oracle_dir = fault_dir / "oracle"
    validation = oracle_dir / "validation.json"
    if validation.is_file():
        report = load_json(validation, "existing oracle validation")
        if report.get("status") == "PASS":
            return validation
        archive_path(oracle_dir, "invalid")
    elif oracle_dir.exists():
        archive_path(oracle_dir, "incomplete")
    oracle_dir.mkdir(parents=True, exist_ok=True)

    oracle = oracle_dir / "oracle.json"
    prompt = oracle_dir / "prompt_context.json"
    validation = oracle_dir / "validation.json"
    builder = root / "scripts/fault_characterization/stage5_batch_oracle.py"
    validator = root / "scripts/fault_characterization/stage5_batch_oracle_validate.py"
    builder_args = [
        sys.executable,
        str(builder),
        "--fault-json",
        str(fault_json),
        "--registry",
        str(registry_path),
        "--routing",
        str(routing_path),
        "--native-run",
        str(native_run),
        "--oracle",
        str(oracle),
        "--prompt-context",
        str(prompt),
    ]
    validator_args = [
        sys.executable,
        str(validator),
        "--oracle",
        str(oracle),
        "--prompt-context",
        str(prompt),
        "--fault-json",
        str(fault_json),
        "--registry",
        str(registry_path),
        "--routing",
        str(routing_path),
        "--native-run",
        str(native_run),
        "--report",
        str(validation),
    ]
    if observe_run is not None:
        builder_args.extend(["--observe-run", str(observe_run)])
        validator_args.extend(["--observe-run", str(observe_run)])
    if quarantine_run is not None:
        builder_args.extend(["--quarantine-run", str(quarantine_run)])
        validator_args.extend(["--quarantine-run", str(quarantine_run)])

    if command(builder_args) != 0:
        raise BatchError("generic oracle construction failed")
    if command(validator_args) != 0:
        raise BatchError("independent oracle validation failed")
    report = load_json(validation, "oracle validation")
    if report.get("status") != "PASS":
        raise BatchError("oracle validation report is not PASS")
    return validation


def run_one_fault(
    *,
    root: Path,
    campaign_root: Path,
    record: Mapping[str, Any],
    maxcycles: int,
) -> str:
    fault_id = str(record["fault_id"])
    fault_dir, fault_json = ensure_fault_prepared(root=root, record=record)
    status_path = fault_dir / "status.json"
    status = current_status(status_path)
    if status.get("state") == PASS_STATE:
        print(f"Skip completed fault: {fault_id}")
        return PASS_STATE

    print()
    print("=" * 70)
    print(f"Stage-5 campaign fault: {fault_id}")
    print("=" * 70)
    update_status(
        status_path,
        remove_keys=(
            "failure_reason",
            "failure_type",
            "work_retained",
        ),
        state="RUNNING_NATIVE",
        fault_id=fault_id,
    )

    native_status, native_run = run_mode(
        root=root,
        fault_dir=fault_dir,
        fault_json=fault_json,
        mode_name="native",
        compose_mode="NATIVE",
        run_purpose="NATIVE_CHARACTERIZATION",
        mm_ram_profile="native",
        maxcycles=maxcycles,
        reusable_statuses=NATIVE_SCIENTIFIC,
    )
    if native_status not in NATIVE_SCIENTIFIC:
        raise BatchError(f"non-scientific Native status: {native_status}")

    registry_path = root / "platform/cv32e40p/stage5_assertion_policy_v1.json"
    registry = load_json(registry_path, "assertion registry")
    routing = create_routing(
        fault_id=fault_id,
        native_status=native_status,
        registry=registry,
        native_run=native_run,
    )
    routing_path = fault_dir / "routing.json"
    write_json(routing_path, routing)
    update_status(
        status_path,
        state="ROUTED",
        native_status=native_status,
        route=routing["route"],
        detector_id=routing.get("detector_id"),
    )

    if str(routing["route"]).startswith("BLOCKED_"):
        update_status(
            status_path,
            state=routing["route"],
            work_retained=True,
            failure_reason=routing["reason"],
        )
        print(f"Fault blocked fail-closed: {fault_id} -> {routing['route']}")
        return str(routing["route"])

    observe_run: Path | None = None
    quarantine_run: Path | None = None
    observe_status: str | None = None
    quarantine_status: str | None = None

    if routing["route"] == "DIAGNOSTIC_THREE_MODE":
        update_status(status_path, state="RUNNING_DIAGNOSTIC")
        observe_status, observe_run = run_mode(
            root=root,
            fault_dir=fault_dir,
            fault_json=fault_json,
            mode_name="observe",
            compose_mode="OBSERVE",
            run_purpose="DIAGNOSTIC_OBSERVE",
            mm_ram_profile="diagnostic",
            maxcycles=maxcycles,
            reusable_statuses=DIAGNOSTIC_SCIENTIFIC,
        )
        if observe_status not in DIAGNOSTIC_SCIENTIFIC:
            raise BatchError(f"non-scientific OBSERVE status: {observe_status}")

        quarantine_status, quarantine_run = run_mode(
            root=root,
            fault_dir=fault_dir,
            fault_json=fault_json,
            mode_name="diagnostic_quarantine",
            compose_mode="QUARANTINE",
            run_purpose="DIAGNOSTIC_QUARANTINE",
            mm_ram_profile="diagnostic",
            maxcycles=maxcycles,
            reusable_statuses=DIAGNOSTIC_SCIENTIFIC,
        )
        if quarantine_status not in DIAGNOSTIC_SCIENTIFIC:
            raise BatchError(
                "non-scientific DIAGNOSTIC_QUARANTINE status: "
                f"{quarantine_status}"
            )

    update_status(
        status_path,
        state="BUILDING_ORACLE",
        observe_status=observe_status,
        diagnostic_quarantine_status=quarantine_status,
    )
    validation = build_and_validate_oracle(
        root=root,
        fault_dir=fault_dir,
        fault_json=fault_json,
        registry_path=registry_path,
        routing_path=routing_path,
        native_run=native_run,
        observe_run=observe_run,
        quarantine_run=quarantine_run,
    )
    cleanup = cleanup_validated_work(fault_dir)
    validation_report = load_json(validation, "oracle validation")
    update_status(
        status_path,
        remove_keys=("failure_reason", "failure_type"),
        state=PASS_STATE,
        validated_capability=validation_report.get("validated_capability"),
        oracle_validation=str(validation),
        cleanup_report=str(fault_dir / "cleanup.json"),
        bytes_freed=cleanup["bytes_freed"],
        work_retained=False,
    )

    print("Fault completed and cleaned: " + fault_id)
    print(f"Native status               : {native_status}")
    print(f"OBSERVE status              : {observe_status or 'NOT_RUN'}")
    print(
        "DIAGNOSTIC_QUARANTINE status: "
        f"{quarantine_status or 'NOT_RUN'}"
    )
    print(
        "Validated capability        : "
        f"{validation_report.get('validated_capability')}"
    )
    print(f"Temporary work removed      : {cleanup['human_freed']}")
    return PASS_STATE


# ---------------------------------------------------------------------------
# Campaign execution, fail-fast error retention, status, and storage
# ---------------------------------------------------------------------------


def target_sites_from_state(
    manifest: Mapping[str, Any],
    state: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    limit = int(state["target"]["through_site_resolved"])
    return list(manifest["sites"][:limit])


def run_campaign(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    campaign_root = args.campaign_root.resolve()
    require_checkpoint(args.checkpoint.resolve())
    manifest, _ = load_runtime_manifest(campaign_root)
    state = ensure_campaign_state(campaign_root, manifest)
    state = refresh_campaign_state(
        campaign_root,
        manifest,
        run_state="RUNNING",
        last_error=None,
        append_milestone=False,
    )
    state["run"]["last_started_at_utc"] = utc_now()
    state["run"]["last_finished_at_utc"] = None
    write_json(state_path(campaign_root), state)

    fault_map = fault_record_map(manifest)
    target_sites = target_sites_from_state(manifest, state)
    attempted_faults = 0

    for site in target_sites:
        selection_id = str(site.get("selection_id"))
        print()
        print("#" * 70)
        print(
            f"Site {site['site_order']}/{state['target']['site_count']}: "
            f"{selection_id} faults={len(site['fault_ids'])}"
        )
        print("#" * 70)

        for fault_id in site["fault_ids"]:
            record = fault_map[str(fault_id)]
            fault_dir = Path(str(record["fault_root"])).resolve()
            status = current_status(fault_dir / "status.json")
            if status.get("state") == PASS_STATE:
                print(f"Skip completed fault: {fault_id}")
                continue
            try:
                result_state = run_one_fault(
                    root=root,
                    campaign_root=campaign_root,
                    record=record,
                    maxcycles=args.maxcycles,
                )
                attempted_faults += 1
                if result_state in BLOCKED_STATES:
                    error = {
                        "type": "BLOCKED_DETECTOR",
                        "message": f"{fault_id} -> {result_state}",
                        "fault_id": str(fault_id),
                        "selection_id": selection_id,
                        "site_order": int(site["site_order"]),
                        "recorded_at_utc": utc_now(),
                    }
                    state = refresh_campaign_state(
                        campaign_root,
                        manifest,
                        run_state="REVIEW_REQUIRED",
                        last_error=error,
                        append_milestone=False,
                    )
                    state["run"]["last_finished_at_utc"] = utc_now()
                    write_json(state_path(campaign_root), state)
                    print_state_summary(state)
                    return 2
            except Exception as exc:
                update_status(
                    fault_dir / "status.json",
                    state="FAILED",
                    work_retained=True,
                    failure_reason=str(exc),
                    failure_type=type(exc).__name__,
                )
                error = {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "fault_id": str(fault_id),
                    "selection_id": selection_id,
                    "site_order": int(site["site_order"]),
                    "recorded_at_utc": utc_now(),
                }
                state = refresh_campaign_state(
                    campaign_root,
                    manifest,
                    run_state="REVIEW_REQUIRED",
                    last_error=error,
                    append_milestone=False,
                )
                state["run"]["last_finished_at_utc"] = utc_now()
                write_json(state_path(campaign_root), state)
                print(
                    f"ERROR: campaign stopped at {fault_id}: {exc}",
                    file=sys.stderr,
                )
                print_state_summary(state)
                return 2

            state = refresh_campaign_state(
                campaign_root,
                manifest,
                run_state="RUNNING",
                last_error=None,
                append_milestone=False,
            )

        # A site passes only when every activity-derived fault instance passes.
        site_row = site_progress(site, fault_map)
        if site_row["site_state"] != "PASSED":
            error = {
                "type": "SITE_NOT_PASSED",
                "message": (
                    f"site {selection_id} ended with state "
                    f"{site_row['site_state']}"
                ),
                "selection_id": selection_id,
                "site_order": int(site["site_order"]),
                "recorded_at_utc": utc_now(),
            }
            state = refresh_campaign_state(
                campaign_root,
                manifest,
                run_state="REVIEW_REQUIRED",
                last_error=error,
                append_milestone=False,
            )
            state["run"]["last_finished_at_utc"] = utc_now()
            write_json(state_path(campaign_root), state)
            print_state_summary(state)
            return 2

        state = refresh_campaign_state(
            campaign_root,
            manifest,
            run_state="RUNNING",
            last_error=None,
            append_milestone=False,
        )
        print(
            f"Site completed: {selection_id} "
            f"({len(site['fault_ids'])} fault instance(s))"
        )
        print(
            "Campaign progress: "
            f"{state['progress']['target']['passed_sites']} / "
            f"{state['target']['site_count']} sites; "
            f"{state['progress']['target']['passed_faults']} / "
            f"{state['target']['fault_count']} faults"
        )

    state = refresh_campaign_state(
        campaign_root,
        manifest,
        last_error=None,
        append_milestone=True,
    )
    state["run"]["last_finished_at_utc"] = utc_now()
    write_json(state_path(campaign_root), state)
    print()
    print_state_summary(state)
    print(f"Faults newly attempted: {attempted_faults}")
    return 0 if state["run"]["state"] in {"BOUNDARY_COMPLETE", "ALL_COMPLETE"} else 2


def run_one_command(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    campaign_root = args.campaign_root.resolve()
    require_checkpoint(args.checkpoint.resolve())
    manifest, _ = load_runtime_manifest(campaign_root)
    ensure_campaign_state(campaign_root, manifest)
    matches = [
        item
        for item in manifest.get("faults", [])
        if isinstance(item, dict) and item.get("fault_id") == args.fault_id
    ]
    if len(matches) != 1:
        raise BatchError(f"fault is not uniquely selected in campaign: {args.fault_id}")
    record = matches[0]
    fault_dir = Path(str(record["fault_root"])).resolve()
    try:
        result_state = run_one_fault(
            root=root,
            campaign_root=campaign_root,
            record=record,
            maxcycles=args.maxcycles,
        )
        state = refresh_campaign_state(campaign_root, manifest)
        print_state_summary(state)
        return 0 if result_state == PASS_STATE else 2
    except Exception as exc:
        update_status(
            fault_dir / "status.json",
            state="FAILED",
            work_retained=True,
            failure_reason=str(exc),
            failure_type=type(exc).__name__,
        )
        refresh_campaign_state(
            campaign_root,
            manifest,
            run_state="REVIEW_REQUIRED",
            last_error={
                "type": type(exc).__name__,
                "message": str(exc),
                "fault_id": args.fault_id,
                "recorded_at_utc": utc_now(),
            },
            append_milestone=False,
        )
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


def status_command(args: argparse.Namespace) -> int:
    campaign_root = args.campaign_root.resolve()
    manifest, _ = load_runtime_manifest(campaign_root)
    state = ensure_campaign_state(campaign_root, manifest)
    state = refresh_campaign_state(campaign_root, manifest)
    print_state_summary(state)
    return 0


def storage_report_command(args: argparse.Namespace) -> int:
    campaign_root = args.campaign_root.resolve()
    manifest, _ = load_runtime_manifest(campaign_root)
    state = ensure_campaign_state(campaign_root, manifest)
    total = directory_bytes(campaign_root)
    work_paths = [
        path
        for path in campaign_root.glob("sites/**/run/work")
        if path.is_dir()
    ]
    work_bytes = sum(directory_bytes(path) for path in work_paths)
    disk = shutil.disk_usage(campaign_root)
    state["storage"] = {
        "generated_at_utc": utc_now(),
        "total_bytes": total,
        "work_bytes": work_bytes,
        "durable_estimate_bytes": max(0, total - work_bytes),
        "filesystem_free_bytes": disk.free,
        "human_total": human_bytes(total),
        "human_work": human_bytes(work_bytes),
        "human_durable_estimate": human_bytes(max(0, total - work_bytes)),
        "human_filesystem_free": human_bytes(disk.free),
    }
    state["updated_at_utc"] = utc_now()
    write_json(state_path(campaign_root), state)
    print_state_summary(state)
    print(f"Campaign storage   : {human_bytes(total)}")
    print(f"Retained work      : {human_bytes(work_bytes)}")
    print(f"Durable estimate   : {human_bytes(max(0, total - work_bytes))}")
    print(f"Filesystem free    : {human_bytes(disk.free)}")
    return 0


def validate_campaign_command(args: argparse.Namespace) -> int:
    campaign_root = args.campaign_root.resolve()
    manifest, path = load_runtime_manifest(campaign_root)
    state = ensure_campaign_state(campaign_root, manifest)
    state = refresh_campaign_state(campaign_root, manifest)
    print("Campaign validation: PASS")
    print(f"Manifest          : {path}")
    print(f"Manifest digest   : {manifest['manifest_digest_sha256']}")
    print(f"Universe sites    : {manifest['universe']['site_count']}")
    print(f"Universe faults   : {manifest['universe']['fault_count']}")
    print(f"Through site      : {state['control']['through_site']}")
    print(f"Target faults     : {state['target']['fault_count']}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    root_default = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root_default)
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=(
            root_default
            / "runs/stage5_dev/phase2_v1/g5_oracle/reports/"
            "TF000002_SA0_validation.json"
        ),
    )
    parser.add_argument(
        "--campaign-root",
        "--pilot-root",
        dest="campaign_root",
        type=Path,
        default=(
            root_default
            / "runs/stage5_campaign_v3/cv32e40p/crc32/sites_all"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser(
        "prepare",
        help="prepare one immutable full campaign manifest",
    )
    prepare.add_argument(
        "--source-campaign",
        type=Path,
        default=(
            root_default
            / "faults/cv32e40p/stage5/stage_05_campaign.json"
        ),
    )
    prepare.add_argument("--seed", type=int, default=20260801)
    prepare.add_argument(
        "--through-site",
        default="100",
        help="initial mutable execution target: integer site order or all",
    )
    prepare.set_defaults(func=prepare_campaign)

    set_target = sub.add_parser(
        "set-through-site",
        help="change only campaign_state.json; never modify the manifest",
    )
    set_target.add_argument("--value", required=True)
    set_target.set_defaults(func=set_through_site)

    run_all = sub.add_parser(
        "run-campaign",
        aliases=["run-pilot"],
        help="run or resume through campaign_state.control.through_site",
    )
    run_all.add_argument("--maxcycles", type=int, default=2_000_000)
    run_all.set_defaults(func=run_campaign)

    run_one = sub.add_parser("run-one", help="run or resume one fault instance")
    run_one.add_argument("--fault-id", required=True)
    run_one.add_argument("--maxcycles", type=int, default=2_000_000)
    run_one.set_defaults(func=run_one_command)

    status = sub.add_parser(
        "status",
        help="rebuild campaign_state progress from per-fault status files",
    )
    status.set_defaults(func=status_command)

    storage = sub.add_parser(
        "storage-report",
        help="store one current storage summary inside campaign_state.json",
    )
    storage.set_defaults(func=storage_report_command)

    validate = sub.add_parser(
        "validate-campaign",
        help="validate immutable manifest and mutable state consistency",
    )
    validate.set_defaults(func=validate_campaign_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
