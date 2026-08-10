#!/usr/bin/env python3
"""Finalize one completed Stage-6 fault into the frozen durable data schema.

Actions:

check
    Validate that the source Stage-6 pilot/work directory is terminal and
    print the compact scientific summary. Do not write or delete anything.

finalize
    Build the durable result atomically:
        fault_result.json
        roundN_property.sva
        optional failure.log
    The source work directory is retained.

cleanup
    Recompute the source result, verify that the durable result is identical,
    then delete the entire source work directory.

This script performs no assertion generation, no downstream selection, no
fault simulation, and no scientific classification beyond summarizing the
already-produced Stage-6 artifacts.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import shutil
import sys
import tempfile

from pathlib import Path
from typing import Any


SCHEMA_VERSION = "stage6_fault_result_v1"

MAX_ROUND = 2

FAULT_RE = re.compile(
    r"^TF\d{6}_SA[01]$"
)

CONTINUE = {
    "GOLDEN_FALSE_POSITIVE",
    "TARGET_NOT_DETECTED",
}

INFRA = {
    "COMPILE_FAILED",
    "GOLDEN_EXECUTION_FAILED",
    "FAULT_EXECUTION_FAILED",
}

NO_LOCALIZATION = {
    "NO_DOWNSTREAM_CANDIDATES",
    "NO_DISCRIMINATIVE_DOWNSTREAM_CANDIDATE",
}

VALID_FEEDBACK = {
    "GOLDEN_REPAIR",
    "LOCALIZED_FAULT",
    "FAULT_COUNTEREXAMPLE",
}


class FinalizeError(RuntimeError):
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
        raise FinalizeError(
            f"{label} not found: {path}"
        ) from exc

    except json.JSONDecodeError as exc:
        raise FinalizeError(
            f"invalid {label} JSON "
            f"{path}: {exc}"
        ) from exc

    if not isinstance(
        value,
        dict,
    ):
        raise FinalizeError(
            f"{label} must contain "
            f"one JSON object: {path}"
        )

    return value


def write_json(
    path: Path,
    value: dict[str, Any],
) -> None:

    tmp = path.with_name(
        f".{path.name}.tmp"
    )

    tmp.write_text(
        json.dumps(
            value,
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    tmp.replace(
        path
    )


def sha256(
    path: Path,
) -> str:

    digest = hashlib.sha256()

    with path.open(
        "rb"
    ) as handle:

        for block in iter(
            lambda: handle.read(
                1024 * 1024
            ),
            b"",
        ):
            digest.update(
                block
            )

    return digest.hexdigest()


def api_summary(
    pilot: Path,
    round_index: int,
) -> dict[str, Any] | None:

    path = (
        pilot
        / f"round{round_index}_api_status.json"
    )

    if not path.is_file():
        return None

    payload = load_json(
        path,
        f"Round-{round_index} API status",
    )

    result: dict[
        str,
        Any,
    ] = {}

    if (
        payload.get(
            "response_id"
        )
        is not None
    ):
        result[
            "response_id"
        ] = payload[
            "response_id"
        ]

    if (
        payload.get(
            "response_status"
        )
        is not None
    ):
        result[
            "status"
        ] = payload[
            "response_status"
        ]

    usage = payload.get(
        "usage"
    )

    if isinstance(
        usage,
        dict,
    ):
        result[
            "usage"
        ] = copy.deepcopy(
            usage
        )

    return (
        result
        or None
    )


def feedback_type(
    pilot: Path,
    round_index: int,
) -> str:

    if round_index == 0:
        return "NONE"

    meta = load_json(
        pilot
        / (
            f"round{round_index}"
            "_generation_meta.json"
        ),
        (
            f"Round-{round_index} "
            "generation metadata"
        ),
    )

    value = meta.get(
        "feedback_type"
    )

    if value not in VALID_FEEDBACK:
        raise FinalizeError(
            f"invalid Round-{round_index} "
            f"feedback_type: {value!r}"
        )

    return str(
        value
    )


def simulation_summary(
    simulation: dict[str, Any],
) -> dict[str, Any]:

    def status(
        name: str,
    ) -> Any:

        value = simulation.get(
            name
        )

        if isinstance(
            value,
            dict,
        ):
            return value.get(
                "runner_status"
            )

        return None

    result: dict[
        str,
        Any,
    ] = {
        "verdict":
            simulation.get(
                "verdict"
            ),

        "compile_status":
            status(
                "compile"
            ),

        "golden_status":
            status(
                "golden"
            ),

        "faulty_status":
            status(
                "faulty"
            ),
    }

    verdict = simulation.get(
        "verdict"
    )

    if (
        verdict
        == "GOLDEN_FALSE_POSITIVE"
    ):
        phase = "golden"

    elif (
        verdict
        == "TARGET_DETECTED"
    ):
        phase = "faulty"

    else:
        phase = None

    if phase is not None:

        record = simulation.get(
            phase
        )

        event = (
            record.get(
                "generated_assertion"
            )
            if isinstance(
                record,
                dict,
            )
            else None
        )

        if (
            isinstance(
                event,
                dict,
            )
            and event.get(
                "triggered"
            )
            is True
        ):

            trigger: dict[
                str,
                Any,
            ] = {
                "phase":
                    phase,

                "triggered":
                    True,
            }

            if isinstance(
                event.get(
                    "cycle"
                ),
                int,
            ):
                trigger[
                    "cycle"
                ] = event[
                    "cycle"
                ]

            if isinstance(
                event.get(
                    "sampled_values"
                ),
                dict,
            ):
                trigger[
                    "sampled_values"
                ] = copy.deepcopy(
                    event[
                        "sampled_values"
                    ]
                )

            result[
                "assertion_trigger"
            ] = trigger

    return result


def round_input_delta(
    pilot: Path,
    round_index: int,
    feedback: str,
) -> dict[str, Any]:

    if round_index == 0:

        visible = load_json(
            pilot
            / "visible_context.json",
            "Round-0 visible context",
        )

        golden = visible.get(
            "golden_behavior"
        )

        if not isinstance(
            golden,
            dict,
        ):
            raise FinalizeError(
                "visible_context.json "
                "has no golden_behavior"
            )

        result: dict[
            str,
            Any,
        ] = {
            "type":
                "BASELINE",

            "golden_behavior":
                copy.deepcopy(
                    golden
                ),
        }

        training = visible.get(
            "training_observation"
        )

        if isinstance(
            training,
            dict,
        ):
            result[
                "training_observation"
            ] = copy.deepcopy(
                training
            )

        return result

    model = load_json(
        pilot
        / (
            f"round{round_index}"
            "_model_context.json"
        ),
        (
            f"Round-{round_index} "
            "model context"
        ),
    )

    previous = model.get(
        "previous_round"
    )

    if not isinstance(
        previous,
        dict,
    ):
        raise FinalizeError(
            f"Round-{round_index} "
            "model context has "
            "no previous_round"
        )

    result = {
        "type":
            feedback,

        "inherits_context_from_round":
            round_index - 1,

        "previous_round":
            previous.get(
                "round"
            ),

        "previous_verdict":
            previous.get(
                "verdict"
            ),
    }

    if (
        feedback
        == "GOLDEN_REPAIR"
    ):

        result[
            "new_information"
        ] = {
            "previous_property_invalid_on_valid_golden":
                True,

            "new_target_fault_information":
                False,

            "exact_golden_counterexample_provided":
                False,
        }

        return result

    diagnostic = model.get(
        "diagnostic_feedback"
    )

    if not isinstance(
        diagnostic,
        dict,
    ):
        raise FinalizeError(
            f"Round-{round_index} "
            f"{feedback} has no "
            "diagnostic_feedback"
        )

    if (
        feedback
        == "LOCALIZED_FAULT"
    ):

        golden = model.get(
            "golden_behavior"
        )

        if not isinstance(
            golden,
            dict,
        ):
            raise FinalizeError(
                f"Round-{round_index} "
                "LOCALIZED_FAULT "
                "has no golden_behavior"
            )

        result[
            "new_information"
        ] = {
            "selected_observation_alias":
                diagnostic.get(
                    "selected_observation_alias"
                ),

            "evidence_types":
                copy.deepcopy(
                    diagnostic.get(
                        "evidence_types",
                        [],
                    )
                ),

            "expanded_golden_behavior":
                copy.deepcopy(
                    golden
                ),
        }

        return result

    if (
        feedback
        == "FAULT_COUNTEREXAMPLE"
    ):

        result[
            "new_information"
        ] = copy.deepcopy(
            diagnostic
        )

        return result

    raise FinalizeError(
        "unsupported feedback type: "
        f"{feedback}"
    )


def collect_rounds(
    pilot: Path,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
]:

    rounds: list[
        dict[str, Any]
    ] = []

    simulations: list[
        dict[str, Any]
    ] = []

    gap_seen = False

    for round_index in range(
        MAX_ROUND + 1
    ):

        property_path = (
            pilot
            / (
                f"round{round_index}"
                "_property.sva"
            )
        )

        simulation_path = (
            pilot
            / (
                f"round{round_index}"
                "_simulation.json"
            )
        )

        property_exists = (
            property_path.is_file()
        )

        simulation_exists = (
            simulation_path.is_file()
        )

        if (
            not property_exists
            and not simulation_exists
        ):
            gap_seen = True
            continue

        if gap_seen:
            raise FinalizeError(
                "non-contiguous artifacts "
                f"at Round {round_index}"
            )

        if (
            not property_exists
            or not simulation_exists
        ):
            raise FinalizeError(
                f"Round-{round_index} "
                "is incomplete: "
                "property/simulation pair "
                "required"
            )

        if not (
            property_path
            .read_text(
                encoding="utf-8"
            )
            .strip()
        ):
            raise FinalizeError(
                f"Round-{round_index} "
                "property is empty"
            )

        simulation = load_json(
            simulation_path,
            (
                f"Round-{round_index} "
                "simulation"
            ),
        )

        feedback = feedback_type(
            pilot,
            round_index,
        )

        record: dict[
            str,
            Any,
        ] = {
            "round":
                round_index,

            "feedback_type":
                feedback,

            "property_file":
                property_path.name,

            "property_sha256":
                sha256(
                    property_path
                ),

            "model_input_delta":
                round_input_delta(
                    pilot,
                    round_index,
                    feedback,
                ),

            "simulation":
                simulation_summary(
                    simulation
                ),
        }

        api = api_summary(
            pilot,
            round_index,
        )

        if api is not None:
            record[
                "api"
            ] = api

        rounds.append(
            record
        )

        simulations.append(
            simulation
        )

    if not rounds:
        raise FinalizeError(
            "no completed rounds found"
        )

    return (
        rounds,
        simulations,
    )


def downstream_block(
    pilot: Path,
    next_round: int,
) -> str | None:

    path = (
        pilot
        / (
            f"round{next_round}"
            "_downstream_feedback.json"
        )
    )

    if not path.is_file():
        return None

    status = load_json(
        path,
        (
            f"Round-{next_round} "
            "downstream feedback"
        ),
    ).get(
        "status"
    )

    if status in NO_LOCALIZATION:
        return str(
            status
        )

    return None


def terminal_state(
    pilot: Path,
    rounds: list[dict[str, Any]],
    simulations: list[dict[str, Any]],
) -> dict[str, Any]:

    for (
        index,
        simulation,
    ) in enumerate(
        simulations
    ):

        verdict = simulation.get(
            "verdict"
        )

        if (
            verdict
            == "TARGET_DETECTED"
        ):

            if (
                index
                != len(
                    simulations
                )
                - 1
            ):
                raise FinalizeError(
                    "artifacts exist after "
                    "TARGET_DETECTED"
                )

            return {
                "terminal":
                    True,

                "success":
                    True,

                "final_verdict":
                    verdict,

                "terminal_reason":
                    "TARGET_DETECTED",

                "success_round":
                    rounds[
                        index
                    ][
                        "round"
                    ],
            }

        if verdict in INFRA:

            if (
                index
                != len(
                    simulations
                )
                - 1
            ):
                raise FinalizeError(
                    "artifacts exist after "
                    f"terminal {verdict}"
                )

            return {
                "terminal":
                    True,

                "success":
                    False,

                "final_verdict":
                    verdict,

                "terminal_reason":
                    "INFRASTRUCTURE_FAILURE",

                "success_round":
                    None,
            }

        if verdict not in CONTINUE:

            if (
                index
                != len(
                    simulations
                )
                - 1
            ):
                raise FinalizeError(
                    "artifacts exist after "
                    "unsupported terminal "
                    f"verdict {verdict!r}"
                )

            return {
                "terminal":
                    True,

                "success":
                    False,

                "final_verdict":
                    verdict,

                "terminal_reason":
                    "OTHER_TERMINAL_FAILURE",

                "success_round":
                    None,
            }

    last_round = int(
        rounds[-1][
            "round"
        ]
    )

    last_verdict = (
        simulations[-1]
        .get(
            "verdict"
        )
    )

    if last_round == MAX_ROUND:

        return {
            "terminal":
                True,

            "success":
                False,

            "final_verdict":
                last_verdict,

            "terminal_reason":
                "GENERATION_BUDGET_EXHAUSTED",

            "success_round":
                None,
        }

    if (
        last_verdict
        == "TARGET_NOT_DETECTED"
    ):

        blocked = downstream_block(
            pilot,
            last_round + 1,
        )

        if blocked is not None:

            return {
                "terminal":
                    True,

                "success":
                    False,

                "final_verdict":
                    blocked,

                "terminal_reason":
                    "NO_LOCALIZATION_CANDIDATE",

                "success_round":
                    None,
            }

    return {
        "terminal":
            False,

        "success":
            False,

        "final_verdict":
            last_verdict,

        "terminal_reason":
            "NEXT_GENERATION_REQUIRED",

        "success_round":
            None,
    }


def evidence_types(
    selected: dict[str, Any],
) -> list[str]:

    result: list[
        str
    ] = []

    states = selected.get(
        "candidate_added_fault_only_states"
    )

    transitions = selected.get(
        "fault_only_site_candidate_transitions"
    )

    if (
        isinstance(
            states,
            list,
        )
        and states
    ):
        result.append(
            "SAME_CYCLE_JOINT_STATE_NOVELTY"
        )

    if (
        isinstance(
            transitions,
            list,
        )
        and transitions
    ):
        result.append(
            "ONE_CYCLE_TRANSITION_NOVELTY"
        )

    return result


def diagnostic_evidence(
    pilot: Path,
    rounds: list[dict[str, Any]],
) -> dict[str, Any] | None:

    teacher_paths: set[
        str
    ] = set()

    for item in rounds:

        if item[
            "feedback_type"
        ] not in {
            "LOCALIZED_FAULT",
            "FAULT_COUNTEREXAMPLE",
        }:
            continue

        round_index = int(
            item[
                "round"
            ]
        )

        meta = load_json(
            pilot
            / (
                f"round{round_index}"
                "_generation_meta.json"
            ),
            (
                f"Round-{round_index} "
                "generation metadata"
            ),
        )

        value = meta.get(
            "teacher_record"
        )

        if (
            isinstance(
                value,
                str,
            )
            and value.strip()
        ):

            teacher_paths.add(
                str(
                    Path(
                        value
                    )
                    .expanduser()
                    .resolve()
                )
            )

    if not teacher_paths:
        return None

    if len(
        teacher_paths
    ) != 1:

        raise FinalizeError(
            "multiple teacher records "
            "referenced: "
            f"{sorted(teacher_paths)}"
        )

    teacher_path = Path(
        next(
            iter(
                teacher_paths
            )
        )
    )

    teacher = load_json(
        teacher_path,
        "downstream teacher record",
    )

    selected = teacher.get(
        "selected"
    )

    if (
        teacher.get(
            "status"
        )
        != "DOWNSTREAM_CANDIDATE_FOUND"
        or not isinstance(
            selected,
            dict,
        )
    ):
        raise FinalizeError(
            "referenced teacher record "
            "has no selected candidate"
        )

    result: dict[
        str,
        Any,
    ] = {
        "selected_alias":
            selected.get(
                "alias"
            ),

        "selected_depth":
            selected.get(
                "depth"
            ),

        "selected_internal_signal":
            selected.get(
                "expression"
            ),

        "evidence_types":
            evidence_types(
                selected
            ),

        "fault_only_observed_states":
            copy.deepcopy(
                selected.get(
                    "candidate_added_fault_only_states",
                    [],
                )
            ),

        "fault_only_site_downstream_transitions":
            copy.deepcopy(
                selected.get(
                    "fault_only_site_candidate_transitions",
                    [],
                )
            ),
    }

    earliest = selected.get(
        "earliest_divergence"
    )

    if (
        isinstance(
            earliest,
            dict,
        )
        and isinstance(
            earliest.get(
                "golden_values"
            ),
            dict,
        )
        and isinstance(
            earliest.get(
                "fault_values"
            ),
            dict,
        )
    ):

        result[
            "divergence_values"
        ] = {
            "golden_values":
                copy.deepcopy(
                    earliest[
                        "golden_values"
                    ]
                ),

            "fault_values":
                copy.deepcopy(
                    earliest[
                        "fault_values"
                    ]
                ),
        }

    return result


def trajectory(
    rounds: list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    str,
]:

    verdict_code = {
        "GOLDEN_FALSE_POSITIVE":
            "GFP",

        "TARGET_NOT_DETECTED":
            "TND",

        "TARGET_DETECTED":
            "TD",

        "COMPILE_FAILED":
            "CF",

        "GOLDEN_EXECUTION_FAILED":
            "GEF",

        "FAULT_EXECUTION_FAILED":
            "FEF",
    }

    feedback_code = {
        "NONE":
            "NONE",

        "GOLDEN_REPAIR":
            "GR",

        "LOCALIZED_FAULT":
            "LOC",

        "FAULT_COUNTEREXAMPLE":
            "CE",
    }

    records: list[
        dict[str, Any]
    ] = []

    codes: list[
        str
    ] = []

    for item in rounds:

        verdict = (
            item[
                "simulation"
            ]
            .get(
                "verdict"
            )
        )

        feedback = item[
            "feedback_type"
        ]

        records.append(
            {
                "round":
                    item[
                        "round"
                    ],

                "feedback_type":
                    feedback,

                "verdict":
                    verdict,
            }
        )

        codes.append(
            f"R{item['round']}:"
            f"{feedback_code.get(feedback, feedback)}:"
            f"{verdict_code.get(str(verdict), str(verdict))}"
        )

    return (
        records,
        " -> ".join(
            codes
        ),
    )


def total_usage(
    rounds: list[dict[str, Any]],
) -> dict[str, int] | None:

    result: dict[
        str,
        int,
    ] = {}

    for item in rounds:

        api = item.get(
            "api"
        )

        usage = (
            api.get(
                "usage"
            )
            if isinstance(
                api,
                dict,
            )
            else None
        )

        if not isinstance(
            usage,
            dict,
        ):
            continue

        for key in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
        ):

            value = usage.get(
                key
            )

            if isinstance(
                value,
                int,
            ):

                result[
                    key
                ] = (
                    result.get(
                        key,
                        0,
                    )
                    + value
                )

    return (
        result
        or None
    )


def build_result(
    fault_id: str,
    pilot: Path,
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
]:

    (
        rounds,
        simulations,
    ) = collect_rounds(
        pilot
    )

    terminal = terminal_state(
        pilot,
        rounds,
        simulations,
    )

    if not terminal[
        "terminal"
    ]:

        raise FinalizeError(
            "fault is not terminal: "
            f"R{rounds[-1]['round']}="
            f"{terminal['final_verdict']}; "
            "continue workflow first"
        )

    (
        trajectory_records,
        trajectory_code,
    ) = trajectory(
        rounds
    )

    result: dict[
        str,
        Any,
    ] = {
        "schema_version":
            SCHEMA_VERSION,

        "fault_id":
            fault_id,

        "generation_budget": {
            "max_attempts":
                3,

            "attempts_used":
                len(
                    rounds
                ),
        },

        "trajectory":
            trajectory_records,

        "trajectory_code":
            trajectory_code,

        "rounds":
            rounds,

        "final": {
            "success":
                terminal[
                    "success"
                ],

            "final_verdict":
                terminal[
                    "final_verdict"
                ],

            "terminal_reason":
                terminal[
                    "terminal_reason"
                ],

            "success_round":
                terminal[
                    "success_round"
                ],
        },
    }

    diagnostic = diagnostic_evidence(
        pilot,
        rounds,
    )

    if diagnostic is not None:

        result[
            "diagnostic_evidence"
        ] = diagnostic

    usage = total_usage(
        rounds
    )

    if usage is not None:

        result[
            "api_usage_total"
        ] = usage

    return (
        result,
        rounds,
    )


def verify_output(
    output: Path,
    fault_id: str,
    expected:
        dict[str, Any]
        | None = None,
) -> dict[str, Any]:

    result = load_json(
        output
        / "fault_result.json",
        "durable fault result",
    )

    if (
        result.get(
            "schema_version"
        )
        != SCHEMA_VERSION
        or result.get(
            "fault_id"
        )
        != fault_id
    ):
        raise FinalizeError(
            "durable result "
            "schema/fault_id mismatch"
        )

    rounds = result.get(
        "rounds"
    )

    if (
        not isinstance(
            rounds,
            list,
        )
        or not rounds
    ):
        raise FinalizeError(
            "durable result "
            "has no rounds"
        )

    for item in rounds:

        round_index = item.get(
            "round"
        )

        property_path = (
            output
            / str(
                item.get(
                    "property_file"
                )
            )
        )

        if (
            not isinstance(
                round_index,
                int,
            )
            or not property_path.is_file()
            or sha256(
                property_path
            )
            != item.get(
                "property_sha256"
            )
        ):
            raise FinalizeError(
                f"durable Round-"
                f"{round_index} "
                "property verification "
                "failed"
            )

    if expected is not None:

        actual_text = json.dumps(
            result,
            sort_keys=True,
            separators=(
                ",",
                ":",
            ),
            ensure_ascii=False,
        )

        expected_text = json.dumps(
            expected,
            sort_keys=True,
            separators=(
                ",",
                ":",
            ),
            ensure_ascii=False,
        )

        if (
            actual_text
            != expected_text
        ):
            raise FinalizeError(
                "durable "
                "fault_result.json "
                "differs from "
                "source-derived result"
            )

    return result


def copy_failure_log(
    pilot: Path,
    temp: Path,
    result: dict[str, Any],
) -> None:

    if (
        result[
            "final"
        ][
            "terminal_reason"
        ]
        not in {
            "INFRASTRUCTURE_FAILURE",
            "OTHER_TERMINAL_FAILURE",
        }
    ):
        return

    round_index = int(
        result[
            "rounds"
        ][
            -1
        ][
            "round"
        ]
    )

    for name in (
        f"round{round_index}_faulty.log",
        f"round{round_index}_golden.log",
        f"round{round_index}_compile.log",
    ):

        source = (
            pilot
            / name
        )

        if (
            source.is_file()
            and source.stat().st_size
        ):

            shutil.copy2(
                source,
                temp
                / "failure.log",
            )

            return


def inside(
    parent: Path,
    child: Path,
) -> bool:

    try:
        child.relative_to(
            parent
        )

        return True

    except ValueError:
        return False


def do_check(
    fault_id: str,
    pilot: Path,
) -> int:

    (
        result,
        _,
    ) = build_result(
        fault_id,
        pilot,
    )

    print("=" * 80)

    print(
        "Stage-6 finalization "
        "check: TERMINAL"
    )

    print("=" * 80)

    print(
        f"Fault ID        : "
        f"{fault_id}"
    )

    print(
        "Attempts used   : "
        f"{result['generation_budget']['attempts_used']}/3"
    )

    print(
        f"Trajectory      : "
        f"{result['trajectory_code']}"
    )

    print(
        f"Final success   : "
        f"{result['final']['success']}"
    )

    print(
        f"Final verdict   : "
        f"{result['final']['final_verdict']}"
    )

    print(
        f"Terminal reason : "
        f"{result['final']['terminal_reason']}"
    )

    return 0


def do_finalize(
    fault_id: str,
    pilot: Path,
    output: Path,
) -> int:

    if output.exists():
        raise FinalizeError(
            "refusing to overwrite "
            "durable output: "
            f"{output}"
        )

    if inside(
        pilot,
        output,
    ):
        raise FinalizeError(
            "durable output must "
            "not be inside "
            "source pilot"
        )

    (
        result,
        rounds,
    ) = build_result(
        fault_id,
        pilot,
    )

    output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp = Path(
        tempfile.mkdtemp(
            prefix=(
                f".{output.name}"
                ".finalizing."
            ),
            dir=output.parent,
        )
    ).resolve()

    try:

        write_json(
            temp
            / "fault_result.json",
            result,
        )

        for item in rounds:

            round_index = int(
                item[
                    "round"
                ]
            )

            shutil.copy2(
                pilot
                / (
                    f"round{round_index}"
                    "_property.sva"
                ),

                temp
                / (
                    f"round{round_index}"
                    "_property.sva"
                ),
            )

        copy_failure_log(
            pilot,
            temp,
            result,
        )

        verify_output(
            temp,
            fault_id,
            result,
        )

        temp.replace(
            output
        )

    except Exception:

        shutil.rmtree(
            temp,
            ignore_errors=True,
        )

        raise

    verify_output(
        output,
        fault_id,
        result,
    )

    print("=" * 80)

    print(
        "Stage-6 finalization: PASS"
    )

    print("=" * 80)

    print(
        f"Fault ID       : "
        f"{fault_id}"
    )

    print(
        f"Durable output : "
        f"{output}"
    )

    print(
        f"Trajectory     : "
        f"{result['trajectory_code']}"
    )

    print(
        f"Final success  : "
        f"{result['final']['success']}"
    )

    print(
        "Source cleanup : "
        "NOT PERFORMED"
    )

    return 0


def do_cleanup(
    fault_id: str,
    pilot: Path,
    output: Path,
) -> int:

    if inside(
        pilot,
        output,
    ):
        raise FinalizeError(
            "durable output must "
            "not be inside source pilot"
        )

    (
        source_result,
        _,
    ) = build_result(
        fault_id,
        pilot,
    )

    verify_output(
        output,
        fault_id,
        source_result,
    )

    if (
        len(
            pilot.parts
        )
        < 5
        or pilot.name
        in {
            "",
            ".",
            "..",
            "stage6",
        }
    ):
        raise FinalizeError(
            "refusing unsafe "
            f"cleanup path: {pilot}"
        )

    shutil.rmtree(
        pilot
    )

    print("=" * 80)

    print(
        "Stage-6 source "
        "cleanup: PASS"
    )

    print("=" * 80)

    print(
        f"Fault ID       : "
        f"{fault_id}"
    )

    print(
        f"Deleted source : "
        f"{pilot}"
    )

    print(
        f"Retained       : "
        f"{output}"
    )

    return 0


def parse_args() -> argparse.Namespace:

    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--fault-id",
        required=True,
    )

    parser.add_argument(
        "--pilot-dir",
        type=Path,
        required=True,
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
    )

    parser.add_argument(
        "--action",
        choices=(
            "check",
            "finalize",
            "cleanup",
        ),
        required=True,
    )

    return parser.parse_args()


def main() -> int:

    args = parse_args()

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
        raise FinalizeError(
            f"invalid fault ID: "
            f"{fault_id!r}"
        )

    pilot = (
        args.pilot_dir
        .expanduser()
        .resolve()
    )

    if not pilot.is_dir():
        raise FinalizeError(
            "source pilot "
            f"not found: {pilot}"
        )

    output = (
        args.output_dir
        .expanduser()
        .resolve()
        if args.output_dir
        is not None
        else None
    )

    if (
        args.action
        == "check"
    ):
        return do_check(
            fault_id,
            pilot,
        )

    if output is None:
        raise FinalizeError(
            "--output-dir is required "
            "for finalize/cleanup"
        )

    if (
        args.action
        == "finalize"
    ):
        return do_finalize(
            fault_id,
            pilot,
            output,
        )

    return do_cleanup(
        fault_id,
        pilot,
        output,
    )


if __name__ == "__main__":

    try:
        raise SystemExit(
            main()
        )

    except FinalizeError as exc:

        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )

        raise SystemExit(2)
