#!/usr/bin/env python3
"""Minimal site-based Stage-5 batch pilot orchestrator.

The orchestrator reuses the existing Stage-5 fault wrapper and Phase-2 mode
composer. It adds only the batch engineering needed for a pilot campaign:

* site/SA0/SA1 directory layout;
* parameterized fault IDs;
* Native-first routing;
* registry-driven detector matching;
* generic oracle construction and independent validation;
* work-directory cleanup only after oracle validation PASS;
* failure retention and resumability;
* periodic storage reports.
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
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = "1.0"
PROGRAM_VERSION = "1.0.1"

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
FINAL_STATES = {
    "ORACLE_VALIDATED_CLEANED",
    "BLOCKED_UNREGISTERED_DETECTOR",
    "BLOCKED_AMBIGUOUS_DETECTOR",
    "BLOCKED_UNSUPPORTED_REGISTERED_DETECTOR",
    "FAILED",
}
FAULT_ID_RE = re.compile(r"^(?P<base>TF\d{6})_SA(?P<sa>[01])$")


class BatchError(RuntimeError):
    """Controlled pilot orchestration failure."""


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


def require_checkpoint(path: Path) -> dict[str, Any]:
    """Validate the frozen Phase2-G5 independent smoke report.

    The repository does not use a synthetic aggregate G1-G5 checkpoint file.
    G5 is built only after G2/G3/G4 PASS and is independently replay-validated,
    so its PASS report is the minimal durable prerequisite for the batch pilot.
    """

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


def polarity_dir(spec: Mapping[str, Any]) -> str:
    stuck_at = spec.get("stuck_at")
    if stuck_at not in (0, 1):
        match = FAULT_ID_RE.fullmatch(str(spec.get("fault_id", "")))
        if match is None:
            raise BatchError(f"cannot resolve polarity for {spec.get('fault_id')}")
        stuck_at = int(match.group("sa"))
    return f"SA{stuck_at}"


def base_fault_id(spec: Mapping[str, Any]) -> str:
    value = spec.get("base_fault_id")
    if isinstance(value, str) and value:
        return value
    match = FAULT_ID_RE.fullmatch(str(spec.get("fault_id", "")))
    if match is None:
        raise BatchError(f"invalid fault ID: {spec.get('fault_id')}")
    return match.group("base")


def load_fault_specs(selected_dir: Path) -> dict[str, tuple[Path, dict[str, Any]]]:
    result: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path in sorted(selected_dir.glob("*.json")):
        spec = load_json(path, "selected fault spec")
        fault_id = str(spec.get("fault_id", ""))
        if FAULT_ID_RE.fullmatch(fault_id) is None:
            continue
        if fault_id in result:
            raise BatchError(f"duplicate selected fault ID: {fault_id}")
        result[fault_id] = (path.resolve(), spec)
    if not result:
        raise BatchError(f"no selected fault specs found: {selected_dir}")
    return result


def stable_site_key(seed: int, base_id: str) -> str:
    return hashlib.sha256(f"{seed}:{base_id}".encode("utf-8")).hexdigest()


def select_pilot_faults(
    specs: Mapping[str, tuple[Path, dict[str, Any]]],
    *,
    count: int,
    seed: int,
    include_faults: Sequence[str],
) -> list[str]:
    if count <= 0:
        raise BatchError("pilot count must be positive")
    if count > len(specs):
        raise BatchError(
            f"pilot count exceeds available faults: {count} > {len(specs)}"
        )

    by_site: dict[str, list[str]] = defaultdict(list)
    site_class: dict[str, str] = {}
    for fault_id, (_, spec) in specs.items():
        base = base_fault_id(spec)
        by_site[base].append(fault_id)
        site_class[base] = str(spec.get("fault_class", "UNKNOWN"))
    for fault_ids in by_site.values():
        fault_ids.sort(key=lambda item: ("_SA1" in item, item))

    selected: list[str] = []
    selected_set: set[str] = set()
    consumed_sites: set[str] = set()

    def add_site(base: str) -> None:
        nonlocal selected
        for fault_id in by_site[base]:
            if len(selected) >= count:
                break
            if fault_id not in selected_set:
                selected.append(fault_id)
                selected_set.add(fault_id)
        consumed_sites.add(base)

    for fault_id in include_faults:
        if fault_id not in specs:
            raise BatchError(f"requested reference fault is unavailable: {fault_id}")
        add_site(base_fault_id(specs[fault_id][1]))
        if len(selected) >= count:
            return selected[:count]

    buckets: dict[str, list[str]] = defaultdict(list)
    for base in by_site:
        if base not in consumed_sites:
            buckets[site_class[base]].append(base)
    for values in buckets.values():
        values.sort(key=lambda item: stable_site_key(seed, item))

    classes = sorted(buckets)
    while len(selected) < count:
        progress = False
        for class_name in classes:
            values = buckets[class_name]
            if not values:
                continue
            base = values.pop(0)
            add_site(base)
            progress = True
            if len(selected) >= count:
                break
        if not progress:
            break

    if len(selected) != count:
        raise BatchError(
            f"could not select requested pilot count: {len(selected)} != {count}"
        )
    return selected


def fault_root(pilot_root: Path, spec: Mapping[str, Any]) -> Path:
    return pilot_root / "sites" / base_fault_id(spec) / polarity_dir(spec)


def current_status(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "schema_version": SCHEMA_VERSION,
            "state": "PREPARED",
            "updated_at_utc": utc_now(),
        }
    return load_json(path, "fault status")


def update_status(path: Path, **values: Any) -> None:
    status = current_status(path)
    status.update(values)
    status["schema_version"] = SCHEMA_VERSION
    status["updated_at_utc"] = utc_now()
    write_json(path, status)


def prepare_pilot(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    pilot_root = args.pilot_root.resolve()
    require_checkpoint(args.checkpoint.resolve())
    specs = load_fault_specs(args.selected_dir.resolve())

    if pilot_root.exists():
        manifest = pilot_root / "pilot_manifest.json"
        if manifest.is_file() and not args.force:
            print(f"Pilot already prepared: {pilot_root}")
            print(f"Manifest              : {manifest}")
            return 0
        if not args.force:
            raise BatchError(f"pilot root already exists: {pilot_root}")
        shutil.rmtree(pilot_root)

    selected = select_pilot_faults(
        specs,
        count=args.count,
        seed=args.seed,
        include_faults=args.include_fault,
    )
    pilot_root.mkdir(parents=True, exist_ok=False)

    stage5_tool = root / "scripts/fault_characterization/stage5_faults.py"
    if not stage5_tool.is_file():
        raise BatchError(f"Stage5 fault tool not found: {stage5_tool}")

    site_records: dict[str, dict[str, Any]] = {}
    fault_records: list[dict[str, Any]] = []

    all_by_site: dict[str, list[str]] = defaultdict(list)
    for fault_id, (_, spec) in specs.items():
        all_by_site[base_fault_id(spec)].append(fault_id)

    for order, fault_id in enumerate(selected, start=1):
        original_path, spec = specs[fault_id]
        base = base_fault_id(spec)
        pol = polarity_dir(spec)
        destination = fault_root(pilot_root, spec)
        base_dir = destination / "base"
        base_dir.mkdir(parents=True, exist_ok=True)

        copied_fault = destination / "fault.json"
        shutil.copy2(original_path, copied_fault)
        copied_spec = load_json(copied_fault, "copied fault spec")
        if copied_spec.get("fault_id") != fault_id:
            raise BatchError(f"copied fault ID mismatch: {fault_id}")

        base_monitor = base_dir / "base_monitor.sv"
        base_manifest = base_dir / "base_manifest.json"
        unused_trace = base_dir / "base_unused.trace.tsv"
        status = command(
            [
                sys.executable,
                str(stage5_tool),
                "make-fault-monitor",
                "--fault-json",
                str(copied_fault),
                "--trace-output",
                str(unused_trace),
                "--output",
                str(base_monitor),
                "--manifest",
                str(base_manifest),
            ]
        )
        if status != 0:
            raise BatchError(f"base monitor generation failed for {fault_id}")

        status_path = destination / "status.json"
        update_status(
            status_path,
            state="PREPARED",
            fault_id=fault_id,
            base_fault_id=base,
            polarity=pol,
            order=order,
        )

        site_dir = pilot_root / "sites" / base
        selected_for_site = [
            item for item in selected if base_fault_id(specs[item][1]) == base
        ]
        site_record = {
            "schema_version": SCHEMA_VERSION,
            "base_fault_id": base,
            "available_fault_ids": sorted(all_by_site[base]),
            "selected_fault_ids": selected_for_site,
            "available_polarities": sorted(
                polarity_dir(specs[item][1]) for item in all_by_site[base]
            ),
            "selected_polarities": sorted(
                polarity_dir(specs[item][1]) for item in selected_for_site
            ),
            "fault_class": spec.get("fault_class"),
            "module": spec.get("site", {}).get("module"),
            "source_net": spec.get("site", {}).get("source_net"),
        }
        site_records[base] = site_record
        write_json(site_dir / "site.json", site_record)

        fault_records.append(
            {
                "order": order,
                "fault_id": fault_id,
                "base_fault_id": base,
                "polarity_directory": pol,
                "fault_root": str(destination),
                "fault_json": str(copied_fault),
                "original_fault_json": str(original_path),
                "fault_class": spec.get("fault_class"),
                "stuck_at": spec.get("stuck_at"),
                "module": spec.get("site", {}).get("module"),
                "source_net": spec.get("site", {}).get("source_net"),
            }
        )

    distributions = {
        "fault_class": dict(Counter(item["fault_class"] for item in fault_records)),
        "polarity": dict(
            Counter(item["polarity_directory"] for item in fault_records)
        ),
        "site_count": len(site_records),
        "fault_count": len(fault_records),
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "kind": "stage5_site_based_batch_pilot",
        "generated_at_utc": utc_now(),
        "repository_root": str(root),
        "pilot_root": str(pilot_root),
        "smoke_validation_report": str(args.checkpoint.resolve()),
        "selected_dir": str(args.selected_dir.resolve()),
        "requested_count": args.count,
        "selected_count": len(fault_records),
        "seed": args.seed,
        "included_reference_faults": list(args.include_fault),
        "site_based_layout": True,
        "distributions": distributions,
        "faults": fault_records,
    }
    write_json(pilot_root / "pilot_manifest.json", manifest)

    print()
    print("======================================================================")
    print("Stage5 site-based pilot preparation: PASS")
    print("======================================================================")
    print(f"Pilot root      : {pilot_root}")
    print(f"Selected sites  : {len(site_records)}")
    print(f"Selected faults : {len(fault_records)}")
    print(f"Manifest        : {pilot_root / 'pilot_manifest.json'}")
    return 0


def preserve_incomplete_mode(mode_dir: Path) -> None:
    if not mode_dir.exists():
        return
    result = mode_dir / "run" / "result.json"
    if result.is_file():
        return
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = mode_dir.with_name(f"{mode_dir.name}_incomplete_{stamp}")
    counter = 1
    while target.exists():
        target = mode_dir.with_name(
            f"{mode_dir.name}_incomplete_{stamp}_{counter}"
        )
        counter += 1
    mode_dir.rename(target)


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
) -> tuple[str, Path]:
    mode_dir = fault_dir / mode_name
    run_dir = mode_dir / "run"
    result_path = run_dir / "result.json"
    if result_path.is_file():
        result = load_json(result_path, f"existing {mode_name} result")
        print(f"Reuse {mode_name} result: {result.get('status')}")
        return str(result.get("status")), run_dir

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
    """Resolve routing only from the verdict-selected detector identity.

    Searching an entire compile/runtime log for detector names is unsound: a
    detector name can appear in source listings even when a different detector
    caused termination. The verdict engine already performs fail-closed
    signature resolution, so batch routing must consume that exact result.
    """

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
    if oracle_dir.exists() and not (oracle_dir / "validation.json").is_file():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        oracle_dir.rename(fault_dir / f"oracle_incomplete_{stamp}")
    oracle_dir.mkdir(parents=True, exist_ok=True)

    oracle = oracle_dir / "oracle.json"
    prompt = oracle_dir / "prompt_context.json"
    validation = oracle_dir / "validation.json"
    if validation.is_file():
        report = load_json(validation, "existing oracle validation")
        if report.get("status") == "PASS":
            return validation

    builder = root / "scripts/fault_characterization/stage5_batch_oracle.py"
    validator = (
        root
        / "scripts/fault_characterization/stage5_batch_oracle_validate.py"
    )
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
    pilot_root: Path,
    record: Mapping[str, Any],
    maxcycles: int,
) -> str:
    fault_id = str(record["fault_id"])
    fault_dir = Path(str(record["fault_root"])).resolve()
    fault_json = Path(str(record["fault_json"])).resolve()
    status_path = fault_dir / "status.json"
    status = current_status(status_path)
    if status.get("state") == "ORACLE_VALIDATED_CLEANED":
        print(f"Skip completed fault: {fault_id}")
        return "ORACLE_VALIDATED_CLEANED"
    if status.get("state") in {
        "BLOCKED_UNREGISTERED_DETECTOR",
        "BLOCKED_AMBIGUOUS_DETECTOR",
        "BLOCKED_UNSUPPORTED_REGISTERED_DETECTOR",
    }:
        print(f"Skip blocked fault: {fault_id} ({status.get('state')})")
        return str(status.get("state"))

    print()
    print("======================================================================")
    print(f"Stage5 pilot fault: {fault_id}")
    print("======================================================================")
    update_status(status_path, state="RUNNING_NATIVE", fault_id=fault_id)

    native_status, native_run = run_mode(
        root=root,
        fault_dir=fault_dir,
        fault_json=fault_json,
        mode_name="native",
        compose_mode="NATIVE",
        run_purpose="NATIVE_CHARACTERIZATION",
        mm_ram_profile="native",
        maxcycles=maxcycles,
    )
    if native_status not in NATIVE_SCIENTIFIC:
        raise BatchError(f"non-scientific Native status: {native_status}")

    registry_path = (
        root / "platform/cv32e40p/stage5_assertion_policy_v1.json"
    )
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
        state="ORACLE_VALIDATED_CLEANED",
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
    return "ORACLE_VALIDATED_CLEANED"


def storage_report(pilot_root: Path, sequence: int | None = None) -> Path:
    manifest = load_json(pilot_root / "pilot_manifest.json", "pilot manifest")
    total = directory_bytes(pilot_root)
    work_paths = list(pilot_root.glob("sites/*/SA*/**/run/work"))
    work_bytes = sum(directory_bytes(path) for path in work_paths)
    states: Counter[str] = Counter()
    per_fault: list[dict[str, Any]] = []
    for record in manifest["faults"]:
        fault_dir = Path(str(record["fault_root"])).resolve()
        status_path = fault_dir / "status.json"
        state = current_status(status_path).get("state", "UNKNOWN")
        states[str(state)] += 1
        size = directory_bytes(fault_dir)
        fault_work = sum(
            directory_bytes(path)
            for path in fault_dir.glob("**/run/work")
            if path.is_dir()
        )
        per_fault.append(
            {
                "fault_id": record["fault_id"],
                "state": state,
                "bytes": size,
                "work_bytes": fault_work,
                "durable_estimate_bytes": max(0, size - fault_work),
            }
        )
    disk = shutil.disk_usage(pilot_root)
    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": "stage5_batch_pilot_storage_report",
        "generated_at_utc": utc_now(),
        "sequence": sequence,
        "pilot_root": str(pilot_root),
        "total_bytes": total,
        "work_bytes": work_bytes,
        "durable_estimate_bytes": max(0, total - work_bytes),
        "filesystem_free_bytes": disk.free,
        "states": dict(states),
        "faults": per_fault,
    }
    storage_dir = pilot_root / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"_{sequence:04d}" if sequence is not None else ""
    snapshot = storage_dir / f"storage_report{suffix}.json"
    write_json(snapshot, report)
    write_json(storage_dir / "latest.json", report)

    text = [
        "Stage5 pilot storage report",
        "===========================",
        f"Pilot root       : {pilot_root}",
        f"Total            : {human_bytes(total)}",
        f"Retained work    : {human_bytes(work_bytes)}",
        f"Durable estimate : {human_bytes(max(0, total - work_bytes))}",
        f"Filesystem free  : {human_bytes(disk.free)}",
        f"States           : {dict(states)}",
    ]
    (storage_dir / "latest.txt").write_text("\n".join(text) + "\n")
    print("\n".join(text))
    return snapshot


def run_pilot(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    pilot_root = args.pilot_root.resolve()
    require_checkpoint(args.checkpoint.resolve())
    manifest = load_json(pilot_root / "pilot_manifest.json", "pilot manifest")
    failures = 0
    attempted = 0

    for record in manifest.get("faults", []):
        if not isinstance(record, dict):
            raise BatchError("pilot manifest contains invalid fault record")
        fault_id = str(record.get("fault_id"))
        try:
            state = run_one_fault(
                root=root,
                pilot_root=pilot_root,
                record=record,
                maxcycles=args.maxcycles,
            )
            if state in {
                "FAILED",
                "BLOCKED_UNREGISTERED_DETECTOR",
                "BLOCKED_AMBIGUOUS_DETECTOR",
                "BLOCKED_UNSUPPORTED_REGISTERED_DETECTOR",
            }:
                failures += 1
        except Exception as exc:  # preserve the fault and continue the pilot
            failures += 1
            fault_dir = Path(str(record["fault_root"])).resolve()
            update_status(
                fault_dir / "status.json",
                state="FAILED",
                work_retained=True,
                failure_reason=str(exc),
                failure_type=type(exc).__name__,
            )
            print(f"ERROR: pilot fault failed: {fault_id}: {exc}", file=sys.stderr)
        attempted += 1
        if args.storage_interval > 0 and attempted % args.storage_interval == 0:
            storage_report(pilot_root, attempted)

    storage_report(pilot_root, attempted)
    summary_path = write_status_summary(pilot_root)

    print()
    print("======================================================================")
    print("Stage5 site-based pilot run complete")
    print("======================================================================")
    print(f"Pilot root        : {pilot_root}")
    print(f"Faults processed  : {attempted}")
    print(f"Blocked/failed    : {failures}")
    print(f"Status summary    : {summary_path}")
    if failures:
        print("Pilot verdict     : REVIEW_REQUIRED")
        return 2
    print("Pilot verdict     : PASS")
    return 0


def write_status_summary(pilot_root: Path) -> Path:
    manifest = load_json(pilot_root / "pilot_manifest.json", "pilot manifest")
    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for record in manifest.get("faults", []):
        fault_dir = Path(str(record["fault_root"])).resolve()
        status = current_status(fault_dir / "status.json")
        state = str(status.get("state", "UNKNOWN"))
        counts[state] += 1
        rows.append(
            {
                "order": record.get("order"),
                "fault_id": record.get("fault_id"),
                "base_fault_id": record.get("base_fault_id"),
                "state": state,
                "native_status": status.get("native_status"),
                "route": status.get("route"),
                "observe_status": status.get("observe_status"),
                "diagnostic_quarantine_status": status.get(
                    "diagnostic_quarantine_status"
                ),
                "validated_capability": status.get("validated_capability"),
                "failure_reason": status.get("failure_reason"),
            }
        )
    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": "stage5_batch_pilot_status",
        "generated_at_utc": utc_now(),
        "pilot_root": str(pilot_root),
        "counts": dict(counts),
        "faults": rows,
    }
    path = pilot_root / "pilot_status.json"
    write_json(path, report)
    print(json.dumps(report["counts"], indent=2))
    return path


def run_one_command(args: argparse.Namespace) -> int:
    manifest = load_json(
        args.pilot_root.resolve() / "pilot_manifest.json", "pilot manifest"
    )
    matches = [
        item
        for item in manifest.get("faults", [])
        if isinstance(item, dict) and item.get("fault_id") == args.fault_id
    ]
    if len(matches) != 1:
        raise BatchError(f"fault is not uniquely selected in pilot: {args.fault_id}")
    try:
        state = run_one_fault(
            root=args.root.resolve(),
            pilot_root=args.pilot_root.resolve(),
            record=matches[0],
            maxcycles=args.maxcycles,
        )
        storage_report(args.pilot_root.resolve(), 1)
        write_status_summary(args.pilot_root.resolve())
        return 0 if state == "ORACLE_VALIDATED_CLEANED" else 2
    except Exception as exc:
        fault_dir = Path(str(matches[0]["fault_root"])).resolve()
        update_status(
            fault_dir / "status.json",
            state="FAILED",
            work_retained=True,
            failure_reason=str(exc),
            failure_type=type(exc).__name__,
        )
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


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
        "--pilot-root",
        type=Path,
        default=(
            root_default
            / "runs/stage5_campaign_v1/cv32e40p/crc32/pilot_20"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="prepare a deterministic pilot")
    prepare.add_argument(
        "--selected-dir",
        type=Path,
        default=root_default / "faults/cv32e40p/stage5/fault_specs",
    )
    prepare.add_argument("--count", type=int, default=20)
    prepare.add_argument("--seed", type=int, default=20260801)
    prepare.add_argument("--include-fault", action="append", default=[])
    prepare.add_argument("--force", action="store_true")
    prepare.set_defaults(func=prepare_pilot)

    run_one = sub.add_parser("run-one", help="run or resume one selected fault")
    run_one.add_argument("--fault-id", required=True)
    run_one.add_argument("--maxcycles", type=int, default=2_000_000)
    run_one.set_defaults(func=run_one_command)

    run_all = sub.add_parser("run-pilot", help="run or resume all pilot faults")
    run_all.add_argument("--maxcycles", type=int, default=2_000_000)
    run_all.add_argument("--storage-interval", type=int, default=5)
    run_all.set_defaults(func=run_pilot)

    status = sub.add_parser("status", help="write and print pilot status")
    status.set_defaults(
        func=lambda values: (
            write_status_summary(values.pilot_root.resolve()) and 0
        )
    )

    storage = sub.add_parser("storage-report", help="write pilot storage report")
    storage.set_defaults(
        func=lambda values: (storage_report(values.pilot_root.resolve()) and 0)
    )
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
