#!/usr/bin/env python3
"""Run a finite batch of not-yet-processed Stage-6 faults."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

from pathlib import Path
from typing import Any


FAULT_RE = re.compile(
    r"^TF\d{6}_SA[01]$"
)

PASS_STATE = (
    "ORACLE_VALIDATED_CLEANED"
)

SUPPORTED_ROUTE = (
    "NATIVE_ONLY"
)

SUPPORTED_NATIVE_STATUS = {
    "OUTPUT_MATCH",
    "OUTPUT_MISMATCH",
    "TIMEOUT",
}

STANDARD_SCHEMA = (
    "stage6_fault_result_v3"
)

DEFERRED_SCHEMA = (
    "stage6_deferred_result_v1"
)


class Stage6BatchError(
    RuntimeError
):
    pass


def load_json(
    path: Path,
    label: str,
) -> dict[str, Any]:

    try:

        value = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

    except FileNotFoundError as exc:

        raise Stage6BatchError(
            f"{label} not found: {path}"
        ) from exc

    except json.JSONDecodeError as exc:

        raise Stage6BatchError(
            f"invalid {label} JSON "
            f"{path}: {exc}"
        ) from exc

    if not isinstance(
        value,
        dict,
    ):

        raise Stage6BatchError(
            f"{label} must contain "
            f"one JSON object: {path}"
        )

    return value


def parse_limit(
    raw: str,
) -> int | None:

    value = (
        raw
        .strip()
        .lower()
    )

    if value == "all":
        return None

    try:

        limit = int(
            value
        )

    except ValueError as exc:

        raise argparse.ArgumentTypeError(
            "--limit must be a positive "
            "integer or 'all'"
        ) from exc

    if limit <= 0:

        raise argparse.ArgumentTypeError(
            "--limit must be a positive "
            "integer or 'all'"
        )

    return limit


def eligible_faults(
    campaign_root: Path,
) -> list[str]:

    faults: list[
        str
    ] = []

    seen: set[
        str
    ] = set()

    for fault_json in (
        campaign_root.glob(
            "sites/*/TF*_SA*/fault.json"
        )
    ):

        fault_id = (
            fault_json
            .parent
            .name
        )

        if (
            FAULT_RE.fullmatch(
                fault_id
            )
            is None
        ):
            continue

        if fault_id in seen:

            raise Stage6BatchError(
                "duplicate fault ID in "
                "Stage-5 campaign: "
                f"{fault_id}"
            )

        fault_dir = (
            fault_json.parent
        )

        status = load_json(
            fault_dir
            / "status.json",
            (
                "Stage-5 status for "
                f"{fault_id}"
            ),
        )

        routing = load_json(
            fault_dir
            / "routing.json",
            (
                "Stage-5 routing for "
                f"{fault_id}"
            ),
        )

        if (
            status.get(
                "state"
            )
            != PASS_STATE
        ):
            continue

        if (
            routing.get(
                "route"
            )
            != SUPPORTED_ROUTE
        ):
            continue

        if (
            status.get(
                "native_status"
            )
            not in
            SUPPORTED_NATIVE_STATUS
        ):
            continue

        seen.add(
            fault_id
        )

        faults.append(
            fault_id
        )

    return sorted(
        faults
    )


def processed_state(
    results_root: Path,
    fault_id: str,
) -> str | None:

    output_dir = (
        results_root
        / fault_id
    )

    result_file = (
        output_dir
        / "fault_result.json"
    )

    if not output_dir.exists():
        return None

    if not result_file.is_file():

        raise Stage6BatchError(
            "result directory exists "
            "without fault_result.json: "
            f"{output_dir}"
        )

    result = load_json(
        result_file,
        (
            "Stage-6 result for "
            f"{fault_id}"
        ),
    )

    if (
        result.get(
            "fault_id"
        )
        != fault_id
    ):

        raise Stage6BatchError(
            "fault_id mismatch in "
            "durable result: "
            f"{result_file}"
        )

    if (
        result.get(
            "schema_version"
        )
        == STANDARD_SCHEMA
    ):

        return "FINALIZED"

    if (
        result.get(
            "schema_version"
        )
        == DEFERRED_SCHEMA
        and result.get(
            "status"
        )
        == "DEFERRED"
    ):

        return "DEFERRED"

    raise Stage6BatchError(
        "unsupported durable result "
        "schema/status: "
        f"{result_file}"
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
        "--limit",
        type=parse_limit,
        default=10,
        help=(
            "number of unfinished faults "
            "to process, or 'all'"
        ),
    )

    parser.add_argument(
        "--campaign-root",
        type=Path,
        default=(
            root
            / "runs"
            / "stage5_campaign_v3"
            / "cv32e40p"
            / "crc32"
            / "sites_all"
        ),
    )

    parser.add_argument(
        "--work-root",
        type=Path,
        default=(
            root
            / "runs"
            / "stage6"
            / "work"
        ),
    )

    parser.add_argument(
        "--results-root",
        type=Path,
        default=(
            root
            / "runs"
            / "stage6"
            / "results"
        ),
    )

    parser.add_argument(
        "--credential-file",
        type=Path,
        default=(
            Path.home()
            / ".config"
            / "fault2assertion"
            / "openai.env"
        ),
    )

    parser.add_argument(
        "--maxcycles",
        type=int,
        default=2_000_000,
    )

    return parser.parse_args()


def main() -> int:

    args = parse_args()

    root = (
        Path(__file__)
        .resolve()
        .parents[2]
    )

    fault_runner = (
        root
        / "scripts"
        / "assertion_generation"
        / "run_stage6_fault.py"
    )

    campaign_root = (
        args.campaign_root
        .expanduser()
        .resolve()
    )

    work_root = (
        args.work_root
        .expanduser()
        .resolve()
    )

    results_root = (
        args.results_root
        .expanduser()
        .resolve()
    )

    credential_file = (
        args.credential_file
        .expanduser()
        .resolve()
    )

    if not campaign_root.is_dir():

        raise Stage6BatchError(
            "Stage-5 campaign root "
            f"not found: {campaign_root}"
        )

    if args.maxcycles <= 0:

        raise Stage6BatchError(
            "--maxcycles must be positive"
        )

    faults = eligible_faults(
        campaign_root
    )

    if not faults:

        raise Stage6BatchError(
            "no Stage-6 eligible faults "
            "found in the Stage-5 campaign"
        )

    finalized: list[
        str
    ] = []

    deferred: list[
        str
    ] = []

    pending: list[
        str
    ] = []

    for fault_id in faults:

        state = processed_state(
            results_root,
            fault_id,
        )

        if state == "FINALIZED":

            finalized.append(
                fault_id
            )

        elif state == "DEFERRED":

            deferred.append(
                fault_id
            )

        else:

            pending.append(
                fault_id
            )

    selected = (
        pending
        if args.limit is None
        else
        pending[
            :args.limit
        ]
    )

    print(
        "=" * 96
    )

    print(
        "Stage-6 batch selection"
    )

    print(
        "=" * 96
    )

    print(
        "Eligible faults    : "
        f"{len(faults)}"
    )

    print(
        "Already finalized  : "
        f"{len(finalized)}"
    )

    print(
        "Already deferred   : "
        f"{len(deferred)}"
    )

    print(
        "Pending            : "
        f"{len(pending)}"
    )

    print(
        "Selected           : "
        f"{len(selected)}"
    )

    if selected:

        print(
            "Faults:"
        )

        for (
            index,
            fault_id,
        ) in enumerate(
            selected,
            start=1,
        ):

            print(
                f"  {index:>3}. "
                f"{fault_id}"
            )

    else:

        print(
            "Nothing to run. "
            "All eligible faults are "
            "already processed."
        )

        return 0

    py = sys.executable

    for (
        index,
        fault_id,
    ) in enumerate(
        selected,
        start=1,
    ):

        print(
            "\n"
            + "#" * 96
        )

        print(
            f"Batch fault "
            f"{index}/"
            f"{len(selected)}: "
            f"{fault_id}",
            flush=True,
        )

        print(
            "#" * 96
        )

        command = [
            py,
            str(
                fault_runner
            ),
            "--fault-id",
            fault_id,
            "--campaign-root",
            str(
                campaign_root
            ),
            "--work-root",
            str(
                work_root
            ),
            "--results-root",
            str(
                results_root
            ),
            "--credential-file",
            str(
                credential_file
            ),
            "--maxcycles",
            str(
                args.maxcycles
            ),
        ]

        print(
            "+ "
            + " ".join(
                command
            ),
            flush=True,
        )

        subprocess.run(
            command,
            check=True,
        )

        state = processed_state(
            results_root,
            fault_id,
        )

        if state is None:

            raise Stage6BatchError(
                "fault runner returned "
                "success but no durable "
                "result exists: "
                f"{fault_id}"
            )

        print(
            "Batch outcome: "
            f"{fault_id} -> {state}"
        )

    finalized_after = 0
    deferred_after = 0

    for fault_id in faults:

        state = processed_state(
            results_root,
            fault_id,
        )

        if state == "FINALIZED":

            finalized_after += 1

        elif state == "DEFERRED":

            deferred_after += 1

    processed_after = (
        finalized_after
        + deferred_after
    )

    print(
        "\n"
        + "=" * 96
    )

    print(
        "Stage-6 batch complete"
    )

    print(
        "=" * 96
    )

    print(
        "Processed this batch : "
        f"{len(selected)}"
    )

    print(
        "Finalized total      : "
        f"{finalized_after}"
    )

    print(
        "Deferred total       : "
        f"{deferred_after}"
    )

    print(
        "Processed total      : "
        f"{processed_after}/"
        f"{len(faults)}"
    )

    print(
        "Remaining            : "
        f"{len(faults) - processed_after}"
    )

    print(
        "Results root         : "
        f"{results_root}"
    )

    print(
        "=" * 96
    )

    return 0


if __name__ == "__main__":

    try:

        raise SystemExit(
            main()
        )

    except subprocess.CalledProcessError as exc:

        print(
            "ERROR: batch stopped "
            "because one fault failed "
            "with a non-deferred error; "
            f"return code="
            f"{exc.returncode}",
            file=sys.stderr,
        )

        raise SystemExit(
            exc.returncode
            or 1
        )

    except Stage6BatchError as exc:

        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )

        raise SystemExit(
            2
        )
