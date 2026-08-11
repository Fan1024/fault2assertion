#!/usr/bin/env python3
"""Finalize one terminal Stage-6 fault into the compact durable v3 schema.

Durable files:
  fault_result.json
  roundN_property.sva for every generated round
  failure.log only for infrastructure/unexpected failures

Scientific terminal conditions:
  * TARGET_DETECTED occurs, or
  * Round 2 simulation completed (3/3 API generations consumed), or
  * an infrastructure execution verdict makes continuation invalid.

NO_DISCRIMINATIVE_EVIDENCE is not terminal before Round 2. It routes to
TARGET_MISS_REPAIR, which keeps the same evidence context but explicitly gives
the model this fault's prior properties and their scientific verdicts.
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
from typing import Any, Mapping


SCHEMA_VERSION = (
    "stage6_fault_result_v3"
)

MAX_ROUND = 2

FAULT_RE = re.compile(
    r"^TF\d{6}_SA[01]$"
)

VALID_FEEDBACK = {
    "NONE",
    "GOLDEN_REPAIR",
    "LOCALIZED_BASE",
    "LOCALIZED_DOWNSTREAM",
    "TARGET_MISS_REPAIR",
    "COUNTEREXAMPLE_BASE",
    "COUNTEREXAMPLE_DOWNSTREAM",
}

CONTINUABLE_VERDICTS = {
    "GOLDEN_FALSE_POSITIVE",
    "TARGET_NOT_DETECTED",
}

INFRA_VERDICTS = {
    "COMPILE_FAILED",
    "GOLDEN_EXECUTION_FAILED",
    "FAULT_EXECUTION_FAILED",
}

VERDICT_CODE = {
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

FEEDBACK_CODE = {
    "NONE":
        "NONE",

    "GOLDEN_REPAIR":
        "GR",

    "LOCALIZED_BASE":
        "LB",

    "LOCALIZED_DOWNSTREAM":
        "LD",

    "TARGET_MISS_REPAIR":
        "TMR",

    "COUNTEREXAMPLE_BASE":
        "CEB",

    "COUNTEREXAMPLE_DOWNSTREAM":
        "CED",
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
            f"{label} not found: "
            f"{path}"
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
    payload: Mapping[str, Any],
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


def sha256(
    path: Path,
) -> str:

    digest = hashlib.sha256()

    with path.open(
        "rb"
    ) as handle:

        for block in iter(
            lambda:
                handle.read(
                    1024 * 1024
                ),
            b"",
        ):

            digest.update(
                block
            )

    return digest.hexdigest()


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

    value = str(
        meta.get(
            "feedback_type",
            "",
        )
    )

    if (
        value
        not in
        VALID_FEEDBACK
    ):

        raise FinalizeError(
            f"invalid Round-{round_index} "
            f"feedback_type: {value!r}"
        )

    return value


def generation_meta(
    pilot: Path,
    round_index: int,
) -> dict[str, Any] | None:

    if round_index == 0:
        return None

    path = (
        pilot
        / (
            f"round{round_index}"
            "_generation_meta.json"
        )
    )

    if not path.is_file():
        return None

    return load_json(
        path,
        (
            f"Round-{round_index} "
            "generation metadata"
        ),
    )


def api_summary(
    pilot: Path,
    round_index: int,
) -> dict[str, Any] | None:

    path = (
        pilot
        / (
            f"round{round_index}"
            "_api_status.json"
        )
    )

    if not path.is_file():
        return None

    payload = load_json(
        path,
        (
            f"Round-{round_index} "
            "API status"
        ),
    )

    result: dict[
        str,
        Any,
    ] = {}

    for (
        source,
        dest,
    ) in (
        (
            "model_requested",
            "model",
        ),
        (
            "response_id",
            "response_id",
        ),
        (
            "response_status",
            "status",
        ),
    ):

        if (
            payload.get(
                source
            )
            is not None
        ):

            result[
                dest
            ] = payload[
                source
            ]

    if isinstance(
        payload.get(
            "usage"
        ),
        dict,
    ):

        result[
            "usage"
        ] = copy.deepcopy(
            payload[
                "usage"
            ]
        )

    if isinstance(
        payload.get(
            "conversation_linkage"
        ),
        dict,
    ):

        result[
            "conversation_linkage"
        ] = copy.deepcopy(
            payload[
                "conversation_linkage"
            ]
        )

    return (
        result
        or None
    )


def usage_metrics(
    usage:
        Mapping[str, Any],
) -> dict[str, int]:

    result: dict[
        str,
        int,
    ] = {}

    for key in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
    ):

        value = (
            usage.get(
                key
            )
        )

        if isinstance(
            value,
            int,
        ):

            result[
                key
            ] = value

    input_details = (
        usage.get(
            "input_tokens_details"
        )
    )

    if isinstance(
        input_details,
        dict,
    ):

        cached = (
            input_details.get(
                "cached_tokens"
            )
        )

        if isinstance(
            cached,
            int,
        ):

            result[
                "cached_input_tokens"
            ] = cached

    output_details = (
        usage.get(
            "output_tokens_details"
        )
    )

    if isinstance(
        output_details,
        dict,
    ):

        reasoning = (
            output_details.get(
                "reasoning_tokens"
            )
        )

        if isinstance(
            reasoning,
            int,
        ):

            result[
                "reasoning_tokens"
            ] = reasoning

    return result


def total_usage(
    rounds:
        list[dict[str, Any]],
) -> dict[str, int] | None:

    total: dict[
        str,
        int,
    ] = {}

    for item in rounds:

        api = (
            item.get(
                "api"
            )
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

        for (
            key,
            value,
        ) in usage_metrics(
            usage
        ).items():

            total[
                key
            ] = (
                total.get(
                    key,
                    0,
                )
                + value
            )

    return (
        total
        or None
    )


def simulation_summary(
    simulation:
        Mapping[str, Any],
) -> dict[str, Any]:

    def runner(
        name: str,
    ) -> Any:

        value = (
            simulation.get(
                name
            )
        )

        return (
            value.get(
                "runner_status"
            )
            if isinstance(
                value,
                dict,
            )
            else None
        )

    result: dict[
        str,
        Any,
    ] = {
        "verdict":
            simulation.get(
                "verdict"
            ),

        "compile_status":
            runner(
                "compile"
            ),

        "golden_status":
            runner(
                "golden"
            ),

        "faulty_status":
            runner(
                "faulty"
            ),
    }

    verdict = (
        simulation.get(
            "verdict"
        )
    )

    phase = (
        "golden"
        if verdict
        == "GOLDEN_FALSE_POSITIVE"

        else "faulty"
        if verdict
        == "TARGET_DETECTED"

        else None
    )

    if phase:

        raw = (
            simulation.get(
                phase
            )
        )

        event = (
            raw.get(
                "generated_assertion"
            )
            if isinstance(
                raw,
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


def model_input_delta(
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

        golden = (
            visible.get(
                "golden_behavior"
            )
        )

        if not isinstance(
            golden,
            dict,
        ):

            raise FinalizeError(
                "Round-0 visible context "
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

        if isinstance(
            visible.get(
                "training_observation"
            ),
            dict,
        ):

            result[
                "training_observation"
            ] = copy.deepcopy(
                visible[
                    "training_observation"
                ]
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

    previous = (
        model.get(
            "previous_round"
        )
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

    history = (
        model.get(
            "attempt_history"
        )
    )

    history_rounds: list[
        int
    ] = []

    if isinstance(
        history,
        list,
    ):

        for item in history:

            if (
                isinstance(
                    item,
                    dict,
                )
                and isinstance(
                    item.get(
                        "round"
                    ),
                    int,
                )
            ):

                history_rounds.append(
                    item[
                        "round"
                    ]
                )

    result: dict[
        str,
        Any,
    ] = {
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

        "attempt_history_included":
            bool(
                history_rounds
            ),

        "attempt_history_rounds":
            history_rounds,
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
        }

        return result

    if (
        feedback
        == "TARGET_MISS_REPAIR"
    ):

        result[
            "new_information"
        ] = {
            "prior_failed_properties_and_verdicts_provided":
                True,

            "new_signal_information":
                False,

            "new_golden_information":
                False,

            "new_target_fault_information":
                False,

            "exact_counterexample_provided":
                False,
        }

        return result

    diagnostic = (
        model.get(
            "diagnostic_feedback"
        )
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
        == "LOCALIZED_BASE"
    ):

        result[
            "new_information"
        ] = {
            "scope":
                "BASE",

            "evidence_types":
                copy.deepcopy(
                    diagnostic.get(
                        "evidence_types",
                        [],
                    )
                ),
        }

        return result

    if (
        feedback
        == "LOCALIZED_DOWNSTREAM"
    ):

        golden = (
            model.get(
                "golden_behavior"
            )
        )

        if not isinstance(
            golden,
            dict,
        ):

            raise FinalizeError(
                "LOCALIZED_DOWNSTREAM "
                "has no expanded "
                "golden_behavior"
            )

        result[
            "new_information"
        ] = {
            "scope":
                "DOWNSTREAM",

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
        in {
            "COUNTEREXAMPLE_BASE",
            "COUNTEREXAMPLE_DOWNSTREAM",
        }
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

        p_exists = (
            property_path.is_file()
        )

        s_exists = (
            simulation_path.is_file()
        )

        if (
            not p_exists
            and not s_exists
        ):

            gap_seen = True
            continue

        if gap_seen:

            raise FinalizeError(
                "non-contiguous artifacts "
                f"at Round {round_index}"
            )

        if (
            not p_exists
            or not s_exists
        ):

            raise FinalizeError(
                f"Round-{round_index} "
                "incomplete: property/"
                "simulation pair required"
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

        meta = generation_meta(
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
                model_input_delta(
                    pilot,
                    round_index,
                    feedback,
                ),

            "simulation":
                simulation_summary(
                    simulation
                ),
        }

        if meta:

            if (
                meta.get(
                    "observation_scope"
                )
                is not None
            ):

                record[
                    "observation_scope"
                ] = meta[
                    "observation_scope"
                ]

            if (
                meta.get(
                    "scope_depth"
                )
                is not None
            ):

                record[
                    "scope_depth"
                ] = meta[
                    "scope_depth"
                ]

            if isinstance(
                meta.get(
                    "attempt_history_rounds"
                ),
                list,
            ):

                record[
                    "attempt_history_rounds"
                ] = copy.deepcopy(
                    meta[
                        "attempt_history_rounds"
                    ]
                )

            if isinstance(
                meta.get(
                    "conversation_linkage"
                ),
                dict,
            ):

                record[
                    "conversation_linkage"
                ] = copy.deepcopy(
                    meta[
                        "conversation_linkage"
                    ]
                )

        api = api_summary(
            pilot,
            round_index,
        )

        if api:

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


def terminal_state(
    rounds:
        list[dict[str, Any]],
    simulations:
        list[dict[str, Any]],
) -> dict[str, Any]:

    for (
        index,
        simulation,
    ) in enumerate(
        simulations
    ):

        verdict = str(
            simulation.get(
                "verdict",
                "",
            )
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

        if (
            verdict
            in INFRA_VERDICTS
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

        if (
            verdict
            not in
            CONTINUABLE_VERDICTS
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
                    f"terminal {verdict!r}"
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
        rounds[
            -1
        ][
            "round"
        ]
    )

    last_verdict = str(
        simulations[
            -1
        ].get(
            "verdict",
            "",
        )
    )

    if (
        last_round
        == MAX_ROUND
    ):

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


def trajectory(
    rounds:
        list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    str,
]:

    records: list[
        dict[str, Any]
    ] = []

    codes: list[
        str
    ] = []

    for item in rounds:

        verdict = str(
            item[
                "simulation"
            ].get(
                "verdict"
            )
        )

        feedback = str(
            item[
                "feedback_type"
            ]
        )

        record: dict[
            str,
            Any,
        ] = {
            "round":
                item[
                    "round"
                ],

            "feedback_type":
                feedback,

            "verdict":
                verdict,
        }

        if (
            item.get(
                "observation_scope"
            )
            is not None
        ):

            record[
                "observation_scope"
            ] = item[
                "observation_scope"
            ]

        if (
            item.get(
                "scope_depth"
            )
            is not None
        ):

            record[
                "scope_depth"
            ] = item[
                "scope_depth"
            ]

        records.append(
            record
        )

        codes.append(
            f"R{item['round']}:"
            f"{FEEDBACK_CODE.get(feedback, feedback)}:"
            f"{VERDICT_CODE.get(verdict, verdict)}"
        )

    return (
        records,
        " -> ".join(
            codes
        ),
    )


def diagnostic_scope(
    pilot: Path,
    rounds:
        list[dict[str, Any]],
) -> dict[str, Any] | None:

    referenced: list[
        Path
    ] = []

    for item in rounds:

        round_index = int(
            item[
                "round"
            ]
        )

        if round_index == 0:
            continue

        meta = generation_meta(
            pilot,
            round_index,
        )

        if not meta:
            continue

        value = (
            meta.get(
                "scope_feedback"
            )
        )

        if (
            isinstance(
                value,
                str,
            )
            and value.strip()
        ):

            path = (
                Path(
                    value
                )
                .expanduser()
                .resolve()
            )

            if (
                path
                not in referenced
            ):

                referenced.append(
                    path
                )

    if not referenced:
        return None

    if (
        len(
            referenced
        )
        != 1
    ):

        raise FinalizeError(
            "multiple frozen scope "
            "records referenced: "
            f"{referenced}"
        )

    scope = load_json(
        referenced[
            0
        ],
        "frozen scope record",
    )

    decision = (
        scope.get(
            "scope_decision"
        )
    )

    result: dict[
        str,
        Any,
    ] = {
        "status":
            scope.get(
                "status"
            ),

        "decision":
            decision,

        "depth":
            scope.get(
                "scope_depth"
            ),

        "evidence_types":
            copy.deepcopy(
                scope.get(
                    "evidence_types",
                    [],
                )
            ),
    }

    if (
        decision
        == "BASE"
    ):

        base = (
            scope.get(
                "base_analysis"
            )
        )

        exact = (
            base.get(
                "exact_evidence"
            )
            if isinstance(
                base,
                dict,
            )
            else None
        )

        if isinstance(
            exact,
            dict,
        ):

            result[
                "exact_evidence"
            ] = copy.deepcopy(
                exact
            )

    elif (
        decision
        == "DOWNSTREAM"
    ):

        selected = (
            scope.get(
                "selected"
            )
        )

        if isinstance(
            selected,
            dict,
        ):

            result[
                "selected_alias"
            ] = (
                selected.get(
                    "alias"
                )
            )

            result[
                "selected_internal_signal"
            ] = (
                selected.get(
                    "internal_signal"
                )
            )

        exact = (
            scope.get(
                "exact_evidence"
            )
        )

        if isinstance(
            exact,
            dict,
        ):

            result[
                "exact_evidence"
            ] = copy.deepcopy(
                exact
            )

    elif (
        decision
        == "NONE"
    ):

        result[
            "exact_evidence"
        ] = None

    return result


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
        traj,
        traj_code,
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
            traj,

        "trajectory_code":
            traj_code,

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

    scope = diagnostic_scope(
        pilot,
        rounds,
    )

    if scope is not None:

        result[
            "diagnostic_scope"
        ] = scope

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

    rounds = (
        result.get(
            "rounds"
        )
    )

    if (
        not isinstance(
            rounds,
            list,
        )
        or not rounds
    ):

        raise FinalizeError(
            "durable result has no rounds"
        )

    for item in rounds:

        path = (
            output
            / str(
                item.get(
                    "property_file"
                )
            )
        )

        if (
            not path.is_file()
            or sha256(
                path
            )
            != item.get(
                "property_sha256"
            )
        ):

            raise FinalizeError(
                "durable Round-"
                f"{item.get('round')} "
                "property verification failed"
            )

    if expected is not None:

        actual = json.dumps(
            result,
            sort_keys=True,
            separators=(
                ",",
                ":",
            ),
            ensure_ascii=False,
        )

        wanted = json.dumps(
            expected,
            sort_keys=True,
            separators=(
                ",",
                ":",
            ),
            ensure_ascii=False,
        )

        if actual != wanted:

            raise FinalizeError(
                "durable fault_result.json "
                "differs from "
                "source-derived result"
            )

    return result


def copy_failure_log(
    pilot: Path,
    temp: Path,
    result:
        Mapping[str, Any],
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


def print_summary(
    result:
        Mapping[str, Any],
) -> None:

    usage = (
        result.get(
            "api_usage_total"
        )
        or {}
    )

    scope = (
        result.get(
            "diagnostic_scope"
        )
        or {}
    )

    print(
        f"Fault ID        : "
        f"{result['fault_id']}"
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
        f"Scope decision  : "
        f"{scope.get('decision')}"
    )

    print(
        f"Scope depth     : "
        f"{scope.get('depth')}"
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

    print(
        "API tokens      : "
        f"input={usage.get('input_tokens', 0)} "
        f"output={usage.get('output_tokens', 0)} "
        f"reasoning={usage.get('reasoning_tokens', 0)} "
        f"cached={usage.get('cached_input_tokens', 0)} "
        f"total={usage.get('total_tokens', 0)}"
    )


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

    print("=" * 96)

    print(
        "Stage-6 finalization "
        "check: TERMINAL"
    )

    print("=" * 96)

    print_summary(
        result
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
            "not be inside source pilot"
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

            name = (
                f"round{item['round']}"
                "_property.sva"
            )

            shutil.copy2(
                pilot
                / name,
                temp
                / name,
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

    print("=" * 96)

    print(
        "Stage-6 finalization: PASS"
    )

    print("=" * 96)

    print_summary(
        result
    )

    print(
        f"Durable output  : "
        f"{output}"
    )

    print(
        "Source cleanup  : "
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
            "refusing unsafe cleanup "
            f"path: {pilot}"
        )

    shutil.rmtree(
        pilot
    )

    print("=" * 96)

    print(
        "Stage-6 source cleanup: PASS"
    )

    print("=" * 96)

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
