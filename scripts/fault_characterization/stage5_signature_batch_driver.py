#!/usr/bin/env python3
"""Fail-closed Stage-5 batch driver with limited signature enumeration.

The driver reuses the existing stage5_batch.py implementation.  It does not
rerun completed faults.  For each failed Native run it:

1. classifies the saved xrun.log against the reviewed signature whitelist;
2. stops immediately for unknown, ambiguous, or real infrastructure failures;
3. archives small result/status files for a registered detector signature;
4. repairs the Native result without rerunning Xcelium;
5. resumes only that fault through routing, diagnostics, oracle validation, and
   cleanup.

This workflow can be used for 4-site, 20-site, 100-site, and full campaigns.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "1.0"
PROGRAM_VERSION = "1.0.0"
COMPLETED = "ORACLE_VALIDATED_CLEANED"
BLOCKED_STATES = {
    "BLOCKED_UNREGISTERED_DETECTOR",
    "BLOCKED_AMBIGUOUS_DETECTOR",
    "BLOCKED_UNSUPPORTED_REGISTERED_DETECTOR",
}


class DriverError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DriverError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DriverError(f"invalid {label} JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DriverError(f"{label} must contain one JSON object: {path}")
    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def run_command(arguments: Sequence[str]) -> int:
    print("+ " + " ".join(str(item) for item in arguments), flush=True)
    completed = subprocess.run([str(item) for item in arguments], check=False)
    return int(completed.returncode)


def manifest_records(batch_root: Path) -> list[dict[str, Any]]:
    manifest = load_json(batch_root / "pilot_manifest.json", "pilot manifest")
    raw = manifest.get("faults")
    if not isinstance(raw, list) or not raw:
        raise DriverError("pilot manifest has no faults array")
    records = [dict(item) for item in raw if isinstance(item, dict)]
    if len(records) != len(raw):
        raise DriverError("pilot manifest contains a non-object fault record")
    return sorted(records, key=lambda item: int(item.get("order", 0)))


def fault_paths(record: Mapping[str, Any]) -> dict[str, Path]:
    fault_root = Path(str(record["fault_root"])).expanduser().resolve()
    native_run = fault_root / "native" / "run"
    return {
        "fault_root": fault_root,
        "status": fault_root / "status.json",
        "native_run": native_run,
        "xrun_log": native_run / "xrun.log",
        "result_json": native_run / "result.json",
        "result_env": native_run / "result.env",
        "result_text": native_run / "result.txt",
    }


def current_state(record: Mapping[str, Any]) -> str:
    paths = fault_paths(record)
    if not paths["status"].is_file():
        return "PREPARED"
    return str(load_json(paths["status"], "fault status").get("state", "UNKNOWN"))


def update_status_for_resume(path: Path, fault_id: str, classification: Mapping[str, Any]) -> None:
    status = load_json(path, "fault status") if path.is_file() else {}
    for key in ("failure_reason", "failure_type", "work_retained"):
        status.pop(key, None)
    status.update(
        {
            "schema_version": "1.0",
            "state": "PREPARED",
            "fault_id": fault_id,
            "signature_reclassified": True,
            "signature_dedupe_key": classification.get("dedupe_key"),
            "registered_detector_id": (
                classification.get("detector_match", {}).get("detector_id")
                if isinstance(classification.get("detector_match"), dict)
                else None
            ),
            "updated_at_utc": utc_now(),
        }
    )
    write_json(path, status)


def classify_one(
    *,
    root: Path,
    policy: Path,
    record: Mapping[str, Any],
    report_dir: Path,
) -> dict[str, Any]:
    paths = fault_paths(record)
    fault_id = str(record["fault_id"])
    output = report_dir / f"{fault_id}.signature.json"
    classifier = root / "scripts/fault_characterization/stage5_signature_classifier.py"
    status = run_command(
        [
            sys.executable,
            str(classifier),
            "--policy",
            str(policy),
            "classify",
            "--log",
            str(paths["xrun_log"]),
            "--output",
            str(output),
        ]
    )
    if not output.is_file():
        raise DriverError(f"signature classifier produced no report for {fault_id}")
    report = load_json(output, "signature classification")
    report["classifier_exit_status"] = status
    report["fault_id"] = fault_id
    report["fault_root"] = str(paths["fault_root"])
    write_json(output, report)
    return report


def archive_small_state(
    *,
    batch_root: Path,
    fault_id: str,
    paths: Mapping[str, Path],
    classification: Mapping[str, Any],
) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = batch_root / "signature_recovery" / "archives" / stamp / fault_id
    archive.mkdir(parents=True, exist_ok=False)
    for key in ("status", "result_json", "result_env", "result_text"):
        source = paths[key]
        if source.is_file():
            shutil.copy2(source, archive / source.name)
    write_json(archive / "classification.json", classification)
    return archive


def repair_registered_result(
    *,
    root: Path,
    policy: Path,
    batch_root: Path,
    record: Mapping[str, Any],
    classification: Mapping[str, Any],
) -> Path:
    if classification.get("semantic_class") != "REGISTERED_DETECTOR_TERMINATION":
        raise DriverError("refusing to repair a non-registered termination")
    paths = fault_paths(record)
    fault_id = str(record["fault_id"])
    archive = archive_small_state(
        batch_root=batch_root,
        fault_id=fault_id,
        paths=paths,
        classification=classification,
    )
    classifier_path = root / "scripts/fault_characterization/stage5_signature_classifier.py"
    classification_path = (
        batch_root / "signature_recovery" / "classifications" / f"{fault_id}.json"
    )
    write_json(classification_path, classification)
    status = run_command(
        [
            sys.executable,
            str(classifier_path),
            "--policy",
            str(policy),
            "repair-result",
            "--result-json",
            str(paths["result_json"]),
            "--result-env",
            str(paths["result_env"]),
            "--result-text",
            str(paths["result_text"]),
            "--classification",
            str(classification_path),
        ]
    )
    if status != 0:
        raise DriverError(f"result repair failed for {fault_id}")
    repaired = load_json(paths["result_json"], "repaired Native result")
    if repaired.get("status") != "EXISTING_ASSERTION_DETECTED":
        raise DriverError(f"repaired Native result has wrong status for {fault_id}")
    update_status_for_resume(paths["status"], fault_id, classification)
    return archive


def run_one(root: Path, batch_root: Path, fault_id: str, maxcycles: int) -> int:
    engine = root / "scripts/fault_characterization/stage5_batch.py"
    return run_command(
        [
            sys.executable,
            str(engine),
            "--root",
            str(root),
            "--pilot-root",
            str(batch_root),
            "run-one",
            "--fault-id",
            fault_id,
            "--maxcycles",
            str(maxcycles),
        ]
    )


def status_summary(root: Path, batch_root: Path) -> None:
    engine = root / "scripts/fault_characterization/stage5_batch.py"
    run_command(
        [
            sys.executable,
            str(engine),
            "--root",
            str(root),
            "--pilot-root",
            str(batch_root),
            "status",
        ]
    )
    run_command(
        [
            sys.executable,
            str(engine),
            "--root",
            str(root),
            "--pilot-root",
            str(batch_root),
            "storage-report",
        ]
    )


def verify_batch(batch_root: Path) -> dict[str, Any]:
    records = manifest_records(batch_root)
    counts: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    for record in records:
        paths = fault_paths(record)
        status = load_json(paths["status"], "fault status")
        state = str(status.get("state", "UNKNOWN"))
        counts[state] += 1
        rows.append(
            {
                "fault_id": record.get("fault_id"),
                "state": state,
                "native_status": status.get("native_status"),
                "route": status.get("route"),
                "validated_capability": status.get("validated_capability"),
                "failure_reason": status.get("failure_reason"),
            }
        )
    report = {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "generated_at_utc": utc_now(),
        "kind": "stage5_signature_batch_verification",
        "batch_root": str(batch_root),
        "fault_count": len(records),
        "counts": dict(counts),
        "faults": rows,
        "status": "PASS" if counts == Counter({COMPLETED: len(records)}) else "FAIL",
    }
    output = batch_root / "signature_recovery" / "verification.json"
    write_json(output, report)
    return report


def unique_signature_report(classifications: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[str]] = defaultdict(list)
    classes: Counter[str] = Counter()
    for item in classifications:
        key = str(item.get("dedupe_key", "MISSING"))
        groups[key].append(str(item.get("fault_id")))
        classes[str(item.get("semantic_class", "UNKNOWN"))] += 1
    return {
        "unique_signature_count": len(groups),
        "semantic_classes": dict(classes),
        "signature_groups": [
            {"dedupe_key": key, "fault_ids": values}
            for key, values in sorted(groups.items())
        ],
    }


def command_classify_existing(args: argparse.Namespace) -> int:
    batch_root = args.batch_root.resolve()
    root = args.root.resolve()
    policy = args.policy.resolve()
    report_dir = batch_root / "signature_recovery" / "classifications"
    classifications: list[dict[str, Any]] = []
    for record in manifest_records(batch_root):
        if current_state(record) != "FAILED":
            continue
        classifications.append(
            classify_one(
                root=root,
                policy=policy,
                record=record,
                report_dir=report_dir,
            )
        )
    if not classifications:
        raise DriverError("no FAILED faults were found")
    summary = {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "generated_at_utc": utc_now(),
        "kind": "stage5_signature_discovery",
        "batch_root": str(batch_root),
        "classifications": classifications,
        **unique_signature_report(classifications),
    }
    output = batch_root / "signature_recovery" / "classification_summary.json"
    write_json(output, summary)
    print()
    print("======================================================================")
    print("Stage5 signature discovery complete")
    print("======================================================================")
    print(f"Failed faults       : {len(classifications)}")
    print(f"Unique signatures   : {summary['unique_signature_count']}")
    print(f"Semantic classes    : {summary['semantic_classes']}")
    print(f"Summary             : {output}")
    return 0


def recover_record(
    *,
    args: argparse.Namespace,
    record: Mapping[str, Any],
    classification: Mapping[str, Any] | None = None,
) -> int:
    root = args.root.resolve()
    batch_root = args.batch_root.resolve()
    policy = args.policy.resolve()
    fault_id = str(record["fault_id"])
    if classification is None:
        classification = classify_one(
            root=root,
            policy=policy,
            record=record,
            report_dir=batch_root / "signature_recovery" / "classifications",
        )
    semantic = str(classification.get("semantic_class"))
    if semantic != "REGISTERED_DETECTOR_TERMINATION":
        print()
        print("======================================================================")
        print("Stage5 signature driver: FAIL-CLOSED")
        print("======================================================================")
        print(f"Fault ID        : {fault_id}")
        print(f"Semantic class  : {semantic}")
        print(f"Dedupe key      : {classification.get('dedupe_key')}")
        print("Action          : STOP; preserve evidence and review the new signature")
        return 2

    archive = repair_registered_result(
        root=root,
        policy=policy,
        batch_root=batch_root,
        record=record,
        classification=classification,
    )
    print(f"Archived old result state: {archive}")
    resumed = run_one(root, batch_root, fault_id, args.maxcycles)
    if resumed != 0:
        return 2
    final_state = current_state(record)
    if final_state != COMPLETED:
        print(f"ERROR: {fault_id} ended in {final_state}", file=sys.stderr)
        return 2
    return 0


def command_recover_existing(args: argparse.Namespace) -> int:
    batch_root = args.batch_root.resolve()
    records = [
        record for record in manifest_records(batch_root) if current_state(record) == "FAILED"
    ]
    if not records:
        raise DriverError("no FAILED faults were found")
    classifications = [
        classify_one(
            root=args.root.resolve(),
            policy=args.policy.resolve(),
            record=record,
            report_dir=batch_root / "signature_recovery" / "classifications",
        )
        for record in records
    ]
    unknown = [
        item
        for item in classifications
        if item.get("semantic_class") != "REGISTERED_DETECTOR_TERMINATION"
    ]
    if unknown:
        summary = {
            "schema_version": SCHEMA_VERSION,
            "generated_at_utc": utc_now(),
            "status": "FAIL_CLOSED",
            "classifications": classifications,
            **unique_signature_report(classifications),
        }
        write_json(
            batch_root / "signature_recovery" / "recovery_blocked.json", summary
        )
        print("ERROR: at least one FAILED fault has an unapproved signature", file=sys.stderr)
        return 2

    failures = 0
    by_fault = {str(item["fault_id"]): item for item in classifications}
    for record in records:
        if recover_record(args=args, record=record, classification=by_fault[str(record["fault_id"])]) != 0:
            failures += 1
            break
    status_summary(args.root.resolve(), batch_root)
    report = verify_batch(batch_root)
    print()
    print("======================================================================")
    print("Stage5 signature recovery verification")
    print("======================================================================")
    print(f"Final states : {report['counts']}")
    print(f"Verdict      : {report['status']}")
    return 0 if failures == 0 and report["status"] == "PASS" else 2


def command_run_batch(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    batch_root = args.batch_root.resolve()
    for record in manifest_records(batch_root):
        fault_id = str(record["fault_id"])
        state = current_state(record)
        if state == COMPLETED:
            print(f"Skip completed fault: {fault_id}")
            continue
        if state in BLOCKED_STATES:
            print(f"ERROR: fail-closed state already present: {fault_id} {state}")
            return 2

        status = run_one(root, batch_root, fault_id, args.maxcycles)
        state = current_state(record)
        if status == 0 and state == COMPLETED:
            continue
        if state != "FAILED":
            print(f"ERROR: {fault_id} ended in non-recoverable state {state}")
            return 2
        if recover_record(args=args, record=record) != 0:
            return 2

    status_summary(root, batch_root)
    report = verify_batch(batch_root)
    print(f"Batch signature-driver verdict: {report['status']}")
    return 0 if report["status"] == "PASS" else 2


def command_verify(args: argparse.Namespace) -> int:
    report = verify_batch(args.batch_root.resolve())
    print("Stage5 signature batch verification")
    print("===================================")
    print(f"Fault count  : {report['fault_count']}")
    print(f"Final states : {report['counts']}")
    print(f"Verdict      : {report['status']}")
    return 0 if report["status"] == "PASS" else 2


def build_parser() -> argparse.ArgumentParser:
    root_default = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root_default)
    parser.add_argument("--batch-root", type=Path, required=True)
    parser.add_argument(
        "--policy",
        type=Path,
        default=root_default / "platform/cv32e40p/stage5_assertion_policy_v1.json",
    )
    parser.add_argument("--maxcycles", type=int, default=2_000_000)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("classify-existing").set_defaults(func=command_classify_existing)
    sub.add_parser("recover-existing").set_defaults(func=command_recover_existing)
    sub.add_parser("run-batch").set_defaults(func=command_run_batch)
    sub.add_parser("verify").set_defaults(func=command_verify)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except DriverError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
