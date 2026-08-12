#!/usr/bin/env python3
"""Run the existing Stage-6 batch to completion while auto-deferring one
explicitly recognized fault-local topology incompatibility.

Scientific policy is unchanged:
- the existing run_stage6_batch.py and run_stage6_fault.py remain authoritative;
- the downstream analyzer's strict topology-consistency guard remains enabled;
- only the exact error
    "Stage-6 downstream depth-1 reconstruction does not match Stage-5 direct receivers"
  is converted to a durable DEFERRED result;
- every other non-zero batch failure still stops immediately.

This wrapper may also recover the most recent already-stopped topology mismatch
from an existing batch log before restarting the inner batch.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import subprocess
import sys
import tempfile

from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FAULT_RE = re.compile(
    r"\bTF\d{6}_SA[01]\b"
)

TOPOLOGY_SIGNATURE = (
    "Stage-6 downstream depth-1 reconstruction does not match "
    "Stage-5 direct receivers"
)

DEFERRED_SCHEMA = (
    "stage6_deferred_result_v1"
)

DEFERRED_REASON = (
    "SCOPE_TOPOLOGY_MISMATCH"
)


class ResilientBatchError(
    RuntimeError
):
    pass


def utc_now() -> str:

    return datetime.now(
        timezone.utc
    ).isoformat()


def load_json(
    path: Path,
) -> dict[str, Any]:

    try:

        value = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

    except FileNotFoundError as exc:

        raise ResilientBatchError(
            f"missing JSON file: {path}"
        ) from exc

    except json.JSONDecodeError as exc:

        raise ResilientBatchError(
            f"invalid JSON file "
            f"{path}: {exc}"
        ) from exc

    if not isinstance(
        value,
        dict,
    ):

        raise ResilientBatchError(
            f"expected JSON object: {path}"
        )

    return value


def parse_list_after_label(
    block: str,
    label: str,
) -> list[str]:

    pattern = re.compile(
        rf"(?m)^\s*"
        rf"{re.escape(label)}"
        rf"\s*:\s*"
        rf"(\[[^\n]*\])"
        rf"\s*$"
    )

    match = pattern.search(
        block
    )

    if not match:
        return []

    raw = match.group(
        1
    )

    try:

        value = ast.literal_eval(
            raw
        )

    except (
        SyntaxError,
        ValueError,
    ):

        return []

    if (
        not isinstance(
            value,
            list,
        )
        or not all(
            isinstance(
                item,
                str,
            )
            for item in value
        )
    ):

        return []

    return value


def mismatch_records(
    text: str,
) -> list[dict[str, Any]]:

    records: list[
        dict[str, Any]
    ] = []

    start = 0

    while True:

        index = text.find(
            TOPOLOGY_SIGNATURE,
            start,
        )

        if index < 0:
            break

        prefix = text[
            :index
        ]

        fault_ids = (
            FAULT_RE.findall(
                prefix
            )
        )

        fault_id = (
            fault_ids[-1]
            if fault_ids
            else None
        )

        next_index = text.find(
            TOPOLOGY_SIGNATURE,
            index
            + len(
                TOPOLOGY_SIGNATURE
            ),
        )

        end = (
            next_index
            if next_index >= 0
            else len(text)
        )

        block = text[
            index:end
        ]

        stage5 = (
            parse_list_after_label(
                block,
                "Stage-5",
            )
        )

        rebuilt = (
            parse_list_after_label(
                block,
                "rebuilt",
            )
        )

        if fault_id is not None:

            records.append(
                {
                    "fault_id":
                        fault_id,

                    "stage5_direct_receivers":
                        stage5,

                    "rebuilt_depth1_receivers":
                        rebuilt,

                    "evidence_block":
                        block.strip(),
                }
            )

        start = (
            index
            + len(
                TOPOLOGY_SIGNATURE
            )
        )

    return records


def result_state(
    result_file: Path,
) -> str | None:

    if not result_file.is_file():
        return None

    data = load_json(
        result_file
    )

    if (
        data.get(
            "status"
        )
        == "DEFERRED"
    ):

        return "DEFERRED"

    if (
        data.get(
            "schema_version"
        )
        == "stage6_fault_result_v3"
    ):

        return "FINALIZED"

    raise ResilientBatchError(
        "existing result has "
        "unrecognized schema/status: "
        f"{result_file}"
    )


def copy_compact_artifacts(
    work_dir: Path,
    output_dir: Path,
) -> list[str]:

    # Only compact top-level
    # scientific/debug artifacts
    # are retained.
    #
    # No VCDs, xcelium work dirs,
    # reproduction bundles, or
    # scratch trees are copied.

    exact_names = {
        "manifest.json",
        "visible_context.json",
        "golden_behavior.json",
        "prompt.txt",
        "scope_diagnosis_error.log",
    }

    allowed_suffixes = (
        "_property.sva",
        "_simulation.json",
        "_generation_meta.json",
        "_model_context.json",
        "_context.json",
        "_api_status.json",
        "_response.txt",
        "_prompt.txt",
        "_scope_feedback.json",
    )

    retained: list[
        str
    ] = []

    for src in sorted(
        work_dir.iterdir()
    ):

        if not src.is_file():
            continue

        name = src.name

        if (
            name not in exact_names
            and not name.endswith(
                allowed_suffixes
            )
        ):

            continue

        # These files should be small.
        # Stop rather than accidentally
        # retaining a huge artifact.
        if (
            src.stat().st_size
            > 16 * 1024 * 1024
        ):

            raise ResilientBatchError(
                "refusing to retain "
                "unexpectedly large artifact: "
                f"{src}"
            )

        shutil.copy2(
            src,
            output_dir
            / name,
        )

        retained.append(
            name
        )

    return retained


def finalize_topology_deferred(
    *,
    root: Path,
    record: dict[str, Any],
) -> None:

    fault_id = str(
        record[
            "fault_id"
        ]
    )

    work_dir = (
        root
        / "runs"
        / "stage6"
        / "work"
        / fault_id
    )

    results_root = (
        root
        / "runs"
        / "stage6"
        / "results"
    )

    result_dir = (
        results_root
        / fault_id
    )

    result_file = (
        result_dir
        / "fault_result.json"
    )

    existing = result_state(
        result_file
    )

    if existing is not None:

        if (
            existing
            == "DEFERRED"
        ):

            print(
                "Topology mismatch "
                "already has durable "
                "DEFERRED result: "
                f"{fault_id}",
                flush=True,
            )

            if work_dir.is_dir():

                shutil.rmtree(
                    work_dir
                )

            return

        raise ResilientBatchError(
            "refusing to replace "
            "existing finalized result "
            f"for {fault_id}: "
            f"{result_file}"
        )

    if result_dir.exists():

        raise ResilientBatchError(
            "result directory exists "
            "without recognized "
            "fault_result.json: "
            f"{result_dir}"
        )

    if not work_dir.is_dir():

        raise ResilientBatchError(
            "recognized topology mismatch "
            "but fault work directory "
            "is missing: "
            f"{work_dir}"
        )

    stage5 = list(
        record.get(
            "stage5_direct_receivers"
        )
        or []
    )

    rebuilt = list(
        record.get(
            "rebuilt_depth1_receivers"
        )
        or []
    )

    missing = sorted(
        set(stage5)
        - set(rebuilt)
    )

    extra = sorted(
        set(rebuilt)
        - set(stage5)
    )

    results_root.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp_dir = Path(
        tempfile.mkdtemp(
            prefix=(
                ".tmp_deferred_"
                f"{fault_id}_"
            ),
            dir=results_root,
        )
    )

    try:

        retained = (
            copy_compact_artifacts(
                work_dir,
                tmp_dir,
            )
        )

        evidence_text = str(
            record.get(
                "evidence_block"
            )
            or ""
        ).strip()

        if evidence_text:

            (
                tmp_dir
                / "deferred_scope_error.txt"
            ).write_text(
                evidence_text
                + "\n",
                encoding="utf-8",
            )

            retained.append(
                "deferred_scope_error.txt"
            )

        payload = {
            "schema_version":
                DEFERRED_SCHEMA,

            "fault_id":
                fault_id,

            "status":
                "DEFERRED",

            "deferred": {
                "reason":
                    DEFERRED_REASON,

                "stage":
                    "OBSERVATION_SCOPE_DIAGNOSIS",

                "round":
                    0,

                "recorded_at_utc":
                    utc_now(),

                "detail":
                    (
                        "Stage-6 downstream "
                        "depth-1 reconstruction "
                        "did not match the "
                        "Stage-5 direct receiver "
                        "set. The strict topology "
                        "guard was preserved; "
                        "this fault is deferred "
                        "rather than counted as "
                        "an assertion-generation "
                        "failure."
                    ),

                "stage5_direct_receivers":
                    stage5,

                "rebuilt_depth1_receivers":
                    rebuilt,

                "missing_from_rebuild":
                    missing,

                "extra_in_rebuild":
                    extra,

                "error_signature":
                    TOPOLOGY_SIGNATURE,

                "retained_files":
                    sorted(
                        set(
                            retained
                        )
                    ),
            },
        }

        (
            tmp_dir
            / "fault_result.json"
        ).write_text(
            json.dumps(
                payload,
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        # Atomic durable commit.
        tmp_dir.replace(
            result_dir
        )

    except Exception:

        shutil.rmtree(
            tmp_dir,
            ignore_errors=True,
        )

        raise

    # Fault-local work is removed only
    # after the durable result exists.
    shutil.rmtree(
        work_dir,
        ignore_errors=True,
    )

    print()
    print(
        "=" * 88
    )

    print(
        "Stage-6 fault automatically deferred"
    )

    print(
        "=" * 88
    )

    print(
        f"Fault ID      : "
        f"{fault_id}"
    )

    print(
        f"Reason        : "
        f"{DEFERRED_REASON}"
    )

    print(
        f"Stage-5       : "
        f"{stage5}"
    )

    print(
        f"Rebuilt       : "
        f"{rebuilt}"
    )

    print(
        f"Missing       : "
        f"{missing}"
    )

    print(
        f"Result        : "
        f"{result_file}"
    )

    print(
        "Batch action  : "
        "restart inner batch and continue"
    )

    print(
        flush=True
    )


def recover_from_existing_log(
    root: Path,
    log_path: Path,
) -> bool:

    if not log_path.is_file():
        return False

    text = log_path.read_text(
        encoding="utf-8",
        errors="replace",
    )

    records = mismatch_records(
        text
    )

    # Work backwards from the newest
    # recorded mismatch.
    for record in reversed(
        records
    ):

        fault_id = str(
            record[
                "fault_id"
            ]
        )

        work_dir = (
            root
            / "runs"
            / "stage6"
            / "work"
            / fault_id
        )

        result_file = (
            root
            / "runs"
            / "stage6"
            / "results"
            / fault_id
            / "fault_result.json"
        )

        if (
            work_dir.is_dir()
            and not result_file.is_file()
        ):

            print(
                "Recovering previously "
                "stopped topology mismatch: "
                f"{fault_id}",
                flush=True,
            )

            finalize_topology_deferred(
                root=root,
                record=record,
            )

            return True

    return False


def run_inner_batch(
    root: Path,
) -> tuple[
    int,
    str,
]:

    inner = (
        root
        / "scripts"
        / "assertion_generation"
        / "run_stage6_batch.py"
    )

    if not inner.is_file():

        raise ResilientBatchError(
            "inner Stage-6 batch runner "
            f"not found: {inner}"
        )

    command = [
        sys.executable,
        str(
            inner
        ),
        "--limit",
        "all",
    ]

    print()

    print(
        "+ "
        + " ".join(
            command
        ),
        flush=True,
    )

    process = subprocess.Popen(
        command,
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    assert (
        process.stdout
        is not None
    )

    lines: list[
        str
    ] = []

    for line in process.stdout:

        sys.stdout.write(
            line
        )

        sys.stdout.flush()

        lines.append(
            line
        )

        # Keep enough recent output to
        # classify the current failure
        # without unbounded memory growth.
        if len(
            lines
        ) > 4000:

            del lines[
                :2000
            ]

    rc = process.wait()

    return (
        rc,
        "".join(
            lines
        ),
    )


def parse_args() -> argparse.Namespace:

    root = (
        Path(__file__)
        .resolve()
        .parents[2]
    )

    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--recovery-log",
        type=Path,
        default=(
            root
            / "runs"
            / "stage6"
            / "batch_002_remaining.log"
        ),
        help=(
            "Existing batch log used only "
            "to recover a topology mismatch "
            "that already stopped before "
            "this resilient runner started."
        ),
    )

    return parser.parse_args()


def main() -> int:

    args = parse_args()

    root = (
        Path(__file__)
        .resolve()
        .parents[2]
    )

    recovery_log = (
        args.recovery_log
        .expanduser()
        .resolve()
    )

    # Recover the fault that already
    # stopped the old batch before this
    # wrapper existed.
    recover_from_existing_log(
        root,
        recovery_log,
    )

    automatically_deferred: set[
        str
    ] = set()

    while True:

        (
            rc,
            output,
        ) = run_inner_batch(
            root
        )

        if rc == 0:

            print()

            print(
                "Stage-6 resilient batch "
                "completed successfully.",
                flush=True,
            )

            return 0

        records = mismatch_records(
            output
        )

        if not records:

            print()

            print(
                "ERROR: inner Stage-6 batch "
                "failed, but the failure is "
                "not the recognized topology "
                "mismatch. No automatic "
                "deferral was performed.",
                file=sys.stderr,
                flush=True,
            )

            return (
                rc
                if rc != 0
                else 2
            )

        record = records[
            -1
        ]

        fault_id = str(
            record[
                "fault_id"
            ]
        )

        if (
            fault_id
            in automatically_deferred
        ):

            raise ResilientBatchError(
                "the same topology-mismatch "
                "fault reappeared after being "
                "deferred; refusing an "
                "infinite restart loop: "
                f"{fault_id}"
            )

        finalize_topology_deferred(
            root=root,
            record=record,
        )

        automatically_deferred.add(
            fault_id
        )

        # Loop:
        # the original batch runner now
        # sees the durable DEFERRED result,
        # skips it, and resumes remaining
        # pending faults.


if __name__ == "__main__":

    try:

        raise SystemExit(
            main()
        )

    except ResilientBatchError as exc:

        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )

        raise SystemExit(
            2
        )
