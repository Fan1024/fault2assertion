#!/usr/bin/env python3
"""Run the complete Fault2Assertion Stage-5 campaign safely and resumably.

The campaign is intentionally sequential.  One comprehensive golden compact
trace is cached while faults remain.  Each fault is simulated, analyzed into a
durable oracle, and then its temporary run directory is deleted.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

CAMPAIGN_STAGE = "stage_05_fault_characterization_campaign"
ORACLE_STAGE = "stage_05_diagnostic_oracle"
CACHE_STAGE = "stage_05_golden_trace_cache"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"expected a JSON object: {path}")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def run(
    command: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    allow_failure: bool = False,
) -> int:
    print("+", shlex.join(command), flush=True)
    completed = subprocess.run(command, env=env, check=False)
    if completed.returncode != 0 and not allow_failure:
        raise RuntimeError(
            "command failed with exit status "
            f"{completed.returncode}: {shlex.join(command)}"
        )
    return completed.returncode


def append_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def oracle_is_current(path: Path, fault_spec_digest: str) -> bool:
    if not path.is_file():
        return False
    try:
        payload = load_json(path)
    except Exception:
        return False
    return (
        payload.get("stage") == ORACLE_STAGE
        and payload.get("fault_spec_digest_sha256") == fault_spec_digest
    )


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def campaign_lock(run_root: Path) -> Iterator[None]:
    """Prevent two Stage-5 drivers from using the same scratch root."""

    lock_path = run_root / "stage5_campaign.lock"
    run_root.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            try:
                current = load_json(lock_path)
                pid = int(current.get("pid", -1))
            except Exception:
                pid = -1
            if process_is_alive(pid):
                raise RuntimeError(
                    f"another Stage-5 campaign is using {run_root}; "
                    f"lock PID={pid}"
                )
            lock_path.unlink(missing_ok=True)
            continue
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "pid": os.getpid(),
                        "created_at_utc": utc_now(),
                        "run_root": str(run_root),
                    },
                    handle,
                    indent=2,
                )
                handle.write("\n")
            break
    try:
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def expected_selection_ids(campaign: dict[str, Any]) -> list[str]:
    values = [str(item["selection_id"]) for item in campaign["selected_sites"]]
    if not values or len(set(values)) != len(values):
        raise RuntimeError("campaign selected-site IDs are empty or duplicated")
    return values


def golden_cache_is_current(
    campaign: dict[str, Any],
    cache_dir: Path,
) -> bool:
    manifest_path = cache_dir / "stage_05_golden_cache_manifest.json"
    if not manifest_path.is_file():
        return False
    try:
        manifest = load_json(manifest_path)
    except Exception:
        return False
    selection_ids = expected_selection_ids(campaign)
    if manifest.get("stage") != CACHE_STAGE:
        return False
    if manifest.get("campaign_digest_sha256") != campaign.get(
        "campaign_digest_sha256"
    ):
        return False
    if manifest.get("selection_ids") != selection_ids:
        return False
    for selection_id in selection_ids:
        path = cache_dir / f"{selection_id}.trace.tsv.gz"
        if not path.is_file() or path.stat().st_size == 0:
            return False
    return True


def build_golden_cache(
    *,
    campaign_path: Path,
    campaign: dict[str, Any],
    tool: Path,
    golden_runner: Path,
    golden_root: Path,
    golden_cache: Path,
    state_path: Path,
    environment: dict[str, str],
) -> None:
    shutil.rmtree(golden_root, ignore_errors=True)
    shutil.rmtree(golden_cache, ignore_errors=True)
    golden_root.mkdir(parents=True)
    golden_cache.mkdir(parents=True)

    raw_trace = golden_root / "golden_all.trace.tsv"
    monitor = golden_root / "stage5_golden_monitor.sv"
    monitor_manifest = golden_root / "stage5_golden_monitor_manifest.json"
    split_manifest = golden_cache / "stage5_golden_split_manifest.json"
    cache_manifest = golden_cache / "stage_05_golden_cache_manifest.json"
    raw_trace.write_text("", encoding="utf-8")

    run(
        [
            sys.executable,
            str(tool),
            "make-golden-monitor",
            "--campaign",
            str(campaign_path),
            "--trace-output",
            str(raw_trace),
            "--output",
            str(monitor),
            "--manifest",
            str(monitor_manifest),
            "--force",
        ]
    )

    golden_run_dir = golden_root / "xrun"
    run(
        [str(golden_runner), str(monitor), str(golden_run_dir)],
        env=environment,
    )
    result_path = golden_run_dir / "result.txt"
    result = (
        result_path.read_text(encoding="utf-8").strip()
        if result_path.is_file()
        else "MISSING"
    )
    if result != "PASS":
        raise RuntimeError(f"Stage-5 golden simulation did not PASS: {result}")
    if not raw_trace.is_file() or raw_trace.stat().st_size == 0:
        raise RuntimeError(f"golden compact trace missing or empty: {raw_trace}")

    run(
        [
            sys.executable,
            str(tool),
            "split-golden-trace",
            "--trace",
            str(raw_trace),
            "--output-dir",
            str(golden_cache),
            "--manifest",
            str(split_manifest),
            "--delete-source",
            "--force",
        ]
    )

    selection_ids = expected_selection_ids(campaign)
    missing = [
        selection_id
        for selection_id in selection_ids
        if not (golden_cache / f"{selection_id}.trace.tsv.gz").is_file()
    ]
    if missing:
        raise RuntimeError(
            "golden trace split did not produce every selected site; "
            f"missing={missing[:20]}"
        )

    write_json(
        cache_manifest,
        {
            "schema_version": "1.0",
            "stage": CACHE_STAGE,
            "generated_at_utc": utc_now(),
            "campaign": str(campaign_path),
            "campaign_digest_sha256": campaign["campaign_digest_sha256"],
            "selection_count": len(selection_ids),
            "selection_ids": selection_ids,
            "storage_policy": "temporary; deleted after all current oracles exist",
        },
    )
    shutil.rmtree(golden_run_dir, ignore_errors=True)
    append_state(
        state_path,
        {
            "time": utc_now(),
            "event": "golden_complete",
            "selection_trace_count": len(selection_ids),
            "campaign_digest_sha256": campaign["campaign_digest_sha256"],
        },
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Stage-5 fault characterization simulations and oracles"
    )
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument(
        "--run-root",
        type=Path,
        required=True,
        help="scratch root; keep it outside durable Stage-5 artifacts",
    )
    parser.add_argument("--maxcycles", type=int, default=2_000_000)
    parser.add_argument("--start-index", type=int, default=1)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--fault-id")
    parser.add_argument("--force-oracles", action="store_true")
    parser.add_argument("--keep-golden-cache", action="store_true")
    parser.add_argument("--keep-failed-run", action="store_true")
    parser.add_argument("--skip-golden", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.maxcycles <= 0:
        raise SystemExit("ERROR: --maxcycles must be positive")
    if args.start_index <= 0:
        raise SystemExit("ERROR: --start-index is 1-based and must be positive")
    if args.limit is not None and args.limit <= 0:
        raise SystemExit("ERROR: --limit must be positive")

    script_dir = Path(__file__).resolve().parent
    root = script_dir.parent
    tool = root / "scripts/fault_characterization/stage5_faults.py"
    golden_runner = root / "scripts/run_xrun_stage5_golden.sh"
    fault_runner = root / "scripts/run_xrun_stage5_fault.sh"
    stage5_common = root / "scripts/lib/xrun_stage5_common.sh"
    for path in (tool, golden_runner, fault_runner, stage5_common):
        if not path.is_file():
            raise SystemExit(f"ERROR: required Stage-5 tool missing: {path}")

    campaign_path = args.campaign.resolve()
    campaign = load_json(campaign_path)
    if campaign.get("stage") != CAMPAIGN_STAGE:
        raise SystemExit("ERROR: invalid Stage-5 campaign marker")
    all_records = list(campaign.get("faults", []))
    if not all_records:
        raise SystemExit("ERROR: Stage-5 campaign contains no faults")

    campaign_root = campaign_path.parent
    oracle_dir = campaign_root / "oracles"
    report_dir = campaign_root / "reports"
    sva_dir = campaign_root / "sva_seeds"
    summary_dir = campaign_root / "summary"
    for path in (oracle_dir, report_dir, sva_dir, summary_dir):
        path.mkdir(parents=True, exist_ok=True)

    selected_records = all_records
    if args.fault_id:
        selected_records = [
            item for item in all_records if item["fault_id"] == args.fault_id
        ]
        if not selected_records:
            raise SystemExit(f"ERROR: fault not in campaign: {args.fault_id}")
    else:
        selected_records = selected_records[args.start_index - 1 :]
        if args.limit is not None:
            selected_records = selected_records[: args.limit]

    pending_records = [
        item
        for item in selected_records
        if args.force_oracles
        or not oracle_is_current(
            oracle_dir / f"{item['fault_id']}.json",
            str(item["fault_spec_digest_sha256"]),
        )
    ]

    run_root = args.run_root.resolve()
    golden_root = run_root / "golden"
    golden_cache = run_root / "golden_cache"
    fault_tmp_root = run_root / "faults"
    state_path = run_root / "campaign_state.jsonl"
    run_root.mkdir(parents=True, exist_ok=True)
    fault_tmp_root.mkdir(parents=True, exist_ok=True)

    environment = os.environ.copy()
    environment["MAXCYCLES"] = str(args.maxcycles)
    environment["VCD"] = "0"
    environment["KEEP_WORK"] = "0"

    completed = 0
    skipped = len(selected_records) - len(pending_records)
    failed = 0

    with campaign_lock(run_root):
        if pending_records:
            cache_current = golden_cache_is_current(campaign, golden_cache)
            if args.skip_golden and not cache_current:
                raise RuntimeError(
                    "--skip-golden was requested, but the golden cache is "
                    "missing, incomplete, or from another campaign digest"
                )
            if not cache_current:
                build_golden_cache(
                    campaign_path=campaign_path,
                    campaign=campaign,
                    tool=tool,
                    golden_runner=golden_runner,
                    golden_root=golden_root,
                    golden_cache=golden_cache,
                    state_path=state_path,
                    environment=environment,
                )

        for ordinal, record in enumerate(selected_records, start=1):
            fault_id = str(record["fault_id"])
            selection_id = str(record["selection_id"])
            fault_json = Path(str(record["fault_spec"])).resolve()
            oracle_path = oracle_dir / f"{fault_id}.json"
            report_path = report_dir / f"{fault_id}.txt"
            sva_path = sva_dir / f"{fault_id}.sva"

            if record not in pending_records:
                print(
                    f"[{ordinal}/{len(selected_records)}] SKIP {fault_id}: "
                    "current oracle exists"
                )
                continue

            work = fault_tmp_root / fault_id
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True)
            trace = work / f"{fault_id}.trace.tsv"
            monitor = work / f"{fault_id}_monitor.sv"
            monitor_manifest = work / f"{fault_id}_monitor_manifest.json"
            xrun_dir = work / "xrun"
            trace.write_text("", encoding="utf-8")

            print(
                f"[{ordinal}/{len(selected_records)}] RUN  {fault_id}",
                flush=True,
            )
            append_state(
                state_path,
                {"time": utc_now(), "event": "fault_start", "fault_id": fault_id},
            )

            try:
                run(
                    [
                        sys.executable,
                        str(tool),
                        "make-fault-monitor",
                        "--fault-json",
                        str(fault_json),
                        "--trace-output",
                        str(trace),
                        "--output",
                        str(monitor),
                        "--manifest",
                        str(monitor_manifest),
                        "--force",
                    ]
                )
                runner_status = run(
                    [str(fault_runner), str(fault_json), str(monitor), str(xrun_dir)],
                    env=environment,
                    allow_failure=True,
                )
                golden_trace = golden_cache / f"{selection_id}.trace.tsv.gz"
                if not golden_trace.is_file() or golden_trace.stat().st_size == 0:
                    raise RuntimeError(f"golden trace missing for {selection_id}")
                if not trace.is_file() or trace.stat().st_size == 0:
                    raise RuntimeError(f"fault compact trace missing/empty: {trace}")

                run(
                    [
                        sys.executable,
                        str(tool),
                        "analyze",
                        "--fault-json",
                        str(fault_json),
                        "--golden-trace",
                        str(golden_trace),
                        "--fault-trace",
                        str(trace),
                        "--result",
                        str(xrun_dir / "result.txt"),
                        "--xrun-log",
                        str(xrun_dir / "xrun.log"),
                        "--oracle-output",
                        str(oracle_path),
                        "--report-output",
                        str(report_path),
                        "--sva-output",
                        str(sva_path),
                        "--force",
                    ]
                )
                oracle = load_json(oracle_path)
                if oracle.get("fault_spec_digest_sha256") != record.get(
                    "fault_spec_digest_sha256"
                ):
                    raise RuntimeError(
                        f"generated oracle digest reference mismatch: {fault_id}"
                    )
                completed += 1
                append_state(
                    state_path,
                    {
                        "time": utc_now(),
                        "event": "fault_complete",
                        "fault_id": fault_id,
                        "runner_status": runner_status,
                        "characterization": oracle["characterization_class"],
                        "functional_result": oracle["functional_result"][
                            "classification"
                        ],
                        "oracle_digest_sha256": oracle["oracle_digest_sha256"],
                    },
                )
                shutil.rmtree(work, ignore_errors=True)
            except Exception as exc:
                failed += 1
                append_state(
                    state_path,
                    {
                        "time": utc_now(),
                        "event": "fault_failed",
                        "fault_id": fault_id,
                        "error": str(exc),
                    },
                )
                print(f"ERROR: {fault_id}: {exc}", file=sys.stderr, flush=True)
                if not args.keep_failed_run:
                    shutil.rmtree(work, ignore_errors=True)

        aggregate_status = run(
            [
                sys.executable,
                str(tool),
                "aggregate",
                "--campaign",
                str(campaign_path),
                "--oracle-dir",
                str(oracle_dir),
                "--output-dir",
                str(summary_dir),
                "--force",
            ],
            allow_failure=True,
        )

        current_count = sum(
            oracle_is_current(
                oracle_dir / f"{item['fault_id']}.json",
                str(item["fault_spec_digest_sha256"]),
            )
            for item in all_records
        )
        campaign_complete = current_count == len(all_records)
        if campaign_complete and not args.keep_golden_cache:
            shutil.rmtree(golden_cache, ignore_errors=True)
            shutil.rmtree(golden_root, ignore_errors=True)
            print("Removed comprehensive golden trace cache after completion.")

    partial_invocation = bool(
        args.fault_id or args.limit is not None or args.start_index != 1
    )
    print()
    print("=" * 72)
    print("Stage-5 campaign invocation complete")
    print("=" * 72)
    print(f"Selected this invocation  : {len(selected_records)}")
    print(f"Completed this invocation : {completed}")
    print(f"Skipped current oracles   : {skipped}")
    print(f"Failed this invocation    : {failed}")
    print(f"Total current oracles     : {current_count}/{len(all_records)}")
    print(f"Campaign complete         : {campaign_complete}")
    print(f"Aggregate status          : {aggregate_status}")
    print(f"Summary                   : {summary_dir / 'stage_05_report.txt'}")
    print("Retained VCD files        : 0")
    print("Retained faulty netlists  : 0")

    if failed:
        return 2
    if campaign_complete or partial_invocation:
        return 0
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
