#!/usr/bin/env python3
"""Freeze, rebuild, plan, and qualify the canonical Stage-5 batch workflow.

This control tool deliberately does not run Xcelium.  Runtime execution remains
inside the existing stage5_batch.py orchestrator.  The control tool provides the
small amount of experiment control that the current repository is missing:

* freeze the deterministic Stage-4 inputs and recover the canonical G5 reference;
* rebuild one consistent Stage-5 v1.0.8 campaign from the frozen Stage-4 files;
* build nested 4-site / 20-site plans that keep canonical IDs and include the
  complete reference site;
* independently qualify the canonical reference result produced by the generic
  batch path.

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

PROGRAM_VERSION = "1.0.1"
SCHEMA_VERSION = "1.0"
EXPECTED_CLASSES = (
    "sequential_state",
    "control_path",
    "architectural_data",
    "generic_observable",
)
FAULT_ID_RE = re.compile(r"^(?P<base>TF\d{6})_SA(?P<sa>[01])$")


class ControlError(RuntimeError):
    """Controlled, user-actionable batch-control failure."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ControlError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ControlError(f"invalid {label} JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ControlError(f"{label} must contain one JSON object: {path}")
    return value


def write_json(path: Path, value: Mapping[str, Any], *, overwrite: bool = False) -> None:
    path = path.expanduser().resolve()
    if path.exists() and not overwrite:
        raise ControlError(f"refusing to overwrite existing file: {path}")
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


def file_record(path: Path, root: Path) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        display = str(resolved.relative_to(root.resolve()))
    except ValueError:
        display = str(resolved)
    return {
        "path": str(resolved),
        "repository_relative_path": display,
        "sha256": sha256_file(resolved),
        "bytes": resolved.stat().st_size,
    }


def require_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.stat().st_size == 0:
        raise ControlError(f"{label} not found or empty: {resolved}")
    return resolved


def require_directory(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise ControlError(f"{label} not found: {resolved}")
    return resolved


def run_command(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    print("+ " + " ".join(str(item) for item in argv), flush=True)
    result = subprocess.run(
        [str(item) for item in argv],
        cwd=str(cwd) if cwd is not None else None,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        check=False,
    )
    if capture and result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    return result


def git_head(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def candidates_default(root: Path) -> Path:
    return root / "faults/cv32e40p/site_catalog/stage_04_candidates.json"


def selection_default(root: Path) -> Path:
    return root / "faults/cv32e40p/site_catalog/stage_04_selection.json"


def freeze_default(root: Path) -> Path:
    return root / "faults/cv32e40p/site_catalog/frozen_stage4_batch_v1"


def campaign_default(root: Path) -> Path:
    return root / "faults/cv32e40p/stage5"


def checkpoint_manifest_default(root: Path) -> Path:
    return root / "docs/stage5/phase2_g5_checkpoint_manifest.json"


def checkpoint_report_default(root: Path) -> Path:
    return (
        root
        / "runs/stage5_dev/phase2_v1/g5_oracle/reports/"
        "TF000002_SA0_validation.json"
    )


def mini_selection_default(root: Path) -> Path:
    return (
        root
        / "runs/stage5_dev/mini_smoke_v1/provenance/"
        "smoke_fault_selection.json"
    )


def stage5_tool_default(root: Path) -> Path:
    return root / "scripts/fault_characterization/stage5_faults.py"


def site_catalog_tool_default(root: Path) -> Path:
    return root / "scripts/fault_sites/site_catalog.py"


def site_policy_default(root: Path) -> Path:
    return root / "platform/cv32e40p/fault_site_policy.json"


def find_unique(
    records: Iterable[Mapping[str, Any]],
    *,
    key: str,
    value: str,
    label: str,
) -> dict[str, Any]:
    matches = [dict(item) for item in records if str(item.get(key, "")) == value]
    if len(matches) != 1:
        raise ControlError(
            f"expected exactly one {label} with {key}={value!r}; found {len(matches)}"
        )
    return matches[0]


def require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ControlError(f"{label} must be an array")
    return value


def resolve_file_reference(value: Any, base_dir: Path, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ControlError(f"{label} path is missing")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return require_file(path, label)


def optional_origin_from_mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    origin = value.get("mini_smoke_origin")
    return dict(origin) if isinstance(origin, dict) else None


def recover_smoke_record(
    *,
    smoke_path: Path,
    smoke_payload: Mapping[str, Any],
    checkpoint_fault_id: str,
) -> tuple[dict[str, Any], Path | None, dict[str, Any] | None, Path | None]:
    """Resolve the historical smoke fault without assuming one JSON schema.

    The active Gate-4 provenance file is a single record containing ``fault_id``
    and ``fault_spec``.  Older development snapshots may instead contain the
    complete mini Stage-4 selection.  Both layouts are accepted, but the
    physical site is always cross-checked against the frozen checkpoint and the
    canonical Stage-4 selection.
    """

    top_fault_id = smoke_payload.get("fault_id")
    if isinstance(top_fault_id, str) and top_fault_id:
        if top_fault_id != checkpoint_fault_id:
            raise ControlError(
                "smoke provenance/checkpoint fault mismatch\n"
                f"  checkpoint: {checkpoint_fault_id}\n"
                f"  smoke     : {top_fault_id}"
            )
        spec_path = resolve_file_reference(
            smoke_payload.get("fault_spec"),
            smoke_path.parent,
            "smoke fault spec",
        )
        spec = load_json(spec_path, "smoke fault spec")
        if str(spec.get("fault_id", "")) != checkpoint_fault_id:
            raise ControlError(
                "smoke fault-spec/checkpoint ID mismatch\n"
                f"  checkpoint: {checkpoint_fault_id}\n"
                f"  fault spec: {spec.get('fault_id')!r}"
            )

        origin = optional_origin_from_mapping(smoke_payload)
        if origin is None:
            origin = optional_origin_from_mapping(spec)

        mini_selection_path: Path | None = None
        source_stage4 = spec.get("source_stage4")
        if isinstance(source_stage4, dict):
            candidate = source_stage4.get("selection_path")
            if not isinstance(candidate, str) or not candidate:
                candidate = source_stage4.get("selection")
            if isinstance(candidate, str) and candidate:
                path = Path(candidate).expanduser()
                if not path.is_absolute():
                    path = (spec_path.parent / path).resolve()
                if path.is_file():
                    mini_selection_path = path.resolve()
                    mini_selection = load_json(
                        mini_selection_path, "smoke source Stage-4 selection"
                    )
                    mini_instances = mini_selection.get("fault_instances")
                    if isinstance(mini_instances, list):
                        mini_matches = [
                            dict(item)
                            for item in mini_instances
                            if isinstance(item, dict)
                            and str(item.get("fault_id", "")) == checkpoint_fault_id
                        ]
                        if len(mini_matches) != 1:
                            raise ControlError(
                                "smoke source selection does not contain exactly one "
                                f"{checkpoint_fault_id}; found {len(mini_matches)}"
                            )
                        selection_origin = optional_origin_from_mapping(
                            mini_matches[0]
                        )
                        if origin is None:
                            origin = selection_origin
                        elif (
                            selection_origin is not None
                            and selection_origin.get("fault_id")
                            and origin.get("fault_id")
                            and selection_origin.get("fault_id")
                            != origin.get("fault_id")
                        ):
                            raise ControlError(
                                "conflicting mini_smoke_origin fault IDs between "
                                "smoke provenance and source selection"
                            )

        return spec, spec_path, origin, mini_selection_path

    mini_instances = smoke_payload.get("fault_instances")
    if isinstance(mini_instances, list):
        mini_record = find_unique(
            (
                item
                for item in mini_instances
                if isinstance(item, Mapping)
            ),
            key="fault_id",
            value=checkpoint_fault_id,
            label="mini smoke fault",
        )
        origin = optional_origin_from_mapping(mini_record)
        return mini_record, None, origin, smoke_path.resolve()

    raise ControlError(
        "unsupported smoke provenance schema: expected either a single "
        "fault_id/fault_spec record or a full selection with fault_instances"
    )


def stage4_fault_class(instance: Mapping[str, Any], candidate: Mapping[str, Any]) -> str:
    direct = instance.get("fault_class")
    if isinstance(direct, str) and direct:
        return direct
    classification = candidate.get("classification")
    if isinstance(classification, dict):
        value = classification.get("primary_class")
        if isinstance(value, str) and value:
            return value
    return "UNKNOWN"


def load_frozen_reference(freeze_root: Path) -> dict[str, Any]:
    return load_json(freeze_root / "reference.json", "frozen canonical reference")


def command_freeze_reference(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    candidates_path = require_file(args.candidates, "Stage-4 candidates")
    selection_path = require_file(args.selection, "Stage-4 selection")
    smoke_path = require_file(args.mini_selection, "smoke fault provenance")
    checkpoint_manifest_path = require_file(
        args.checkpoint_manifest, "G5 checkpoint manifest"
    )
    checkpoint_report_path = require_file(
        args.checkpoint_report, "G5 validation report"
    )
    freeze_root = args.freeze_root.resolve()

    if freeze_root.exists():
        raise ControlError(
            f"freeze root already exists; inspect it instead of overwriting: {freeze_root}"
        )

    candidates = load_json(candidates_path, "Stage-4 candidates")
    selection = load_json(selection_path, "Stage-4 selection")
    smoke_payload = load_json(smoke_path, "smoke fault provenance")
    checkpoint = load_json(checkpoint_manifest_path, "G5 checkpoint manifest")
    validation = load_json(checkpoint_report_path, "G5 validation report")

    if candidates.get("stage") != "stage_04_fault_type_classification":
        raise ControlError("Stage-4 candidates stage marker mismatch")
    if selection.get("stage") != "stage_04_targeted_fault_selection_plan":
        raise ControlError("Stage-4 selection stage marker mismatch")
    if validation.get("status") != "PASS":
        raise ControlError("G5 validation report is not PASS")

    smoke_fault_id = str(checkpoint.get("fault_id", ""))
    if FAULT_ID_RE.fullmatch(smoke_fault_id) is None:
        raise ControlError(f"invalid checkpoint smoke fault ID: {smoke_fault_id!r}")
    validation_fault_id = validation.get("fault_id")
    if isinstance(validation_fault_id, str) and validation_fault_id:
        if validation_fault_id != smoke_fault_id:
            raise ControlError(
                "checkpoint manifest/validation fault mismatch\n"
                f"  manifest  : {smoke_fault_id}\n"
                f"  validation: {validation_fault_id}"
            )

    smoke_record, smoke_spec_path, mini_origin, mini_source_selection_path = (
        recover_smoke_record(
            smoke_path=smoke_path,
            smoke_payload=smoke_payload,
            checkpoint_fault_id=smoke_fault_id,
        )
    )

    frozen_results = checkpoint.get("frozen_results")
    if not isinstance(frozen_results, dict):
        raise ControlError("checkpoint manifest has no frozen_results object")
    expected_module = str(frozen_results.get("exact_injection_module", ""))
    expected_signal = str(frozen_results.get("exact_injection_signal", ""))
    if not expected_module or not expected_signal:
        raise ControlError("checkpoint exact injection identity is incomplete")

    smoke_site = smoke_record.get("site")
    if isinstance(smoke_site, dict):
        smoke_module = str(smoke_site.get("module", ""))
        smoke_signal = str(smoke_site.get("source_net", ""))
        if smoke_module and smoke_module != expected_module:
            raise ControlError(
                "smoke fault-spec module does not match the G5 checkpoint\n"
                f"  checkpoint: {expected_module}\n"
                f"  smoke     : {smoke_module}"
            )
        if smoke_signal and smoke_signal != expected_signal:
            raise ControlError(
                "smoke fault-spec source signal does not match the G5 checkpoint\n"
                f"  checkpoint: {expected_signal}\n"
                f"  smoke     : {smoke_signal}"
            )

    smoke_stuck_at = smoke_record.get("stuck_at")
    smoke_polarity = smoke_record.get("polarity")
    if smoke_stuck_at is not None and int(smoke_stuck_at) != 0:
        raise ControlError(
            f"smoke fault stuck-at changed; expected 0, got {smoke_stuck_at!r}"
        )
    if isinstance(smoke_polarity, str) and smoke_polarity and smoke_polarity != "SA0":
        raise ControlError(
            f"smoke fault polarity changed; expected SA0, got {smoke_polarity!r}"
        )

    stage4_instances = require_list(
        selection.get("fault_instances"), "Stage-4 fault_instances"
    )
    selected_sites = require_list(
        selection.get("selected_sites"), "Stage-4 selected_sites"
    )
    candidate_sites = require_list(candidates.get("sites"), "Stage-4 candidate sites")

    candidate_by_id = {
        str(item.get("site_id")): dict(item)
        for item in candidate_sites
        if isinstance(item, dict) and item.get("site_id")
    }

    # Recover the canonical selected site from the immutable physical identity.
    # Prefer the site_id carried by the mini fault spec; otherwise use the exact
    # checkpoint module/source pair.
    smoke_site_id = str(smoke_record.get("site_id", ""))
    selected_matches: list[dict[str, Any]] = []
    if smoke_site_id:
        selected_matches = [
            dict(item)
            for item in selected_sites
            if isinstance(item, dict)
            and str(item.get("site_id", "")) == smoke_site_id
        ]

    if not selected_matches:
        for item in selected_sites:
            if not isinstance(item, dict):
                continue
            candidate = candidate_by_id.get(str(item.get("site_id", "")))
            if candidate is None:
                continue
            if (
                str(candidate.get("module", "")) == expected_module
                and str(candidate.get("source_net", "")) == expected_signal
            ):
                selected_matches.append(dict(item))

    if len(selected_matches) != 1:
        raise ControlError(
            "could not resolve exactly one canonical Stage-4 selected site for "
            f"{expected_module}/{expected_signal}; found {len(selected_matches)}"
        )
    canonical_selected = selected_matches[0]
    canonical_selection_id = str(canonical_selected.get("selection_id", ""))
    canonical_site_id = str(canonical_selected.get("site_id", ""))
    canonical_candidate = candidate_by_id.get(canonical_site_id)
    if canonical_candidate is None:
        raise ControlError(
            f"canonical candidate site is missing from Stage-4: {canonical_site_id}"
        )

    actual_module = str(canonical_candidate.get("module", ""))
    actual_signal = str(canonical_candidate.get("source_net", ""))
    if actual_module != expected_module:
        raise ControlError(
            "canonical reference module does not match the G5 checkpoint\n"
            f"  checkpoint: {expected_module}\n"
            f"  canonical : {actual_module}"
        )
    if actual_signal != expected_signal:
        raise ControlError(
            "canonical reference source signal does not match the G5 checkpoint\n"
            f"  checkpoint: {expected_signal}\n"
            f"  canonical : {actual_signal}"
        )

    canonical_matches = [
        dict(item)
        for item in stage4_instances
        if isinstance(item, dict)
        and str(item.get("selection_id", "")) == canonical_selection_id
        and int(item.get("stuck_at", -1)) == 0
        and str(item.get("polarity", "")) == "SA0"
    ]
    if len(canonical_matches) != 1:
        raise ControlError(
            "expected exactly one canonical SA0 fault instance for "
            f"{canonical_selection_id}; found {len(canonical_matches)}"
        )
    canonical_instance = canonical_matches[0]
    canonical_fault_id = str(canonical_instance.get("fault_id", ""))
    if FAULT_ID_RE.fullmatch(canonical_fault_id) is None:
        raise ControlError(
            f"invalid canonical Stage-4 fault ID: {canonical_fault_id!r}"
        )

    # mini_smoke_origin remains a useful consistency check, but no longer a
    # fragile schema assumption.  Physical identity is the authoritative
    # recovery path.
    origin_fault_id = (
        str(mini_origin.get("fault_id", ""))
        if isinstance(mini_origin, dict)
        else ""
    )
    origin_selection_id = (
        str(mini_origin.get("selection_id", ""))
        if isinstance(mini_origin, dict)
        else ""
    )
    if origin_fault_id and origin_fault_id != canonical_fault_id:
        raise ControlError(
            "mini_smoke_origin disagrees with physical canonical recovery\n"
            f"  origin    : {origin_fault_id}\n"
            f"  canonical : {canonical_fault_id}"
        )
    if origin_selection_id and origin_selection_id != canonical_selection_id:
        raise ControlError(
            "mini_smoke_origin selection disagrees with physical canonical recovery\n"
            f"  origin    : {origin_selection_id}\n"
            f"  canonical : {canonical_selection_id}"
        )

    source_candidates = selection.get("source_candidates")
    if isinstance(source_candidates, dict):
        recorded_candidate_sha = source_candidates.get("sha256")
        if (
            recorded_candidate_sha
            and recorded_candidate_sha != sha256_file(candidates_path)
        ):
            raise ControlError(
                "Stage-4 selection candidate SHA does not match candidates"
            )

    freeze_root.mkdir(parents=True, exist_ok=False)

    reference = {
        "schema_version": SCHEMA_VERSION,
        "kind": "stage5_canonical_reference_identity",
        "generated_at_utc": utc_now(),
        "recovery_method": (
            "smoke fault physical identity mapped into deterministic canonical "
            "Stage-4 selection"
        ),
        "smoke_fault_id": smoke_fault_id,
        "smoke_fault_spec": (
            file_record(smoke_spec_path, root)
            if smoke_spec_path is not None
            else None
        ),
        "smoke_source_selection": (
            file_record(mini_source_selection_path, root)
            if mini_source_selection_path is not None
            else None
        ),
        "canonical_fault_id": canonical_fault_id,
        "canonical_base_fault_id": FAULT_ID_RE.fullmatch(
            canonical_fault_id
        ).group("base"),
        "canonical_selection_id": canonical_selection_id,
        "canonical_selection_rank": canonical_instance.get("selection_rank"),
        "canonical_site_id": canonical_site_id,
        "fault_class": stage4_fault_class(
            canonical_instance, canonical_candidate
        ),
        "module": actual_module,
        "source_net": actual_signal,
        "polarity": "SA0",
        "stuck_at": 0,
        "mini_smoke_origin": dict(mini_origin) if mini_origin else None,
        "mini_smoke_origin_cross_check": (
            "PASS" if mini_origin else "NOT_AVAILABLE_PHYSICAL_IDENTITY_USED"
        ),
        "checkpoint_expected": {
            "natural_architectural_outcome": frozen_results.get(
                "natural_architectural_outcome"
            ),
            "observe_runner_status": frozen_results.get(
                "observe_runner_status"
            ),
            "diagnostic_quarantine_runner_status": frozen_results.get(
                "diagnostic_quarantine_runner_status"
            ),
            "validated_capability": frozen_results.get(
                "validated_capability"
            ),
            "capability_scope": frozen_results.get("capability_scope"),
            "first_observable_detector_cycle": frozen_results.get(
                "first_observable_detector_cycle"
            ),
            "first_observable_detector_time": frozen_results.get(
                "first_observable_detector_time"
            ),
        },
    }
    write_json(freeze_root / "reference.json", reference)

    interface_paths = [
        root / "scripts/fault_characterization/stage5_batch.py",
        root / "scripts/fault_characterization/stage5_batch_oracle.py",
        root / "scripts/fault_characterization/stage5_batch_oracle_validate.py",
        root / "scripts/fault_characterization/stage5_faults.py",
        root / "scripts/fault_characterization/stage5_faults_v107_impl.py",
        root / "scripts/fault_characterization/stage5_phase2_modes.py",
        root / "scripts/fault_characterization/stage5_verdict.py",
        root / "scripts/run_xrun_stage5_fault.sh",
        root / "scripts/lib/xrun_stage5_common.sh",
        root / "platform/cv32e40p/stage5_phase2_execution_policy_v1.json",
        root / "platform/cv32e40p/stage5_assertion_policy_v1.json",
    ]
    missing_interface = [
        str(path) for path in interface_paths if not path.is_file()
    ]
    if missing_interface:
        raise ControlError(
            "required batch interface files are missing:\n  "
            + "\n  ".join(missing_interface)
        )

    summary = selection.get("selection_summary")
    if not isinstance(summary, dict):
        summary = {}
    freeze = {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "kind": "stage4_and_stage5_batch_interface_freeze",
        "generated_at_utc": utc_now(),
        "repository_root": str(root),
        "repository_head": git_head(root),
        "stage4": {
            "candidates": file_record(candidates_path, root),
            "selection": file_record(selection_path, root),
            "candidate_digest_sha256": candidates.get(
                "candidate_digest_sha256"
            ),
            "selection_digest_sha256": selection.get(
                "selection_digest_sha256"
            ),
            "selected_unique_site_count": summary.get(
                "selected_unique_site_count"
            ),
            "selected_fault_instance_count": summary.get(
                "selected_fault_instance_count"
            ),
            "selection_has_seed_parameter": False,
            "selection_order_contract": (
                "deterministic Stage-4 score/module/site-ID ordering"
            ),
        },
        "reference": reference,
        "checkpoint_manifest": file_record(
            checkpoint_manifest_path, root
        ),
        "checkpoint_validation": file_record(
            checkpoint_report_path, root
        ),
        "smoke_fault_provenance": file_record(smoke_path, root),
        "frozen_interface_files": [
            file_record(path, root) for path in interface_paths
        ],
        "contract": {
            "stage4_is_not_regenerated_for_batch_size": True,
            "canonical_fault_ids_are_preserved": True,
            "batch_size_means_unique_site_count": True,
            "every_selected_site_keeps_all_stage4_fault_instances": True,
            "reference_site_is_always_included": True,
            "no_xcelium_was_run_by_this_freeze": True,
        },
    }
    write_json(freeze_root / "interface_freeze.json", freeze)

    print()
    print(
        "======================================================================"
    )
    print("Canonical Stage-4 / Stage-5 interface freeze: PASS")
    print(
        "======================================================================"
    )
    print(f"Freeze root             : {freeze_root}")
    print(f"Smoke fault ID          : {smoke_fault_id}")
    print(f"Canonical fault ID      : {canonical_fault_id}")
    print(f"Canonical selection ID  : {canonical_selection_id}")
    print(f"Canonical site ID       : {canonical_site_id}")
    print(f"Fault class             : {reference['fault_class']}")
    print(f"Injection module        : {actual_module}")
    print(f"Injection signal        : {actual_signal}")
    print(
        "Origin cross-check      : "
        f"{reference['mini_smoke_origin_cross_check']}"
    )
    print("Stage-4 seed            : NOT APPLICABLE")
    print("Xcelium executed        : NO")
    return 0



def validate_campaign_v108(
    campaign_root: Path,
    reference: Mapping[str, Any],
    candidates_sha: str,
    selection_sha: str,
) -> dict[str, Any]:
    campaign_path = require_file(
        campaign_root / "stage_05_campaign.json", "Stage-5 campaign"
    )
    campaign = load_json(campaign_path, "Stage-5 campaign")
    if campaign.get("stage") != "stage_05_fault_characterization_campaign":
        raise ControlError("Stage-5 campaign marker mismatch")
    if str(campaign.get("program_version")) != "1.0.8":
        raise ControlError(
            f"Stage-5 campaign version is not 1.0.8: {campaign.get('program_version')!r}"
        )
    source = campaign.get("source_stage4")
    if not isinstance(source, dict):
        raise ControlError("Stage-5 campaign has no source_stage4 object")
    if source.get("candidates_sha256") != candidates_sha:
        raise ControlError("Stage-5 campaign candidates SHA differs from frozen Stage 4")
    if source.get("selection_sha256") != selection_sha:
        raise ControlError("Stage-5 campaign selection SHA differs from frozen Stage 4")

    faults = require_list(campaign.get("faults"), "Stage-5 campaign faults")
    if not faults:
        raise ControlError("Stage-5 campaign contains no faults")

    version_counts: Counter[str] = Counter()
    layout_counts: Counter[str] = Counter()
    canonical_reference_id = str(reference["canonical_fault_id"])
    reference_spec: dict[str, Any] | None = None
    reference_spec_path: Path | None = None

    for record in faults:
        if not isinstance(record, dict):
            raise ControlError("Stage-5 campaign contains a non-object fault record")
        fault_id = str(record.get("fault_id", ""))
        spec_path = Path(str(record.get("fault_spec", ""))).expanduser().resolve()
        spec = load_json(spec_path, f"fault spec {fault_id}")
        if str(spec.get("fault_id")) != fault_id:
            raise ControlError(f"fault-spec ID mismatch for {fault_id}")
        version = str(spec.get("program_version", ""))
        modification = spec.get("modification")
        layout = (
            str(modification.get("materialization_layout_version", ""))
            if isinstance(modification, dict)
            else ""
        )
        version_counts[version] += 1
        layout_counts[layout] += 1
        if version != "1.0.8":
            raise ControlError(f"fault spec is not v1.0.8: {fault_id} -> {version!r}")
        if layout != "declaration_before_first_use_v1":
            raise ControlError(
                f"fault spec has obsolete layout: {fault_id} -> {layout!r}"
            )
        if fault_id == canonical_reference_id:
            reference_spec = spec
            reference_spec_path = spec_path

    if reference_spec is None or reference_spec_path is None:
        raise ControlError(
            f"canonical reference is absent from rebuilt campaign: {canonical_reference_id}"
        )
    site = reference_spec.get("site")
    if not isinstance(site, dict):
        raise ControlError("canonical reference spec has no site object")
    if site.get("module") != reference.get("module"):
        raise ControlError("rebuilt reference module differs from frozen reference")
    if site.get("source_net") != reference.get("source_net"):
        raise ControlError("rebuilt reference source_net differs from frozen reference")
    if int(reference_spec.get("stuck_at", -1)) != int(reference.get("stuck_at", -2)):
        raise ControlError("rebuilt reference stuck-at differs from frozen reference")

    return {
        "campaign_path": str(campaign_path),
        "campaign_sha256": sha256_file(campaign_path),
        "campaign_digest_sha256": campaign.get("campaign_digest_sha256"),
        "site_count": campaign.get("campaign_summary", {}).get(
            "selected_unique_site_count"
        ),
        "fault_count": len(faults),
        "version_counts": dict(version_counts),
        "layout_counts": dict(layout_counts),
        "reference_spec_path": str(reference_spec_path),
        "reference_spec_sha256": sha256_file(reference_spec_path),
    }


def command_rebuild_campaign(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    freeze_root = require_directory(args.freeze_root, "freeze root")
    freeze = load_json(freeze_root / "interface_freeze.json", "interface freeze")
    reference = load_frozen_reference(freeze_root)

    candidates_path = require_file(args.candidates, "Stage-4 candidates")
    selection_path = require_file(args.selection, "Stage-4 selection")
    stage5_tool = require_file(args.stage5_tool, "active Stage-5 tool")
    site_catalog_tool = require_file(args.site_catalog_tool, "site catalog tool")
    policy = require_file(args.policy, "fault-site policy")
    campaign_root = args.campaign_root.resolve()
    archive_root = args.archive_root.resolve()

    frozen_stage4 = freeze.get("stage4")
    if not isinstance(frozen_stage4, dict):
        raise ControlError("interface freeze has no stage4 object")
    frozen_candidates = frozen_stage4.get("candidates")
    frozen_selection = frozen_stage4.get("selection")
    if not isinstance(frozen_candidates, dict) or not isinstance(frozen_selection, dict):
        raise ControlError("interface freeze Stage-4 file records are incomplete")
    candidates_sha = sha256_file(candidates_path)
    selection_sha = sha256_file(selection_path)
    if candidates_sha != frozen_candidates.get("sha256"):
        raise ControlError("current Stage-4 candidates differ from frozen candidates")
    if selection_sha != frozen_selection.get("sha256"):
        raise ControlError("current Stage-4 selection differs from frozen selection")

    version_result = run_command(
        [sys.executable, str(stage5_tool), "--version"], cwd=root, capture=True
    )
    if version_result.returncode != 0 or "1.0.8" not in (version_result.stdout or ""):
        raise ControlError("active Stage-5 tool is not v1.0.8")

    if campaign_root.is_dir():
        try:
            existing = validate_campaign_v108(
                campaign_root, reference, candidates_sha, selection_sha
            )
        except ControlError:
            existing = None
        if existing is not None:
            print("Existing canonical Stage-5 v1.0.8 campaign is already valid.")
            print(f"Campaign root: {campaign_root}")
            return 0

    if archive_root.exists():
        raise ControlError(
            f"fixed archive root already exists; inspect it before rebuilding: {archive_root}"
        )

    had_previous = campaign_root.exists()
    if had_previous:
        archive_root.parent.mkdir(parents=True, exist_ok=True)
        campaign_root.rename(archive_root)
        print(f"Archived previous Stage-5 campaign: {archive_root}")

    try:
        result = run_command(
            [
                sys.executable,
                str(stage5_tool),
                "prepare",
                "--candidates",
                str(candidates_path),
                "--selection",
                str(selection_path),
                "--site-catalog-tool",
                str(site_catalog_tool),
                "--policy",
                str(policy),
                "--output-root",
                str(campaign_root),
            ],
            cwd=root,
        )
        if result.returncode != 0:
            raise ControlError(
                f"Stage-5 v1.0.8 prepare failed with status {result.returncode}"
            )

        validation_result = run_command(
            [
                sys.executable,
                str(stage5_tool),
                "validate",
                "--campaign",
                str(campaign_root / "stage_05_campaign.json"),
            ],
            cwd=root,
        )
        if validation_result.returncode != 0:
            raise ControlError(
                f"Stage-5 campaign validation failed with status {validation_result.returncode}"
            )

        campaign_validation = validate_campaign_v108(
            campaign_root, reference, candidates_sha, selection_sha
        )

        report_root = root / "runs/stage5_reference_recovery_v108"
        report_root.mkdir(parents=True, exist_ok=True)
        reference_spec_path = Path(
            str(campaign_validation["reference_spec_path"])
        ).resolve()
        with tempfile.TemporaryDirectory(
            prefix="materialization_check_",
            dir=str(report_root),
        ) as temporary_directory:
            temporary_netlist = (
                Path(temporary_directory).resolve() / "fault_netlist.v"
            )
            apply_result = run_command(
                [
                    sys.executable,
                    str(stage5_tool),
                    "apply",
                    "--fault-json",
                    str(reference_spec_path),
                    "--output-netlist",
                    str(temporary_netlist),
                ],
                cwd=root,
            )
            if (
                apply_result.returncode != 0
                or not temporary_netlist.is_file()
            ):
                raise ControlError(
                    "canonical reference v1.0.8 materialization failed"
                )
            materialized_sha = sha256_file(temporary_netlist)
            materialized_bytes = temporary_netlist.stat().st_size

        report = {
            "schema_version": SCHEMA_VERSION,
            "program_version": PROGRAM_VERSION,
            "kind": "stage5_v108_canonical_campaign_rebuild_validation",
            "generated_at_utc": utc_now(),
            "status": "PASS",
            "campaign_root": str(campaign_root),
            "archived_previous_campaign": str(archive_root) if had_previous else None,
            "stage4_candidates_sha256": candidates_sha,
            "stage4_selection_sha256": selection_sha,
            "campaign_validation": campaign_validation,
            "reference": reference,
            "reference_materialization": {
                "status": "PASS",
                "temporary_netlist_retained": False,
                "temporary_netlist_sha256": materialized_sha,
                "temporary_netlist_bytes": materialized_bytes,
                "required_layout": "declaration_before_first_use_v1",
            },
        }
        write_json(
            report_root / "campaign_rebuild_validation.json",
            report,
            overwrite=(report_root / "campaign_rebuild_validation.json").exists(),
        )
    except Exception:
        if campaign_root.exists():
            shutil.rmtree(campaign_root)
        if had_previous and archive_root.exists():
            archive_root.rename(campaign_root)
            print("Rebuild failed; previous Stage-5 campaign was restored.", file=sys.stderr)
        raise

    print()
    print("======================================================================")
    print("Canonical Stage-5 v1.0.8 campaign rebuild: PASS")
    print("======================================================================")
    print(f"Campaign root            : {campaign_root}")
    print(f"Selected sites           : {campaign_validation['site_count']}")
    print(f"Fault instances          : {campaign_validation['fault_count']}")
    print(f"Canonical reference      : {reference['canonical_fault_id']}")
    print("Reference materialization: PASS")
    print("Temporary netlist kept   : NO")
    return 0


def build_site_order(
    selection: Mapping[str, Any],
    reference: Mapping[str, Any],
) -> tuple[list[str], dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    selected_sites = require_list(
        selection.get("selected_sites"), "Stage-4 selected_sites"
    )
    fault_instances = require_list(
        selection.get("fault_instances"), "Stage-4 fault_instances"
    )

    site_by_selection: dict[str, dict[str, Any]] = {}
    for item in selected_sites:
        if not isinstance(item, dict):
            raise ControlError("Stage-4 selected_sites contains a non-object record")
        selection_id = str(item.get("selection_id", ""))
        if not selection_id or selection_id in site_by_selection:
            raise ControlError(f"invalid or duplicate selection ID: {selection_id!r}")
        site_by_selection[selection_id] = dict(item)

    instances_by_selection: dict[str, list[dict[str, Any]]] = defaultdict(list)
    class_by_selection: dict[str, str] = {}
    for item in fault_instances:
        if not isinstance(item, dict):
            raise ControlError("Stage-4 fault_instances contains a non-object record")
        selection_id = str(item.get("selection_id", ""))
        if selection_id not in site_by_selection:
            raise ControlError(
                f"fault instance references unknown selection ID: {selection_id}"
            )
        instances_by_selection[selection_id].append(dict(item))
        value = str(item.get("fault_class", "UNKNOWN"))
        previous = class_by_selection.setdefault(selection_id, value)
        if previous != value:
            raise ControlError(
                f"one selected site has multiple fault classes: {selection_id}"
            )

    for selection_id, records in instances_by_selection.items():
        records.sort(
            key=lambda item: (
                int(item.get("stuck_at", 9)),
                str(item.get("fault_id", "")),
            )
        )
        if not records:
            raise ControlError(f"selected site has no fault instances: {selection_id}")

    reference_selection = str(reference["canonical_selection_id"])
    if reference_selection not in site_by_selection:
        raise ControlError(
            f"frozen reference selection is absent from Stage 4: {reference_selection}"
        )

    buckets: dict[str, list[str]] = defaultdict(list)
    for selection_id, site in site_by_selection.items():
        if selection_id == reference_selection:
            continue
        fault_class = class_by_selection.get(selection_id, "UNKNOWN")
        buckets[fault_class].append(selection_id)
    for values in buckets.values():
        values.sort(
            key=lambda selection_id: (
                int(site_by_selection[selection_id].get("selection_rank", 10**9)),
                str(site_by_selection[selection_id].get("module", "")),
                str(site_by_selection[selection_id].get("site_id", "")),
                selection_id,
            )
        )

    order = [reference_selection]
    reference_class = class_by_selection.get(reference_selection, "UNKNOWN")

    # The first four sites are the reference plus one site from every other
    # primary class.  This guarantees four-class coverage without changing the
    # canonical Stage-4 population or IDs.
    for fault_class in EXPECTED_CLASSES:
        if fault_class == reference_class:
            continue
        if buckets.get(fault_class):
            order.append(buckets[fault_class].pop(0))

    # Continue in a deterministic class round-robin while preserving each
    # class's original Stage-4 rank order.  Therefore sites_4 is a strict subset
    # of sites_20 and every larger prefix remains reproducible.
    class_order = list(EXPECTED_CLASSES)
    extra_classes = sorted(set(buckets) - set(class_order))
    class_order.extend(extra_classes)
    while len(order) < len(site_by_selection):
        progress = False
        for fault_class in class_order:
            values = buckets.get(fault_class, [])
            if not values:
                continue
            order.append(values.pop(0))
            progress = True
        if not progress:
            break

    if len(order) != len(site_by_selection) or len(set(order)) != len(order):
        raise ControlError(
            "failed to construct one complete deterministic Stage-4 site order"
        )
    return order, site_by_selection, instances_by_selection


def command_plan(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    freeze_root = require_directory(args.freeze_root, "freeze root")
    freeze = load_json(freeze_root / "interface_freeze.json", "interface freeze")
    reference = load_frozen_reference(freeze_root)
    selection_path = require_file(args.selection, "Stage-4 selection")
    campaign_root = require_directory(args.campaign_root, "Stage-5 campaign root")
    fault_specs_root = require_directory(
        campaign_root / "fault_specs", "canonical fault specs"
    )
    plan_root = args.plan_root.resolve()

    if args.site_count <= 0:
        raise ControlError("--site-count must be positive")

    frozen_stage4 = freeze.get("stage4")
    if not isinstance(frozen_stage4, dict):
        raise ControlError("interface freeze has no stage4 object")
    frozen_candidates = frozen_stage4.get("candidates")
    frozen_selection = frozen_stage4.get("selection")
    if (
        not isinstance(frozen_candidates, dict)
        or not isinstance(frozen_selection, dict)
    ):
        raise ControlError("interface freeze Stage-4 records are incomplete")
    if sha256_file(selection_path) != frozen_selection.get("sha256"):
        raise ControlError("current Stage-4 selection differs from frozen selection")
    validate_campaign_v108(
        campaign_root,
        reference,
        str(frozen_candidates.get("sha256", "")),
        str(frozen_selection.get("sha256", "")),
    )

    selection = load_json(selection_path, "Stage-4 selection")
    order, site_by_selection, instances_by_selection = build_site_order(
        selection, reference
    )
    if args.site_count > len(order):
        raise ControlError(
            f"requested site count exceeds canonical Stage-4 selection: "
            f"{args.site_count} > {len(order)}"
        )

    selected_ids = order[: args.site_count]
    reference_selection = str(reference["canonical_selection_id"])
    if reference_selection not in selected_ids:
        raise ControlError("internal error: batch plan omitted the reference site")

    selected_specs: list[tuple[str, Path, dict[str, Any]]] = []
    site_records: list[dict[str, Any]] = []
    for site_order, selection_id in enumerate(selected_ids, start=1):
        site = site_by_selection[selection_id]
        instances = instances_by_selection.get(selection_id, [])
        if not instances:
            raise ControlError(f"selected site has no fault instances: {selection_id}")
        fault_records: list[dict[str, Any]] = []
        for instance in instances:
            fault_id = str(instance.get("fault_id", ""))
            if FAULT_ID_RE.fullmatch(fault_id) is None:
                raise ControlError(f"invalid canonical fault ID: {fault_id!r}")
            spec_path = fault_specs_root / f"{fault_id}.json"
            spec = load_json(spec_path, f"canonical fault spec {fault_id}")
            if str(spec.get("program_version")) != "1.0.8":
                raise ControlError(
                    f"batch plan requires v1.0.8 fault specs: {fault_id}"
                )
            modification = spec.get("modification")
            if not isinstance(modification, dict) or modification.get(
                "materialization_layout_version"
            ) != "declaration_before_first_use_v1":
                raise ControlError(
                    f"batch plan requires declaration-before-first-use layout: {fault_id}"
                )
            if str(spec.get("selection_id")) != selection_id:
                raise ControlError(
                    f"fault spec selection mismatch: {fault_id} -> "
                    f"{spec.get('selection_id')!r}, expected {selection_id!r}"
                )
            selected_specs.append((fault_id, spec_path, spec))
            fault_records.append(
                {
                    "fault_id": fault_id,
                    "polarity": spec.get("polarity"),
                    "stuck_at": spec.get("stuck_at"),
                    "fault_spec": str(spec_path.resolve()),
                    "fault_spec_sha256": sha256_file(spec_path),
                }
            )
        representative_spec = selected_specs[-len(instances)][2]
        representative_site = representative_spec.get("site")
        if not isinstance(representative_site, dict):
            raise ControlError(
                f"canonical representative spec has no site object: {selection_id}"
            )
        fault_class = str(
            representative_spec.get(
                "fault_class",
                instances[0].get("fault_class", "UNKNOWN"),
            )
        )
        site_records.append(
            {
                "order": site_order,
                "selection_id": selection_id,
                "selection_rank": site.get("selection_rank"),
                "site_id": site.get("site_id"),
                "fault_class": fault_class,
                "module": representative_site.get("module"),
                "source_net": representative_site.get("source_net"),
                "is_reference_site": selection_id == reference_selection,
                "faults": fault_records,
            }
        )

    if plan_root.exists():
        existing_path = plan_root / "batch_plan.json"
        if existing_path.is_file():
            existing = load_json(existing_path, "existing batch plan")
            if (
                existing.get("requested_unique_site_count") == args.site_count
                and existing.get("reference_fault_id")
                == reference.get("canonical_fault_id")
                and [item.get("selection_id") for item in existing.get("sites", [])]
                == selected_ids
            ):
                expected_dir = plan_root / "selected_fault_specs"
                expected_ids = {item[0] for item in selected_specs}
                actual_paths = sorted(expected_dir.glob("TF*_SA*.json"))
                actual_ids = {path.stem for path in actual_paths}
                hashes_match = (
                    expected_dir.is_dir()
                    and actual_ids == expected_ids
                    and all(
                        sha256_file(expected_dir / f"{fault_id}.json")
                        == sha256_file(spec_path)
                        for fault_id, spec_path, _ in selected_specs
                    )
                )
                if hashes_match:
                    print(
                        f"Existing deterministic batch plan is valid: {plan_root}"
                    )
                    return 0
        raise ControlError(
            f"plan root already exists but does not match this request: {plan_root}"
        )

    selected_dir = plan_root / "selected_fault_specs"
    selected_dir.mkdir(parents=True, exist_ok=False)
    for fault_id, spec_path, _ in selected_specs:
        shutil.copy2(spec_path, selected_dir / f"{fault_id}.json")

    class_distribution = Counter(item["fault_class"] for item in site_records)
    fault_distribution = Counter(
        str(fault.get("polarity"))
        for site in site_records
        for fault in site["faults"]
    )
    plan = {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "kind": "stage5_nested_unique_site_batch_plan",
        "generated_at_utc": utc_now(),
        "repository_root": str(root),
        "stage4_selection": file_record(selection_path, root),
        "canonical_campaign_root": str(campaign_root),
        "canonical_fault_specs_root": str(fault_specs_root),
        "requested_unique_site_count": args.site_count,
        "selected_unique_site_count": len(site_records),
        "selected_fault_instance_count": len(selected_specs),
        "reference_fault_id": reference["canonical_fault_id"],
        "reference_selection_id": reference_selection,
        "reference_site_included": True,
        "selection_contract": {
            "source_population": "frozen canonical Stage-4 selection",
            "canonical_ids_preserved": True,
            "all_stage4_fault_instances_per_site_preserved": True,
            "first_site_is_reference": True,
            "first_four_sites_cover_primary_classes_when_available": True,
            "larger_batches_are_prefixes_of_one_frozen_site_order": True,
            "random_seed_used": False,
        },
        "distributions": {
            "site_fault_class": dict(class_distribution),
            "fault_polarity": dict(fault_distribution),
        },
        "selected_fault_specs_dir": str(selected_dir),
        "sites": site_records,
    }
    write_json(plan_root / "batch_plan.json", plan)

    print()
    print("======================================================================")
    print("Nested unique-site Stage-5 batch plan: PASS")
    print("======================================================================")
    print(f"Plan root              : {plan_root}")
    print(f"Requested sites        : {args.site_count}")
    print(f"Selected sites         : {len(site_records)}")
    print(f"Selected fault instances: {len(selected_specs)}")
    print(f"Reference fault        : {reference['canonical_fault_id']}")
    print(f"Reference site included: YES")
    print(f"Site classes           : {dict(class_distribution)}")
    print("Random seed used       : NO")
    return 0



def command_verify_prepared(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    plan_path = require_file(args.plan, "batch plan")
    batch_root = require_directory(args.batch_root, "prepared batch root")
    plan = load_json(plan_path, "batch plan")
    manifest = load_json(batch_root / "pilot_manifest.json", "batch manifest")

    plan_sites = require_list(plan.get("sites"), "batch plan sites")
    manifest_faults = require_list(
        manifest.get("faults"), "prepared batch manifest faults"
    )

    planned_faults = [
        dict(fault)
        for site in plan_sites
        if isinstance(site, dict)
        for fault in require_list(site.get("faults"), "batch plan site faults")
        if isinstance(fault, dict)
    ]
    planned_ids = [str(item.get("fault_id", "")) for item in planned_faults]
    manifest_ids = [
        str(item.get("fault_id", ""))
        for item in manifest_faults
        if isinstance(item, dict)
    ]

    errors: list[str] = []
    if len(planned_ids) != int(plan.get("selected_fault_instance_count", -1)):
        errors.append("batch plan fault count is internally inconsistent")
    if len(set(planned_ids)) != len(planned_ids):
        errors.append("batch plan contains duplicate fault IDs")
    if len(set(manifest_ids)) != len(manifest_ids):
        errors.append("prepared batch manifest contains duplicate fault IDs")
    if set(planned_ids) != set(manifest_ids):
        missing = sorted(set(planned_ids) - set(manifest_ids))
        extra = sorted(set(manifest_ids) - set(planned_ids))
        errors.append(
            f"prepared fault set differs from plan; missing={missing}, extra={extra}"
        )

    expected_reference = str(plan.get("reference_fault_id", ""))
    if expected_reference not in manifest_ids:
        errors.append(
            f"prepared batch omitted canonical reference: {expected_reference}"
        )

    manifest_site_count = manifest.get("distributions", {}).get("site_count")
    expected_site_count = plan.get("selected_unique_site_count")
    if manifest_site_count != expected_site_count:
        errors.append(
            "prepared unique-site count differs from plan: "
            f"{manifest_site_count!r} != {expected_site_count!r}"
        )

    planned_by_id = {
        str(item.get("fault_id", "")): item for item in planned_faults
    }
    for record in manifest_faults:
        if not isinstance(record, dict):
            errors.append("prepared batch manifest contains a non-object fault")
            continue
        fault_id = str(record.get("fault_id", ""))
        expected = planned_by_id.get(fault_id)
        if expected is None:
            continue
        copied_path = Path(str(record.get("fault_json", ""))).resolve()
        if not copied_path.is_file():
            errors.append(f"prepared copied fault spec is missing: {fault_id}")
            continue
        if sha256_file(copied_path) != expected.get("fault_spec_sha256"):
            errors.append(
                f"prepared copied fault spec SHA differs from plan: {fault_id}"
            )

    report_path = batch_root / "prepared_validation.json"
    report = {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "kind": "stage5_prepared_batch_validation",
        "generated_at_utc": utc_now(),
        "status": "PASS" if not errors else "FAIL",
        "repository_root": str(root),
        "plan": file_record(plan_path, root),
        "batch_root": str(batch_root),
        "requested_unique_site_count": plan.get(
            "requested_unique_site_count"
        ),
        "selected_unique_site_count": expected_site_count,
        "selected_fault_instance_count": len(planned_ids),
        "reference_fault_id": expected_reference,
        "errors": errors,
    }
    write_json(report_path, report, overwrite=report_path.exists())

    if errors:
        print("Prepared Stage-5 batch validation: FAIL", file=sys.stderr)
        for item in errors:
            print(f"  - {item}", file=sys.stderr)
        print(f"Report: {report_path}", file=sys.stderr)
        return 2

    print()
    print("======================================================================")
    print("Prepared Stage-5 batch validation: PASS")
    print("======================================================================")
    print(f"Plan                 : {plan_path}")
    print(f"Batch root           : {batch_root}")
    print(f"Selected sites       : {expected_site_count}")
    print(f"Selected faults      : {len(planned_ids)}")
    print(f"Canonical reference  : {expected_reference}")
    print(f"Validation report    : {report_path}")
    return 0

def normalize_time(value: Any) -> str:
    if isinstance(value, dict):
        raw_value = value.get("value")
        raw_unit = value.get("unit")
        if raw_value is not None and raw_unit is not None:
            value = f"{raw_value}{raw_unit}"
    return re.sub(r"\s+", "", str(value).strip().lower())


def event_time(event: Mapping[str, Any]) -> Any:
    for key in ("time", "simulation_time", "sim_time", "time_text"):
        if key in event:
            return event.get(key)
    return None


def command_verify_reference(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    freeze_root = require_directory(args.freeze_root, "freeze root")
    reference = load_frozen_reference(freeze_root)
    checkpoint = reference.get("checkpoint_expected")
    if not isinstance(checkpoint, dict):
        raise ControlError("frozen reference has no checkpoint_expected object")
    batch_root = require_directory(args.batch_root, "batch root")
    manifest = load_json(batch_root / "pilot_manifest.json", "batch manifest")
    canonical_fault_id = str(reference["canonical_fault_id"])

    faults = require_list(manifest.get("faults"), "batch manifest faults")
    matches = [
        dict(item)
        for item in faults
        if isinstance(item, dict) and item.get("fault_id") == canonical_fault_id
    ]
    if len(matches) != 1:
        raise ControlError(
            f"canonical reference is not uniquely present in batch: {canonical_fault_id}"
        )
    record = matches[0]
    fault_root = Path(str(record.get("fault_root", ""))).resolve()
    fault_json = load_json(fault_root / "fault.json", "batch reference fault spec")
    status = load_json(fault_root / "status.json", "batch reference status")
    validation = load_json(
        fault_root / "oracle/validation.json", "batch reference oracle validation"
    )
    oracle = load_json(fault_root / "oracle/oracle.json", "batch reference oracle")

    errors: list[str] = []
    checks: list[dict[str, Any]] = []

    def expect(condition: bool, message: str) -> None:
        checks.append(
            {
                "name": message,
                "status": "PASS" if condition else "FAIL",
            }
        )
        if not condition:
            errors.append(message)

    site = fault_json.get("site")
    expect(isinstance(site, dict), "fault spec site object is missing")
    if isinstance(site, dict):
        expect(site.get("module") == reference.get("module"), "reference module changed")
        expect(
            site.get("source_net") == reference.get("source_net"),
            "reference source_net changed",
        )
    expect(
        fault_json.get("fault_id") == canonical_fault_id,
        "reference fault ID changed",
    )
    expect(
        int(fault_json.get("stuck_at", -1)) == int(reference.get("stuck_at", -2)),
        "reference stuck-at changed",
    )
    expect(
        str(fault_json.get("program_version")) == "1.0.8",
        "reference fault spec is not v1.0.8",
    )
    modification = fault_json.get("modification")
    expect(
        isinstance(modification, dict)
        and modification.get("materialization_layout_version")
        == "declaration_before_first_use_v1",
        "reference materialization layout is not declaration-before-first-use",
    )

    expect(
        status.get("state") == "ORACLE_VALIDATED_CLEANED",
        f"reference final state is {status.get('state')!r}",
    )
    expect(validation.get("status") == "PASS", "oracle validation is not PASS")

    derived = oracle.get("derived_conclusions")
    expect(isinstance(derived, dict), "oracle derived_conclusions is missing")
    if isinstance(derived, dict):
        natural = derived.get("natural_execution")
        observe = derived.get("observe_execution")
        quarantine = derived.get("diagnostic_quarantine_execution")
        capability = derived.get("continuation_capability")
        expect(isinstance(natural, dict), "natural execution summary is missing")
        expect(isinstance(observe, dict), "OBSERVE execution summary is missing")
        expect(
            isinstance(quarantine, dict),
            "DIAGNOSTIC_QUARANTINE execution summary is missing",
        )
        expect(isinstance(capability, dict), "continuation capability is missing")
        if isinstance(natural, dict):
            expect(
                natural.get("runner_status") == "EXISTING_ASSERTION_DETECTED",
                f"Native status changed: {natural.get('runner_status')!r}",
            )
            expect(
                natural.get("architectural_outcome")
                == checkpoint.get("natural_architectural_outcome"),
                "natural architectural outcome changed",
            )
        if isinstance(observe, dict):
            expect(
                observe.get("runner_status") == checkpoint.get("observe_runner_status"),
                "OBSERVE status changed",
            )
        if isinstance(quarantine, dict):
            expect(
                quarantine.get("runner_status")
                == checkpoint.get("diagnostic_quarantine_runner_status"),
                "DIAGNOSTIC_QUARANTINE status changed",
            )
        if isinstance(capability, dict):
            expect(
                capability.get("validated_capability")
                == checkpoint.get("validated_capability"),
                "validated capability changed",
            )
            expect(
                capability.get("scope") == checkpoint.get("capability_scope"),
                "capability scope changed",
            )

    private = oracle.get("private_ground_truth")
    expect(isinstance(private, dict), "private ground truth is missing")
    boundary_events: dict[str, Mapping[str, Any] | None] = {
        "native": None,
        "observe": None,
        "diagnostic_quarantine": None,
    }
    if isinstance(private, dict):
        exact = private.get("exact_fault_injection_signal")
        boundaries = private.get("first_observable_detector_boundaries")
        expect(isinstance(exact, dict), "exact fault label is missing")
        expect(isinstance(boundaries, dict), "detector boundaries are missing")
        if isinstance(exact, dict):
            expect(exact.get("module") == reference.get("module"), "oracle module changed")
            expect(
                exact.get("source_net") == reference.get("source_net"),
                "oracle source_net changed",
            )
        if isinstance(boundaries, dict):
            for mode in boundary_events:
                candidate = boundaries.get(mode)
                if isinstance(candidate, dict):
                    boundary_events[mode] = candidate
                else:
                    errors.append(f"{mode} first detector event is missing")

    # Native Xcelium ASRTST evidence records simulation time but does not carry
    # the monitor cycle.  OBSERVE/QUARANTINE structured events carry both.
    # The G5 checkpoint cycle is therefore checked against the first available
    # structured diagnostic boundary, while time is checked across every mode.
    expected_cycle = checkpoint.get("first_observable_detector_cycle")
    cycle_event = next(
        (
            event
            for mode in ("observe", "diagnostic_quarantine", "native")
            for event in [boundary_events.get(mode)]
            if isinstance(event, Mapping) and event.get("cycle") is not None
        ),
        None,
    )
    if expected_cycle is not None:
        expect(cycle_event is not None, "no detector boundary contains a cycle")
        if cycle_event is not None:
            expect(
                cycle_event.get("cycle") == expected_cycle,
                f"first detector cycle changed: {cycle_event.get('cycle')!r}",
            )

    expected_time = checkpoint.get("first_observable_detector_time")
    if expected_time is not None:
        for mode, event in boundary_events.items():
            if event is None:
                continue
            actual_time = event_time(event)
            expect(actual_time is not None, f"{mode} detector time is missing")
            if actual_time is not None:
                expect(
                    normalize_time(actual_time) == normalize_time(expected_time),
                    f"{mode} detector time changed: {actual_time!r}",
                )

    qualification_path = batch_root / "reference_qualification.json"
    report = {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "kind": "stage5_canonical_reference_batch_qualification",
        "generated_at_utc": utc_now(),
        "status": "PASS" if not errors else "FAIL",
        "batch_root": str(batch_root),
        "canonical_fault_id": canonical_fault_id,
        "canonical_selection_id": reference.get("canonical_selection_id"),
        "canonical_site_id": reference.get("canonical_site_id"),
        "physical_identity": {
            "module": reference.get("module"),
            "source_net": reference.get("source_net"),
            "polarity": reference.get("polarity"),
            "stuck_at": reference.get("stuck_at"),
        },
        "expected_checkpoint": checkpoint,
        "checks": checks,
        "observed": {
            "status_state": status.get("state"),
            "oracle_validation": validation.get("status"),
            "detector_boundaries": {
                mode: dict(event) if event is not None else None
                for mode, event in boundary_events.items()
            },
        },
        "errors": errors,
    }
    write_json(qualification_path, report, overwrite=qualification_path.exists())

    if errors:
        print("Canonical reference batch qualification: FAIL", file=sys.stderr)
        for item in errors:
            print(f"  - {item}", file=sys.stderr)
        print(f"Report: {qualification_path}", file=sys.stderr)
        return 2

    print()
    print("======================================================================")
    print("Canonical reference batch qualification: PASS")
    print("======================================================================")
    print(f"Batch root              : {batch_root}")
    print(f"Canonical fault ID      : {canonical_fault_id}")
    print(f"Module                  : {reference['module']}")
    print(f"Source signal           : {reference['source_net']}")
    print("Native outcome          : CENSORED")
    print("OBSERVE                  : DIAGNOSTIC_TIMEOUT")
    print("DIAGNOSTIC_QUARANTINE    : DIAGNOSTIC_TIMEOUT")
    print("Validated capability    : NON_CONTINUABLE")
    print(f"Qualification report    : {qualification_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    root_default = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {PROGRAM_VERSION}")
    parser.add_argument("--root", type=Path, default=root_default)
    subparsers = parser.add_subparsers(dest="command", required=True)

    freeze = subparsers.add_parser(
        "freeze-reference",
        help="freeze Stage-4 inputs and recover the canonical G5 reference",
    )
    freeze.add_argument("--candidates", type=Path)
    freeze.add_argument("--selection", type=Path)
    freeze.add_argument("--mini-selection", type=Path)
    freeze.add_argument("--checkpoint-manifest", type=Path)
    freeze.add_argument("--checkpoint-report", type=Path)
    freeze.add_argument("--freeze-root", type=Path)
    freeze.set_defaults(func=command_freeze_reference)

    rebuild = subparsers.add_parser(
        "rebuild-campaign",
        help="rebuild the canonical full Stage-5 campaign with active v1.0.8",
    )
    rebuild.add_argument("--freeze-root", type=Path)
    rebuild.add_argument("--candidates", type=Path)
    rebuild.add_argument("--selection", type=Path)
    rebuild.add_argument("--stage5-tool", type=Path)
    rebuild.add_argument("--site-catalog-tool", type=Path)
    rebuild.add_argument("--policy", type=Path)
    rebuild.add_argument("--campaign-root", type=Path)
    rebuild.add_argument("--archive-root", type=Path)
    rebuild.set_defaults(func=command_rebuild_campaign)

    plan = subparsers.add_parser(
        "plan",
        help="build one nested unique-site batch plan from the frozen Stage-4 order",
    )
    plan.add_argument("--freeze-root", type=Path)
    plan.add_argument("--selection", type=Path)
    plan.add_argument("--campaign-root", type=Path)
    plan.add_argument("--site-count", type=int, required=True)
    plan.add_argument("--plan-root", type=Path, required=True)
    plan.set_defaults(func=command_plan)

    prepared = subparsers.add_parser(
        "verify-prepared",
        help="verify that a prepared engine batch exactly matches its site plan",
    )
    prepared.add_argument("--plan", type=Path, required=True)
    prepared.add_argument("--batch-root", type=Path, required=True)
    prepared.set_defaults(func=command_verify_prepared)

    verify = subparsers.add_parser(
        "verify-reference",
        help="qualify the canonical reference result from the generic batch path",
    )
    verify.add_argument("--freeze-root", type=Path)
    verify.add_argument("--batch-root", type=Path, required=True)
    verify.set_defaults(func=command_verify_reference)
    return parser


def apply_defaults(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    if args.command == "freeze-reference":
        args.candidates = args.candidates or candidates_default(root)
        args.selection = args.selection or selection_default(root)
        args.mini_selection = args.mini_selection or mini_selection_default(root)
        args.checkpoint_manifest = (
            args.checkpoint_manifest or checkpoint_manifest_default(root)
        )
        args.checkpoint_report = args.checkpoint_report or checkpoint_report_default(root)
        args.freeze_root = args.freeze_root or freeze_default(root)
    elif args.command == "rebuild-campaign":
        args.freeze_root = args.freeze_root or freeze_default(root)
        args.candidates = args.candidates or candidates_default(root)
        args.selection = args.selection or selection_default(root)
        args.stage5_tool = args.stage5_tool or stage5_tool_default(root)
        args.site_catalog_tool = (
            args.site_catalog_tool or site_catalog_tool_default(root)
        )
        args.policy = args.policy or site_policy_default(root)
        args.campaign_root = args.campaign_root or campaign_default(root)
        args.archive_root = args.archive_root or (
            root / "runs/stage5_archives/full_stage5_pre_v108_batch"
        )
    elif args.command == "plan":
        args.freeze_root = args.freeze_root or freeze_default(root)
        args.selection = args.selection or selection_default(root)
        args.campaign_root = args.campaign_root or campaign_default(root)
    elif args.command == "verify-reference":
        args.freeze_root = args.freeze_root or freeze_default(root)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    apply_defaults(args)
    try:
        return int(args.func(args))
    except ControlError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"ERROR: unexpected {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
