#!/usr/bin/env python3
"""Run the complete Stage-6 workflow for exactly one fault."""

from __future__ import annotations

import argparse
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
    r"^TF\d{6}_SA[01]$"
)

STANDARD_SCHEMA = (
    "stage6_fault_result_v3"
)

DEFERRED_SCHEMA = (
    "stage6_deferred_result_v1"
)

CONTINUABLE_VERDICTS = {
    "GOLDEN_FALSE_POSITIVE",
    "TARGET_NOT_DETECTED",
    "COMPILE_FAILED",
}

# run_stage6_simulation.py returns
# non-zero for these explicit verdicts.
# COMPILE_FAILED is repairable within
# the three-generation budget.
NONZERO_SIMULATION_VERDICTS = {
    "COMPILE_FAILED",
    "GOLDEN_EXECUTION_FAILED",
    "FAULT_EXECUTION_FAILED",
}

# Runtime/tool failures remain terminal.
INFRA_VERDICTS = {
    "GOLDEN_EXECUTION_FAILED",
    "FAULT_EXECUTION_FAILED",
}


class Stage6FaultError(
    RuntimeError
):
    pass


def utc_now() -> str:

    return datetime.now(
        timezone.utc
    ).isoformat()


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

        raise Stage6FaultError(
            f"{label} not found: {path}"
        ) from exc

    except json.JSONDecodeError as exc:

        raise Stage6FaultError(
            f"invalid {label} JSON "
            f"{path}: {exc}"
        ) from exc

    if not isinstance(
        value,
        dict,
    ):

        raise Stage6FaultError(
            f"{label} must contain "
            f"one JSON object: {path}"
        )

    return value


def write_json(
    path: Path,
    payload: dict[str, Any],
) -> None:

    tmp = path.with_name(
        f".{path.name}.tmp"
    )

    tmp.write_text(
        json.dumps(
            payload,
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    tmp.replace(
        path
    )


def run(
    command: list[str],
) -> None:

    print(
        "+ "
        + " ".join(command),
        flush=True,
    )

    subprocess.run(
        command,
        check=True,
    )


def run_simulation(
    command: list[str],
    simulation_json: Path,
) -> None:

    print(
        "+ "
        + " ".join(command),
        flush=True,
    )

    completed = subprocess.run(
        command,
        check=False,
    )

    if completed.returncode == 0:
        return

    if not simulation_json.is_file():

        raise Stage6FaultError(
            "Stage-6 simulation failed "
            "before writing its verdict: "
            f"return_code="
            f"{completed.returncode}"
        )

    payload = load_json(
        simulation_json,
        "Stage-6 simulation",
    )

    verdict = str(
        payload.get(
            "verdict",
            "",
        )
    ).strip()

    if verdict not in NONZERO_SIMULATION_VERDICTS:

        raise Stage6FaultError(
            "Stage-6 simulation returned "
            "non-zero without a recognized "
            "terminal infrastructure verdict: "
            f"rc={completed.returncode}, "
            f"verdict={verdict!r}"
        )


def simulation_verdict(
    pilot_dir: Path,
    round_index: int,
) -> str:

    payload = load_json(
        pilot_dir
        / (
            f"round{round_index}"
            "_simulation.json"
        ),
        (
            f"Round-{round_index} "
            "simulation"
        ),
    )

    verdict = str(
        payload.get(
            "verdict",
            "",
        )
    ).strip()

    if not verdict:

        raise Stage6FaultError(
            f"Round-{round_index} "
            "simulation has no verdict"
        )

    return verdict


def result_state(
    result_file: Path,
    fault_id: str,
) -> str:

    result = load_json(
        result_file,
        "durable Stage-6 result",
    )

    if (
        result.get(
            "fault_id"
        )
        != fault_id
    ):

        raise Stage6FaultError(
            "existing result fault_id "
            f"mismatch: {result_file}"
        )

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

    if (
        result.get(
            "schema_version"
        )
        == STANDARD_SCHEMA
    ):

        return "FINALIZED"

    raise Stage6FaultError(
        "existing result has unsupported "
        "schema/status: "
        f"{result_file}"
    )


def classify_generation_output(
    response_text: str,
) -> str:

    lowered = (
        response_text.lower()
    )

    refusal_phrases = (
        "no valid property",
        "no property exists",
        "cannot generate",
        "can't generate",
        "unable to generate",
        "cannot produce",
        "unable to produce",
    )

    if any(
        phrase in lowered
        for phrase
        in refusal_phrases
    ):

        return (
            "GENERATOR_REFUSAL"
        )

    return (
        "GENERATION_OUTPUT_CONTRACT"
    )


def finalize_deferred_generation(
    *,
    fault_id: str,
    round_index: int,
    pilot_dir: Path,
    results_root: Path,
    output_dir: Path,
) -> None:

    prefix = (
        f"round{round_index}"
    )

    response_path = (
        pilot_dir
        / f"{prefix}_response.txt"
    )

    api_status_path = (
        pilot_dir
        / f"{prefix}_api_status.json"
    )

    prompt_path = (
        pilot_dir
        / "prompt.txt"
        if round_index == 0
        else
        pilot_dir
        / f"{prefix}_prompt.txt"
    )

    context_path = (
        pilot_dir
        / "visible_context.json"
        if round_index == 0
        else
        pilot_dir
        / f"{prefix}_model_context.json"
    )

    if (
        not response_path.is_file()
        or not api_status_path.is_file()
    ):

        raise Stage6FaultError(
            "cannot defer generation "
            "failure because persisted API "
            "artifacts are incomplete for "
            f"{fault_id} Round {round_index}"
        )

    response_text = (
        response_path
        .read_text(
            encoding="utf-8"
        )
        .strip()
    )

    api_status = load_json(
        api_status_path,
        "Stage-6 API status",
    )

    if (
        api_status.get(
            "response_status"
        )
        != "completed"
        or not response_text
    ):

        raise Stage6FaultError(
            "generation failed, but API "
            "response was not a completed "
            "nonempty model response; "
            "treating this as "
            "infrastructure/system failure"
        )

    reason = (
        classify_generation_output(
            response_text
        )
    )

    results_root.mkdir(
        parents=True,
        exist_ok=True,
    )

    if output_dir.exists():

        raise Stage6FaultError(
            "refusing to overwrite durable "
            "output directory: "
            f"{output_dir}"
        )

    temp = Path(
        tempfile.mkdtemp(
            prefix=(
                ".tmp_deferred_"
                f"{fault_id}_"
            ),
            dir=results_root,
        )
    ).resolve()

    try:

        copied: dict[
            str,
            str,
        ] = {}

        shutil.copy2(
            response_path,
            temp
            / "deferred_response.txt",
        )

        copied[
            "response_file"
        ] = (
            "deferred_response.txt"
        )

        shutil.copy2(
            api_status_path,
            temp
            / "deferred_api_status.json",
        )

        copied[
            "api_status_file"
        ] = (
            "deferred_api_status.json"
        )

        if prompt_path.is_file():

            shutil.copy2(
                prompt_path,
                temp
                / "deferred_prompt.txt",
            )

            copied[
                "prompt_file"
            ] = (
                "deferred_prompt.txt"
            )

        if context_path.is_file():

            shutil.copy2(
                context_path,
                temp
                / "deferred_context.json",
            )

            copied[
                "context_file"
            ] = (
                "deferred_context.json"
            )

        result = {
            "schema_version":
                DEFERRED_SCHEMA,

            "fault_id":
                fault_id,

            "status":
                "DEFERRED",

            "deferred": {
                "reason":
                    reason,

                "stage":
                    "ASSERTION_GENERATION",

                "round":
                    round_index,

                "recorded_at_utc":
                    utc_now(),

                "detail":
                    (
                        "The OpenAI API "
                        "completed and returned "
                        "nonempty output, but "
                        "the generation script "
                        "did not produce a valid "
                        "SVA property artifact. "
                        "This fault is deferred "
                        "for later manual "
                        "analysis and is not "
                        "counted as a scientific "
                        "TARGET_NOT_DETECTED "
                        "result."
                    ),

                "begin_sva_count":
                    response_text.count(
                        "BEGIN_SVA"
                    ),

                "end_sva_count":
                    response_text.count(
                        "END_SVA"
                    ),

                **copied,
            },

            "api": {
                "model":
                    api_status.get(
                        "model_requested"
                    ),

                "response_id":
                    api_status.get(
                        "response_id"
                    ),

                "response_status":
                    api_status.get(
                        "response_status"
                    ),

                "usage":
                    api_status.get(
                        "usage"
                    ),
            },
        }

        write_json(
            temp
            / "fault_result.json",
            result,
        )

        temp.replace(
            output_dir
        )

    except Exception:

        shutil.rmtree(
            temp,
            ignore_errors=True,
        )

        raise

    shutil.rmtree(
        pilot_dir,
        ignore_errors=True,
    )

    print(
        "=" * 96
    )

    print(
        "Stage-6 fault deferred: "
        f"{fault_id}"
    )

    print(
        f"Reason        : {reason}"
    )

    print(
        f"Round         : {round_index}"
    )

    print(
        "Result        : "
        f"{output_dir / 'fault_result.json'}"
    )

    print(
        "Batch action  : "
        "continue to next fault"
    )

    print(
        "=" * 96
    )


def recover_existing_generator_refusal(
    *,
    fault_id: str,
    pilot_dir: Path,
    results_root: Path,
    output_dir: Path,
) -> bool:

    if not pilot_dir.is_dir():
        return False

    for round_index in (
        0,
        1,
        2,
    ):

        prefix = (
            f"round{round_index}"
        )

        response_path = (
            pilot_dir
            / f"{prefix}_response.txt"
        )

        api_status_path = (
            pilot_dir
            / f"{prefix}_api_status.json"
        )

        property_path = (
            pilot_dir
            / f"{prefix}_property.sva"
        )

        if (
            response_path.is_file()
            and api_status_path.is_file()
            and not property_path.exists()
        ):

            response_text = (
                response_path
                .read_text(
                    encoding="utf-8"
                )
                .strip()
            )

            api_status = load_json(
                api_status_path,
                "Stage-6 API status",
            )

            if (
                api_status.get(
                    "response_status"
                )
                == "completed"
                and response_text
                and
                classify_generation_output(
                    response_text
                )
                == "GENERATOR_REFUSAL"
            ):

                finalize_deferred_generation(
                    fault_id=
                        fault_id,

                    round_index=
                        round_index,

                    pilot_dir=
                        pilot_dir,

                    results_root=
                        results_root,

                    output_dir=
                        output_dir,
                )

                return True

    return False


def run_generation(
    *,
    command: list[str],
    fault_id: str,
    round_index: int,
    pilot_dir: Path,
    results_root: Path,
    output_dir: Path,
) -> bool:

    print(
        "+ "
        + " ".join(command),
        flush=True,
    )

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    captured: list[
        str
    ] = []

    assert (
        process.stdout
        is not None
    )

    for line in process.stdout:

        print(
            line,
            end="",
            flush=True,
        )

        captured.append(
            line
        )

    return_code = (
        process.wait()
    )

    if return_code == 0:
        return False

    combined_output = (
        "".join(
            captured
        )
    )

    output_contract_errors = (
        (
            "OpenAI response must "
            "contain exactly one BEGIN_SVA"
        ),
        (
            "OpenAI response contains "
            "text outside"
        ),
        (
            "generated property is empty"
        ),
        (
            "model returned a complete "
            "assert property statement"
        ),
        (
            "generated property body "
            "must not end with a semicolon"
        ),
    )

    recognized_output_failure = any(
        marker in combined_output
        for marker
        in output_contract_errors
    )

    response_path = (
        pilot_dir
        / (
            f"round{round_index}"
            "_response.txt"
        )
    )

    api_status_path = (
        pilot_dir
        / (
            f"round{round_index}"
            "_api_status.json"
        )
    )

    if (
        recognized_output_failure
        and response_path.is_file()
        and api_status_path.is_file()
    ):

        api_status = load_json(
            api_status_path,
            "Stage-6 API status",
        )

        response_text = (
            response_path
            .read_text(
                encoding="utf-8"
            )
            .strip()
        )

        if (
            api_status.get(
                "response_status"
            )
            == "completed"
            and response_text
        ):

            finalize_deferred_generation(
                fault_id=
                    fault_id,

                round_index=
                    round_index,

                pilot_dir=
                    pilot_dir,

                results_root=
                    results_root,

                output_dir=
                    output_dir,
            )

            return True

    raise Stage6FaultError(
        "generation command failed "
        "with a non-deferred error; "
        f"return_code={return_code}. "
        "Batch must stop."
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
        "--fault-id",
        required=True,
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

    scripts = (
        root
        / "scripts"
        / "assertion_generation"
    )

    fault_id = (
        args.fault_id
        .strip()
    )

    if (
        FAULT_RE.fullmatch(
            fault_id
        )
        is None
    ):

        raise Stage6FaultError(
            f"invalid fault ID: "
            f"{fault_id!r}"
        )

    if args.maxcycles <= 0:

        raise Stage6FaultError(
            "--maxcycles must be positive"
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

    pilot_dir = (
        work_root
        / fault_id
    ).resolve()

    output_dir = (
        results_root
        / fault_id
    ).resolve()

    result_file = (
        output_dir
        / "fault_result.json"
    )

    if result_file.is_file():

        state = result_state(
            result_file,
            fault_id,
        )

        print(
            "Stage-6 fault already "
            f"processed: {fault_id} "
            f"({state})"
        )

        return 0

    if output_dir.exists():

        raise Stage6FaultError(
            "durable output directory "
            "exists without a valid result: "
            f"{output_dir}"
        )

    results_root.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Narrow recovery only for an already
    # persisted generator refusal.
    #
    # This handles the current
    # TF000015_SA1 case without another
    # API call.
    if pilot_dir.exists():

        if (
            recover_existing_generator_refusal(
                fault_id=
                    fault_id,

                pilot_dir=
                    pilot_dir,

                results_root=
                    results_root,

                output_dir=
                    output_dir,
            )
        ):

            return 0

        raise Stage6FaultError(
            "working directory already "
            "exists and is not a recognized "
            "deferred generator-refusal "
            "case: "
            f"{pilot_dir}"
        )

    work_root.mkdir(
        parents=True,
        exist_ok=True,
    )

    py = sys.executable

    # ------------------------------------------------------------
    # Golden behavior profile
    # ------------------------------------------------------------

    run(
        [
            py,
            str(
                scripts
                / "profile_golden_behavior.py"
            ),
            "--fault-id",
            fault_id,
            "--campaign-root",
            str(
                campaign_root
            ),
            "--pilot-dir",
            str(
                pilot_dir
            ),
            "--maxcycles",
            str(
                args.maxcycles
            ),
        ]
    )

    # ------------------------------------------------------------
    # Frozen baseline
    # ------------------------------------------------------------

    run(
        [
            py,
            str(
                scripts
                / "prepare_stage6_baseline.py"
            ),
            "--fault-id",
            fault_id,
            "--campaign-root",
            str(
                campaign_root
            ),
            "--pilot-dir",
            str(
                pilot_dir
            ),
        ]
    )

    # ------------------------------------------------------------
    # Round 0 generation
    # ------------------------------------------------------------

    deferred = run_generation(
        command=[
            py,
            str(
                scripts
                / "generate_stage6_round0.py"
            ),
            "--fault-id",
            fault_id,
            "--pilot-dir",
            str(
                pilot_dir
            ),
            "--credential-file",
            str(
                credential_file
            ),
        ],
        fault_id=
            fault_id,

        round_index=
            0,

        pilot_dir=
            pilot_dir,

        results_root=
            results_root,

        output_dir=
            output_dir,
    )

    if deferred:
        return 0

    # ------------------------------------------------------------
    # Round 0 simulation
    # ------------------------------------------------------------

    run_simulation(
        [
            py,
            str(
                scripts
                / "run_stage6_simulation.py"
            ),
            "--fault-id",
            fault_id,
            "--round",
            "0",
            "--campaign-root",
            str(
                campaign_root
            ),
            "--pilot-dir",
            str(
                pilot_dir
            ),
            "--maxcycles",
            str(
                args.maxcycles
            ),
        ],
        (
            pilot_dir
            / "round0_simulation.json"
        ),
    )

    scope_diagnosed = False
    final_round = 0

    # ------------------------------------------------------------
    # Round 1 / Round 2
    # ------------------------------------------------------------

    for next_round in (
        1,
        2,
    ):

        previous_round = (
            next_round - 1
        )

        verdict = (
            simulation_verdict(
                pilot_dir,
                previous_round,
            )
        )

        print(
            f"Fault {fault_id}: "
            f"Round {previous_round} "
            f"verdict = {verdict}",
            flush=True,
        )

        if (
            verdict
            not in
            CONTINUABLE_VERDICTS
        ):

            final_round = (
                previous_round
            )

            break

        if (
            verdict
            == "TARGET_NOT_DETECTED"
            and not scope_diagnosed
        ):

            run(
                [
                    py,
                    str(
                        scripts
                        / (
                            "prepare_stage6_"
                            "downstream_feedback.py"
                        )
                    ),
                    "--fault-id",
                    fault_id,
                    "--source-round",
                    str(
                        previous_round
                    ),
                    "--consumer-round",
                    str(
                        next_round
                    ),
                    "--campaign-root",
                    str(
                        campaign_root
                    ),
                    "--pilot-dir",
                    str(
                        pilot_dir
                    ),
                    "--maxcycles",
                    str(
                        args.maxcycles
                    ),
                ]
            )

            scope_diagnosed = True

        # --------------------------------------------------------
        # Next generation round
        # --------------------------------------------------------

        deferred = run_generation(
            command=[
                py,
                str(
                    scripts
                    / (
                        "generate_stage6_"
                        "next_round.py"
                    )
                ),
                "--fault-id",
                fault_id,
                "--next-round",
                str(
                    next_round
                ),
                "--pilot-dir",
                str(
                    pilot_dir
                ),
                "--credential-file",
                str(
                    credential_file
                ),
            ],
            fault_id=
                fault_id,

            round_index=
                next_round,

            pilot_dir=
                pilot_dir,

            results_root=
                results_root,

            output_dir=
                output_dir,
        )

        if deferred:
            return 0

        # --------------------------------------------------------
        # Simulation
        # --------------------------------------------------------

        run_simulation(
            [
                py,
                str(
                    scripts
                    / (
                        "run_stage6_"
                        "simulation.py"
                    )
                ),
                "--fault-id",
                fault_id,
                "--round",
                str(
                    next_round
                ),
                "--campaign-root",
                str(
                    campaign_root
                ),
                "--pilot-dir",
                str(
                    pilot_dir
                ),
                "--maxcycles",
                str(
                    args.maxcycles
                ),
            ],
            (
                pilot_dir
                / (
                    f"round{next_round}"
                    "_simulation.json"
                )
            ),
        )

        final_round = (
            next_round
        )

    # ------------------------------------------------------------
    # Normal terminal result
    # ------------------------------------------------------------

    final_verdict = (
        simulation_verdict(
            pilot_dir,
            final_round,
        )
    )

    print(
        f"Fault {fault_id}: "
        f"terminal Round {final_round} "
        f"verdict = {final_verdict}",
        flush=True,
    )

    run(
        [
            py,
            str(
                scripts
                / "finalize_stage6_fault.py"
            ),
            "--fault-id",
            fault_id,
            "--pilot-dir",
            str(
                pilot_dir
            ),
            "--output-dir",
            str(
                output_dir
            ),
            "--action",
            "finalize",
        ]
    )

    run(
        [
            py,
            str(
                scripts
                / "finalize_stage6_fault.py"
            ),
            "--fault-id",
            fault_id,
            "--pilot-dir",
            str(
                pilot_dir
            ),
            "--output-dir",
            str(
                output_dir
            ),
            "--action",
            "cleanup",
        ]
    )

    result = load_json(
        result_file,
        "durable Stage-6 result",
    )

    print(
        "=" * 96
    )

    print(
        "Stage-6 fault complete: "
        f"{fault_id}"
    )

    print(
        "Final verdict : "
        f"{result.get('final', {}).get('final_verdict')}"
    )

    print(
        "Success       : "
        f"{result.get('final', {}).get('success')}"
    )

    print(
        "Result        : "
        f"{result_file}"
    )

    print(
        "=" * 96
    )

    # Xcelium / infrastructure failures are
    # NOT deferred by this policy.
    if (
        final_verdict
        in INFRA_VERDICTS
    ):

        print(
            "ERROR: terminal "
            "infrastructure verdict; "
            "stopping the batch after "
            f"finalizing {fault_id}: "
            f"{final_verdict}",
            file=sys.stderr,
        )

        return 3

    return 0


if __name__ == "__main__":

    try:

        raise SystemExit(
            main()
        )

    except subprocess.CalledProcessError as exc:

        print(
            "ERROR: Stage-6 command "
            "failed with return code "
            f"{exc.returncode}",
            file=sys.stderr,
        )

        raise SystemExit(
            exc.returncode
            or 1
        )

    except Stage6FaultError as exc:

        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )

        raise SystemExit(
            2
        )
