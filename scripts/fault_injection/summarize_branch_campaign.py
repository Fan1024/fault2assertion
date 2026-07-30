#!/usr/bin/env python3
"""Summarize one VCD=0 branch-fault screening pass."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--workload", default="crc32")
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"ERROR: expected JSON object: {path}")
    return payload


def read_first_line(path: Path) -> str | None:
    if not path.is_file() or path.stat().st_size == 0:
        return None
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        return stream.readline().strip()


def read_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in raw:
            continue
        key, value = raw.split("=", maxsplit=1)
        result[key.strip()] = value.strip()
    return result


def decision_for(result: str) -> str:
    return {
        "OUTPUT_MISMATCH": "KEEP_FINAL_OUTPUT_FAILURE",
        "TIMEOUT": "KEEP_TIMEOUT_FOR_REVIEW",
        "OUTPUT_MATCH": "LOCAL_PROBE_REQUIRED",
        "PASS": "LOCAL_PROBE_REQUIRED",
        "ERROR": "INFRASTRUCTURE_REVIEW",
        "UNKNOWN": "INFRASTRUCTURE_REVIEW",
        "RUNNER_ERROR": "INFRASTRUCTURE_REVIEW",
        "INCOMPLETE": "PENDING",
        "NOT_RUN": "PENDING",
    }.get(result, "REVIEW")


def flatten_selection(selection: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    ordinal = 0

    for location in selection.get("selected_locations", []):
        if not isinstance(location, dict):
            continue
        for fault in location.get("faults", []):
            if not isinstance(fault, dict):
                continue
            ordinal += 1
            rows.append(
                {
                    "ordinal": ordinal,
                    "fault_id": str(fault["fault_id"]),
                    "paired_fault_id": str(fault.get("paired_fault_id", "")),
                    "stuck_at": int(fault["stuck_at"]),
                    "location_id": str(location.get("location_id", "")),
                    "site_id": str(location.get("site_id", "")),
                    "site_key": str(location.get("site_key", "")),
                    "region": str(location.get("region", "")),
                    "source_net": str(location.get("source_net", "")),
                    "source_class": str(location.get("source_class", "")),
                    "source_fanout": int(location.get("source_fanout", 0)),
                    "fanout_bucket": str(location.get("fanout_bucket", "")),
                    "sink_instance": str(location.get("sink_instance", "")),
                    "sink_cell_type": str(location.get("sink_cell_type", "")),
                    "sink_pin": str(location.get("sink_pin", "")),
                    "sink_role": str(location.get("sink_role", "")),
                    "stratum": str(location.get("stratum", "")),
                }
            )
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else []
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        if fieldnames:
            writer.writeheader()
            writer.writerows(rows)


def main() -> int:
    args = parse_args()
    campaign_root = args.campaign_root.resolve()
    selection_path = campaign_root / "selection.json"

    if not selection_path.is_file():
        raise SystemExit(f"ERROR: selection.json not found: {selection_path}")

    selection = read_json(selection_path)
    base_rows = flatten_selection(selection)
    if not base_rows:
        raise SystemExit("ERROR: no selected fault instances found")

    result_rows: list[dict[str, Any]] = []

    for base in base_rows:
        fault_id = base["fault_id"]
        run_dir = (
            campaign_root
            / fault_id
            / "results"
            / args.workload
            / args.run_name
        )
        result_path = run_dir / "result.txt"

        if result_path.is_file():
            result = read_first_line(result_path) or "UNKNOWN"
        elif run_dir.exists():
            result = "INCOMPLETE"
        else:
            result = "NOT_RUN"

        env = read_env(run_dir / "result.env")
        result_rows.append(
            {
                **base,
                "result": result,
                "screening_decision": decision_for(result),
                "xrun_exit_status": env.get("xrun_exit_status", ""),
                "run_dir": str(run_dir),
                "xrun_log": str(run_dir / "xrun.log"),
                "signature_present": (run_dir / "signature.txt").is_file(),
                "fault_json_present": (
                    campaign_root / fault_id / "fault.json"
                ).is_file(),
                "fault_patch_present": (
                    campaign_root / fault_id / "fault.patch"
                ).is_file(),
            }
        )

    by_result = Counter(row["result"] for row in result_rows)
    by_decision = Counter(row["screening_decision"] for row in result_rows)
    by_region_result: dict[str, Counter[str]] = defaultdict(Counter)
    for row in result_rows:
        by_region_result[row["region"]][row["result"]] += 1

    seed = (
        selection.get("sampling_policy", {}).get("random_seed")
        if isinstance(selection.get("sampling_policy"), dict)
        else None
    )

    payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "campaign_root": str(campaign_root),
        "selection_json": str(selection_path),
        "workload": args.workload,
        "run_name": args.run_name,
        "random_seed": seed,
        "expected_fault_instances": len(result_rows),
        "counts_by_result": dict(sorted(by_result.items())),
        "counts_by_screening_decision": dict(sorted(by_decision.items())),
        "counts_by_region_and_result": {
            region: dict(sorted(counter.items()))
            for region, counter in sorted(by_region_result.items())
        },
        "results": result_rows,
    }

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / "campaign_results.json"
    csv_path = output_dir / "campaign_results.csv"
    text_path = output_dir / "campaign_summary.txt"

    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    write_csv(csv_path, result_rows)

    lines = [
        "Branch fault VCD=0 screening summary",
        "=====================================",
        f"Campaign root: {campaign_root}",
        f"Workload: {args.workload}",
        f"Run name: {args.run_name}",
        f"Random seed: {seed}",
        f"Expected fault instances: {len(result_rows)}",
        "",
        "Counts by result:",
    ]
    for name, count in sorted(by_result.items()):
        lines.append(f"  {name:24s} {count:6d}")

    lines.extend(["", "Counts by screening decision:"])
    for name, count in sorted(by_decision.items()):
        lines.append(f"  {name:30s} {count:6d}")

    lines.extend(
        [
            "",
            "Interpretation:",
            "  OUTPUT_MISMATCH -> keep as final-output-detectable fault.",
            "  TIMEOUT         -> keep for review; verify golden completes under the same MAXCYCLES.",
            "  OUTPUT_MATCH    -> do not discard; run local probe next.",
            "  ERROR/UNKNOWN   -> infrastructure failure, not a scientific fault result.",
            "  NOT_RUN         -> pending.",
            "",
            f"JSON: {json_path}",
            f"CSV:  {csv_path}",
        ]
    )
    text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(text_path)
    print(json_path)
    print(csv_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
