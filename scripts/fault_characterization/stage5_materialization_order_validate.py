#!/usr/bin/env python3
"""End-to-end static validation of Stage-5 v1.0.8 materialization order.

For every fault in a mini campaign, this validator:

1. invokes the public ``stage5_faults.py apply`` CLI;
2. checks that the temporary wire declaration is the first token occurrence;
3. runs the CV32E40P simulation-netlist preparation script;
4. repeats the same declaration/use/assignment validation on the prepared copy;
5. confirms the immutable golden mapped-netlist SHA never changes;
6. removes all temporary netlists only after every fault passes.

No simulator is invoked.  On failure the scratch directory is retained.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


class ValidationError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid {label} JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError(f"{label} must contain one JSON object")
    return payload


def import_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValidationError(f"cannot import Python module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def run_checked(command: list[str], log_path: Path) -> None:
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(completed.stdout, encoding="utf-8")
    if completed.returncode != 0:
        raise ValidationError(
            f"command failed with status {completed.returncode}: "
            f"{' '.join(command)}\nlog: {log_path}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate declaration-before-use on all mini Stage-5 faults."
    )
    parser.add_argument("--stage5-tool", type=Path, required=True)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--prepare-netlist", type=Path, required=True)
    parser.add_argument("--scratch-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    stage5_tool = args.stage5_tool.resolve()
    campaign_path = args.campaign.resolve()
    prepare_netlist = args.prepare_netlist.resolve()
    scratch_root = args.scratch_root.resolve()
    report_path = args.report.resolve()

    for path, label in (
        (stage5_tool, "Stage-5 tool"),
        (campaign_path, "mini campaign"),
        (prepare_netlist, "prepare_netlist.py"),
    ):
        if not path.is_file() or path.stat().st_size == 0:
            print(f"ERROR: {label} missing or empty: {path}", file=sys.stderr)
            return 1

    if scratch_root.exists():
        print(
            "ERROR: materialization validation scratch already exists; "
            f"preserve or remove it intentionally: {scratch_root}",
            file=sys.stderr,
        )
        return 1

    scratch_root.mkdir(parents=True)
    records: list[dict[str, Any]] = []
    errors: list[str] = []

    try:
        tool = import_module(stage5_tool, "f2a_stage5_order_validator_target")
        campaign = load_json(campaign_path, "mini campaign")
        if campaign.get("stage") != tool.STAGE5_CAMPAIGN_MARKER:
            raise ValidationError("campaign stage marker mismatch")
        if str(campaign.get("program_version")) != str(tool.PROGRAM_VERSION):
            raise ValidationError(
                "campaign/tool version mismatch: "
                f"campaign={campaign.get('program_version')!r}, "
                f"tool={tool.PROGRAM_VERSION!r}"
            )
        if str(tool.PROGRAM_VERSION) != "1.0.8":
            raise ValidationError(
                f"declaration-order validator requires Stage-5 1.0.8, got "
                f"{tool.PROGRAM_VERSION!r}"
            )

        faults = campaign.get("faults")
        if not isinstance(faults, list) or not faults:
            raise ValidationError("campaign contains no faults")

        golden_path = Path(str(campaign["mapped_netlist"]["path"])).resolve()
        golden_expected_sha = str(campaign["mapped_netlist"]["sha256"])
        if not golden_path.is_file():
            raise ValidationError(f"golden mapped netlist missing: {golden_path}")
        if sha256_file(golden_path) != golden_expected_sha:
            raise ValidationError("golden mapped-netlist SHA mismatch before validation")

        for item in sorted(faults, key=lambda value: str(value["fault_id"])):
            fault_id = str(item["fault_id"])
            spec_path = Path(str(item["fault_spec"])).resolve()
            spec = load_json(spec_path, f"fault spec {fault_id}")
            if str(spec.get("program_version")) != "1.0.8":
                raise ValidationError(
                    f"fault spec is not v1.0.8: {fault_id}: "
                    f"{spec.get('program_version')!r}"
                )
            modification = spec.get("modification")
            if not isinstance(modification, dict):
                raise ValidationError(f"missing modification metadata: {fault_id}")
            if modification.get("materialization_layout_version") != (
                tool.MATERIALIZATION_LAYOUT_VERSION
            ):
                raise ValidationError(
                    f"layout-version mismatch: {fault_id}: "
                    f"{modification.get('materialization_layout_version')!r}"
                )

            fault_root = scratch_root / fault_id
            fault_root.mkdir()
            raw_netlist = fault_root / "fault_netlist.v"
            sim_netlist = fault_root / "cv32e40p.mapped.sim.v"
            apply_log = fault_root / "apply.log"
            prepare_log = fault_root / "prepare_netlist.log"

            run_checked(
                [
                    sys.executable,
                    str(stage5_tool),
                    "apply",
                    "--fault-json",
                    str(spec_path),
                    "--output-netlist",
                    str(raw_netlist),
                ],
                apply_log,
            )

            raw_text = raw_netlist.read_text(encoding="utf-8", errors="strict")
            raw_facts = tool.validate_materialized_netlist_text(
                raw_text,
                module_name=str(spec["site"]["module"]),
                source_net=str(spec["site"]["source_net"]),
                stuck_at=int(spec["stuck_at"]),
                temporary_net=str(modification["temporary_pre_fault_net"]),
                fault_id=fault_id,
            )

            run_checked(
                [
                    sys.executable,
                    str(prepare_netlist),
                    str(raw_netlist),
                    str(sim_netlist),
                ],
                prepare_log,
            )

            sim_text = sim_netlist.read_text(encoding="utf-8", errors="strict")
            sim_facts = tool.validate_materialized_netlist_text(
                sim_text,
                module_name=str(spec["site"]["module"]),
                source_net=str(spec["site"]["source_net"]),
                stuck_at=int(spec["stuck_at"]),
                temporary_net=str(modification["temporary_pre_fault_net"]),
                fault_id=fault_id,
            )

            current_golden_sha = sha256_file(golden_path)
            if current_golden_sha != golden_expected_sha:
                raise ValidationError(
                    f"immutable golden netlist changed while checking {fault_id}"
                )

            raw_sha = sha256_file(raw_netlist)
            sim_sha = sha256_file(sim_netlist)
            if raw_sha == golden_expected_sha:
                raise ValidationError(
                    f"fault netlist unexpectedly equals golden netlist: {fault_id}"
                )

            records.append(
                {
                    "fault_id": fault_id,
                    "fault_spec": str(spec_path),
                    "fault_spec_sha256": sha256_file(spec_path),
                    "raw_fault_netlist_sha256": raw_sha,
                    "prepared_fault_netlist_sha256": sim_sha,
                    "raw_layout": raw_facts,
                    "prepared_layout": sim_facts,
                    "apply_log": str(apply_log),
                    "prepare_log": str(prepare_log),
                }
            )

        if len(records) != len(faults):
            raise ValidationError(
                f"validated fault count mismatch: {len(records)} != {len(faults)}"
            )

    except Exception as exc:
        errors.append(str(exc))

    status = "PASS" if not errors else "FAIL"
    report = {
        "schema_version": "1.0",
        "generated_at_utc": utc_now(),
        "kind": "stage5_materialization_declaration_order_validation",
        "status": status,
        "stage5_tool": str(stage5_tool),
        "stage5_tool_sha256": sha256_file(stage5_tool),
        "campaign": str(campaign_path),
        "campaign_sha256": sha256_file(campaign_path),
        "prepare_netlist": str(prepare_netlist),
        "prepare_netlist_sha256": sha256_file(prepare_netlist),
        "scratch_root": str(scratch_root),
        "validated_fault_count": len(records),
        "records": records,
        "errors": errors,
        "scratch_retained": bool(errors),
    }
    atomic_write_json(report_path, report)

    print("=" * 78)
    print("Stage-5 v1.0.8 Materialization Order Validation")
    print("=" * 78)
    print(f"Campaign              : {campaign_path}")
    print(f"Validated faults      : {len(records)}")
    print(f"Errors                : {len(errors)}")
    print(f"Result                : {status}")
    print(f"Report                : {report_path}")

    if errors:
        print(f"Scratch retained      : {scratch_root}")
        for error in errors:
            print(f"  - {error}")
        return 1

    # Successful validation is static and reproducible from the report and
    # source artifacts; no run-local netlist should remain after Gate 1.
    shutil.rmtree(scratch_root)

    report["scratch_retained"] = False
    report["scratch_removed_after_pass"] = True
    atomic_write_json(report_path, report)
    print("Temporary netlists     : removed after PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
