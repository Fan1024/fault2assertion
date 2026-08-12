#!/usr/bin/env python3
"""Generate Stage-6 Round 1 or 2 under the frozen three-generation budget.

Method:
    Generate first -> Verify -> only after a Golden-safe TARGET_NOT_DETECTED
    diagnose observation sufficiency -> refine at the shallowest sufficient
    frozen scope.

Routing:

GOLDEN_FALSE_POSITIVE
    -> GOLDEN_REPAIR.

First TARGET_NOT_DETECTED after the current scope has not yet been diagnosed
    -> consume round<next>_scope_feedback.json:

       BASE_EVIDENCE_FOUND
           -> LOCALIZED_BASE

       DOWNSTREAM_EVIDENCE_FOUND
           -> LOCALIZED_DOWNSTREAM

       NO_DISCRIMINATIVE_EVIDENCE
           -> TARGET_MISS_REPAIR

TARGET_NOT_DETECTED after coarse localization
    -> exact counterexample at the SAME frozen scope:

       LOCALIZED_BASE
           -> COUNTEREXAMPLE_BASE

       LOCALIZED_DOWNSTREAM
           -> COUNTEREXAMPLE_DOWNSTREAM

TARGET_NOT_DETECTED after TARGET_MISS_REPAIR
    -> TARGET_MISS_REPAIR again, using the same evidence context.

Every feedback round receives fault-local attempt_history containing all earlier
properties and their scientific verdicts. No request uses previous_response_id
or a shared conversation object; every API request is explicitly constructed
from this fault's own artifacts.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


BEGIN_MARKER = "BEGIN_SVA"
END_MARKER = "END_SVA"

FAULT_ID_RE = re.compile(
    r"^TF\d{6}_SA[01]$"
)

VALID_FEEDBACK = {
    "COMPILE_REPAIR",
    "GOLDEN_REPAIR",
    "LOCALIZED_BASE",
    "LOCALIZED_DOWNSTREAM",
    "TARGET_MISS_REPAIR",
    "COUNTEREXAMPLE_BASE",
    "COUNTEREXAMPLE_DOWNSTREAM",
}


class WorkflowError(RuntimeError):
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

        raise WorkflowError(
            f"{label} not found: "
            f"{path}"
        ) from exc

    except json.JSONDecodeError as exc:

        raise WorkflowError(
            f"invalid {label} JSON "
            f"{path}: {exc}"
        ) from exc

    if not isinstance(
        value,
        dict,
    ):

        raise WorkflowError(
            f"{label} must contain "
            f"one JSON object: {path}"
        )

    return value


def write_json(
    path: Path,
    payload: Mapping[str, Any],
) -> None:

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

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


def write_text(
    path: Path,
    text: str,
) -> None:

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp = path.with_name(
        f".{path.name}.tmp"
    )

    tmp.write_text(
        text,
        encoding="utf-8",
    )

    tmp.replace(
        path
    )


def sha256_file(
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


def parse_env_file(
    path: Path,
) -> dict[str, str]:

    if not path.is_file():

        raise WorkflowError(
            "credential file not found: "
            f"{path}"
        )

    result: dict[
        str,
        str,
    ] = {}

    for (
        line_number,
        raw,
    ) in enumerate(
        path.read_text(
            encoding="utf-8"
        ).splitlines(),
        start=1,
    ):

        line = raw.strip()

        if (
            not line
            or line.startswith(
                "#"
            )
        ):
            continue

        if line.startswith(
            "export "
        ):

            line = (
                line[
                    7:
                ]
                .strip()
            )

        if "=" not in line:

            raise WorkflowError(
                "invalid credential line "
                f"{line_number}: {path}"
            )

        (
            key,
            value,
        ) = line.split(
            "=",
            1,
        )

        key = key.strip()
        value = value.strip()

        if (
            len(value) >= 2
            and value[0]
            == value[-1]
            and value[0]
            in {
                "'",
                '"',
            }
        ):

            value = value[
                1:-1
            ]

        result[
            key
        ] = value

    return result


def normalize_property(
    text: str,
) -> str:

    return " ".join(
        text.strip().split()
    )


def extract_property(
    response_text: str,
    prior_properties: list[str],
) -> str:

    if (
        response_text.count(
            BEGIN_MARKER
        )
        != 1
        or response_text.count(
            END_MARKER
        )
        != 1
    ):

        raise WorkflowError(
            "response must contain "
            "exactly one BEGIN_SVA "
            "and END_SVA"
        )

    marker_start = (
        response_text.index(
            BEGIN_MARKER
        )
    )

    body_start = (
        marker_start
        + len(
            BEGIN_MARKER
        )
    )

    body_end = (
        response_text.index(
            END_MARKER,
            body_start,
        )
    )

    if (
        response_text[
            :marker_start
        ].strip()
        or response_text[
            body_end
            + len(
                END_MARKER
            ):
        ].strip()
    ):

        raise WorkflowError(
            "response contains text "
            "outside BEGIN_SVA/END_SVA"
        )

    body = response_text[
        body_start:
        body_end
    ].strip()

    if not body:

        raise WorkflowError(
            "generated property is empty"
        )

    lowered = body.lower()

    if (
        "assert property"
        in lowered
        or "endmodule"
        in lowered
        or re.search(
            r"\bmodule\b",
            lowered,
        )
    ):

        raise WorkflowError(
            "model emitted "
            "wrapper/module syntax"
        )

    if body.endswith(
        ";"
    ):

        raise WorkflowError(
            "property body must not "
            "end with semicolon"
        )

    normalized = (
        normalize_property(
            body
        )
    )

    prior_normalized = {
        normalize_property(
            item
        )
        for item
        in prior_properties
    }

    if (
        normalized
        in prior_normalized
    ):

        raise WorkflowError(
            "new property repeats a "
            "previously generated property; "
            "this API attempt still consumes "
            "its generation round and is not "
            "eligible for a free retry"
        )

    return body


def core_context(
    context: Mapping[str, Any],
) -> dict[str, Any]:

    required = (
        "design",
        "workload",
        "fault",
        "signals",
        "golden_behavior",
    )

    for key in required:

        if key not in context:

            raise WorkflowError(
                "context missing required "
                f"field {key!r}"
            )

    result: dict[
        str,
        Any,
    ] = {
        "design":
            copy.deepcopy(
                context[
                    "design"
                ]
            ),

        "workload":
            copy.deepcopy(
                context[
                    "workload"
                ]
            ),

        "fault":
            copy.deepcopy(
                context[
                    "fault"
                ]
            ),

        "signals":
            copy.deepcopy(
                context[
                    "signals"
                ]
            ),

        "golden_behavior":
            copy.deepcopy(
                context[
                    "golden_behavior"
                ]
            ),
    }

    for key in (
        "training_observation",
        "diagnostic_feedback",
        "workflow_feedback",
        "diagnostic_scope",
    ):

        if key in context:

            result[
                key
            ] = copy.deepcopy(
                context[
                    key
                ]
            )

    # attempt_history is deliberately
    # rebuilt from THIS fault's artifacts
    # before every API call.
    return result


def previous_contexts(
    pilot_dir: Path,
    previous_round: int,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
]:

    if previous_round == 0:

        visible = load_json(
            pilot_dir
            / "visible_context.json",
            "Round-0 visible context",
        )

        return (
            copy.deepcopy(
                visible
            ),

            copy.deepcopy(
                visible
            ),
        )

    internal = load_json(
        pilot_dir
        / (
            f"round{previous_round}"
            "_context.json"
        ),
        (
            f"Round-{previous_round} "
            "internal context"
        ),
    )

    model_path = (
        pilot_dir
        / (
            f"round{previous_round}"
            "_model_context.json"
        )
    )

    model = (
        load_json(
            model_path,
            (
                f"Round-{previous_round} "
                "model context"
            ),
        )
        if model_path.is_file()
        else copy.deepcopy(
            internal
        )
    )

    return (
        internal,
        model,
    )


def read_property(
    pilot_dir: Path,
    round_index: int,
) -> str:

    path = (
        pilot_dir
        / (
            f"round{round_index}"
            "_property.sva"
        )
    )

    if (
        not path.is_file()
        or path.stat().st_size
        == 0
    ):

        raise WorkflowError(
            f"Round-{round_index} "
            "property missing/empty: "
            f"{path}"
        )

    return (
        path.read_text(
            encoding="utf-8"
        )
        .strip()
    )


def build_attempt_history(
    pilot_dir: Path,
    through_round: int,
    fault_id: str,
) -> list[dict[str, Any]]:

    history: list[
        dict[str, Any]
    ] = []

    for round_index in range(
        through_round + 1
    ):

        property_body = read_property(
            pilot_dir,
            round_index,
        )

        simulation_path = (
            pilot_dir
            / (
                f"round{round_index}"
                "_simulation.json"
            )
        )

        simulation = load_json(
            simulation_path,
            (
                f"Round-{round_index} "
                "simulation"
            ),
        )

        sim_fault_id = (
            simulation.get(
                "fault_id"
            )
        )

        if (
            sim_fault_id
            is not None
            and sim_fault_id
            != fault_id
        ):

            raise WorkflowError(
                f"Round-{round_index} "
                "simulation fault_id mismatch: "
                f"expected {fault_id}, "
                f"got {sim_fault_id!r}"
            )

        verdict = (
            simulation.get(
                "verdict"
            )
        )

        if (
            not isinstance(
                verdict,
                str,
            )
            or not verdict
        ):

            raise WorkflowError(
                f"Round-{round_index} "
                "simulation has no "
                "scientific verdict"
            )

        history.append(
            {
                "round":
                    round_index,

                "property":
                    property_body,

                "verdict":
                    verdict,
            }
        )

    return history


def attach_attempt_history(
    internal: dict[str, Any],
    model: dict[str, Any],
    history:
        list[dict[str, Any]],
) -> None:

    internal[
        "attempt_history"
    ] = copy.deepcopy(
        history
    )

    model[
        "attempt_history"
    ] = copy.deepcopy(
        history
    )


def generation_meta(
    pilot_dir: Path,
    round_index: int,
) -> dict[str, Any] | None:

    path = (
        pilot_dir
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


def frozen_scope_meta(
    pilot_dir: Path,
    through_round: int,
) -> tuple[
    dict[str, Any],
    Path,
] | None:

    for round_index in range(
        1,
        through_round + 1,
    ):

        meta = generation_meta(
            pilot_dir,
            round_index,
        )

        if not meta:
            continue

        value = meta.get(
            "scope_feedback"
        )

        if (
            not isinstance(
                value,
                str,
            )
            or not value.strip()
        ):

            continue

        path = (
            Path(
                value
            )
            .expanduser()
            .resolve()
        )

        if not path.is_file():

            raise WorkflowError(
                "frozen scope feedback "
                f"missing: {path}"
            )

        return (
            meta,
            path,
        )

    return None


def load_scope_feedback(
    path: Path,
) -> dict[str, Any]:

    payload = load_json(
        path,
        "scope feedback",
    )

    if (
        payload.get(
            "stage"
        )
        != "stage_06_scope_diagnosis"
    ):

        raise WorkflowError(
            "invalid scope feedback "
            "stage: "
            f"{payload.get('stage')!r}"
        )

    if (
        payload.get(
            "status"
        )
        not in {
            "BASE_EVIDENCE_FOUND",
            "DOWNSTREAM_EVIDENCE_FOUND",
            "NO_DISCRIMINATIVE_EVIDENCE",
        }
    ):

        raise WorkflowError(
            "invalid scope feedback "
            "status: "
            f"{payload.get('status')!r}"
        )

    return payload


def build_golden_repair(
    previous_internal:
        Mapping[str, Any],
    previous_model:
        Mapping[str, Any],
    previous_round_index: int,
    previous_property_body: str,
    history:
        list[dict[str, Any]],
) -> tuple[
    dict[str, Any],
    dict[str, Any],
]:

    internal = core_context(
        previous_internal
    )

    model = core_context(
        previous_model
    )

    previous = {
        "round":
            previous_round_index,

        "property":
            previous_property_body,

        "verdict":
            "GOLDEN_FALSE_POSITIVE",
    }

    feedback = {
        "type":
            "GOLDEN_REPAIR",

        "meaning":
            (
                "The previous property was "
                "violated during valid "
                "fault-free Golden execution "
                "and is inconsistent with "
                "the already provided "
                "Golden behavior."
            ),

        "new_target_fault_information":
            False,

        "exact_golden_counterexample_provided":
            False,
    }

    for target in (
        internal,
        model,
    ):

        target[
            "previous_round"
        ] = copy.deepcopy(
            previous
        )

        target[
            "workflow_feedback"
        ] = copy.deepcopy(
            feedback
        )

    attach_attempt_history(
        internal,
        model,
        history,
    )

    return (
        internal,
        model,
    )


def compile_diagnostics(
    pilot_dir: Path,
    previous_round_index: int,
) -> dict[str, Any]:

    log_path = (
        pilot_dir
        / (
            f"round{previous_round_index}"
            "_compile.log"
        )
    )

    result_path = (
        pilot_dir
        / (
            f"round{previous_round_index}"
            "_compile.json"
        )
    )

    if not log_path.is_file():

        raise WorkflowError(
            "COMPILE_REPAIR requires "
            "the previous compile log: "
            f"{log_path}"
        )

    compile_result: dict[
        str,
        Any,
    ] = {}

    if result_path.is_file():

        compile_result = load_json(
            result_path,
            (
                f"Round-{previous_round_index} "
                "compile result"
            ),
        )

    raw_lines = (
        log_path.read_text(
            encoding="utf-8",
            errors="replace",
        )
        .splitlines()
    )

    checker_name = (
        f"round{previous_round_index}"
        "_checker.sv"
    )

    error_re = re.compile(
        r"(?:"
        r"\*E,"
        r"|\*F,"
        r"|\berror\b"
        r"|\bsyntax\b"
        r"|\billegal\b"
        r"|\bexpect(?:ed|ing)\b"
        r")",
        flags=re.IGNORECASE,
    )

    # Prefer diagnostics that explicitly
    # point to the generated Stage-6
    # checker/property.
    hit_indices = [
        index
        for (
            index,
            line,
        ) in enumerate(
            raw_lines
        )
        if (
            checker_name
            in line
            and error_re.search(
                line
            )
        )
    ]

    # Some Xcelium diagnostics print the
    # source line and tool message on
    # adjacent lines. Fall back to all
    # compiler-error markers if necessary.
    if not hit_indices:

        hit_indices = [
            index
            for (
                index,
                line,
            ) in enumerate(
                raw_lines
            )
            if error_re.search(
                line
            )
        ]

    selected_indices: list[
        int
    ] = []

    seen: set[
        int
    ] = set()

    for index in hit_indices:

        for candidate in range(
            max(
                0,
                index - 2,
            ),
            min(
                len(
                    raw_lines
                ),
                index + 4,
            ),
        ):

            if candidate in seen:
                continue

            seen.add(
                candidate
            )

            selected_indices.append(
                candidate
            )

    # Fail closed only if the log is
    # completely unusable. A bounded tail
    # is better compiler feedback than
    # silently discarding the failure.
    if not selected_indices:

        start = max(
            0,
            len(
                raw_lines
            )
            - 40,
        )

        selected_indices = list(
            range(
                start,
                len(
                    raw_lines
                ),
            )
        )

    selected_indices = (
        selected_indices[
            :80
        ]
    )

    pilot_string = str(
        pilot_dir
    )

    lines: list[
        str
    ] = []

    for index in selected_indices:

        line = (
            raw_lines[
                index
            ]
            .replace(
                pilot_string,
                "<STAGE6_WORK>",
            )
        )

        if len(
            line
        ) > 1200:

            line = (
                line[
                    :1200
                ]
                + " ..."
            )

        if line.strip():

            lines.append(
                line
            )

    if not lines:

        raise WorkflowError(
            "COMPILE_REPAIR could not "
            "extract any compiler "
            "diagnostic text"
        )

    return {
        "source_file":
            log_path.name,

        "checker_file":
            checker_name,

        "runner_status":
            compile_result.get(
                "status"
            ),

        "runner_reason":
            compile_result.get(
                "reason"
            ),

        "lines":
            lines,

        "line_count":
            len(
                lines
            ),
    }


def compile_repair(
    previous_internal:
        Mapping[str, Any],
    previous_model:
        Mapping[str, Any],
    previous_round_index: int,
    previous_property_body: str,
    history:
        list[dict[str, Any]],
    pilot_dir: Path,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
]:

    internal = core_context(
        previous_internal
    )

    model = core_context(
        previous_model
    )

    diagnostics = (
        compile_diagnostics(
            pilot_dir,
            previous_round_index,
        )
    )

    previous = {
        "round":
            previous_round_index,

        "property":
            previous_property_body,

        "verdict":
            "COMPILE_FAILED",
    }

    feedback = {
        "type":
            "COMPILE_REPAIR",

        "meaning":
            (
                "The previous generated "
                "property failed Stage-6 "
                "compile/elaboration. Repair "
                "the SVA property using the "
                "compiler diagnostics while "
                "preserving the same "
                "diagnostic objective and "
                "available observation scope."
            ),

        "compiler_diagnostics":
            diagnostics,

        "new_signal_information":
            False,

        "new_golden_information":
            False,

        "new_target_fault_information":
            False,

        "exact_counterexample_provided":
            False,
    }

    for target in (
        internal,
        model,
    ):

        target[
            "previous_round"
        ] = copy.deepcopy(
            previous
        )

        target[
            "workflow_feedback"
        ] = copy.deepcopy(
            feedback
        )

    attach_attempt_history(
        internal,
        model,
        history,
    )

    return (
        internal,
        model,
    )


def base_coarse_context(
    previous_internal:
        Mapping[str, Any],
    previous_model:
        Mapping[str, Any],
    previous_round_index: int,
    previous_property_body: str,
    scope:
        Mapping[str, Any],
    history:
        list[dict[str, Any]],
) -> tuple[
    dict[str, Any],
    dict[str, Any],
]:

    internal = core_context(
        previous_internal
    )

    model = core_context(
        previous_model
    )

    diagnostic_scope = {
        "decision":
            "BASE",

        "depth":
            0,

        "frozen":
            True,
    }

    diagnostic = {
        "level":
            "COARSE_LOCALIZATION",

        "scope":
            "BASE",

        "evidence_types":
            copy.deepcopy(
                scope.get(
                    "evidence_types",
                    [],
                )
            ),
    }

    previous = {
        "round":
            previous_round_index,

        "property":
            previous_property_body,

        "verdict":
            "TARGET_NOT_DETECTED",
    }

    for target in (
        internal,
        model,
    ):

        target[
            "previous_round"
        ] = copy.deepcopy(
            previous
        )

        target[
            "diagnostic_scope"
        ] = copy.deepcopy(
            diagnostic_scope
        )

        target[
            "diagnostic_feedback"
        ] = copy.deepcopy(
            diagnostic
        )

    attach_attempt_history(
        internal,
        model,
        history,
    )

    return (
        internal,
        model,
    )


def downstream_coarse_context(
    previous_internal:
        Mapping[str, Any],
    previous_model:
        Mapping[str, Any],
    previous_round_index: int,
    previous_property_body: str,
    scope:
        Mapping[str, Any],
    history:
        list[dict[str, Any]],
) -> tuple[
    dict[str, Any],
    dict[str, Any],
]:

    internal = core_context(
        previous_internal
    )

    model = core_context(
        previous_model
    )

    selected = (
        scope.get(
            "selected"
        )
    )

    if not isinstance(
        selected,
        dict,
    ):

        raise WorkflowError(
            "DOWNSTREAM scope has "
            "no selected record"
        )

    expression = (
        selected.get(
            "internal_signal"
        )
    )

    expanded = (
        selected.get(
            "expanded_golden_behavior"
        )
    )

    depth = (
        scope.get(
            "scope_depth"
        )
    )

    if (
        not isinstance(
            expression,
            str,
        )
        or not expression.strip()
    ):

        raise WorkflowError(
            "DOWNSTREAM selected "
            "internal signal missing"
        )

    if not isinstance(
        expanded,
        dict,
    ):

        raise WorkflowError(
            "DOWNSTREAM expanded "
            "Golden behavior missing"
        )

    if (
        not isinstance(
            depth,
            int,
        )
        or depth <= 0
    ):

        raise WorkflowError(
            "DOWNSTREAM scope "
            "depth invalid"
        )

    if (
        "down_0_i"
        in internal[
            "signals"
        ]
        or "down_0_i"
        in model[
            "signals"
        ]
    ):

        raise WorkflowError(
            "down_0_i already exists "
            "before downstream "
            "localization"
        )

    internal[
        "signals"
    ][
        "down_0_i"
    ] = {
        "role":
            "downstream_observation",

        "netlist_expression":
            expression.strip(),

        "downstream_depth":
            depth,
    }

    model[
        "signals"
    ][
        "down_0_i"
    ] = {
        "role":
            "downstream_observation",
    }

    expected_order = list(
        internal[
            "signals"
        ].keys()
    )

    if (
        expanded.get(
            "signal_order"
        )
        != expected_order
    ):

        raise WorkflowError(
            "expanded Golden behavior "
            "signal order mismatch: "
            f"expected={expected_order}, "
            "actual="
            f"{expanded.get('signal_order')!r}"
        )

    internal[
        "golden_behavior"
    ] = copy.deepcopy(
        expanded
    )

    model[
        "golden_behavior"
    ] = copy.deepcopy(
        expanded
    )

    diagnostic_scope = {
        "decision":
            "DOWNSTREAM",

        "depth":
            depth,

        "selected_alias":
            "down_0_i",

        "frozen":
            True,
    }

    diagnostic = {
        "level":
            "COARSE_LOCALIZATION",

        "scope":
            "DOWNSTREAM",

        "selected_observation_alias":
            "down_0_i",

        "evidence_types":
            copy.deepcopy(
                scope.get(
                    "evidence_types",
                    [],
                )
            ),
    }

    previous = {
        "round":
            previous_round_index,

        "property":
            previous_property_body,

        "verdict":
            "TARGET_NOT_DETECTED",
    }

    for target in (
        internal,
        model,
    ):

        target[
            "previous_round"
        ] = copy.deepcopy(
            previous
        )

        target[
            "diagnostic_scope"
        ] = copy.deepcopy(
            diagnostic_scope
        )

        target[
            "diagnostic_feedback"
        ] = copy.deepcopy(
            diagnostic
        )

    attach_attempt_history(
        internal,
        model,
        history,
    )

    serialized = json.dumps(
        model,
        sort_keys=True,
    )

    for token in (
        expression.strip(),
        "fault_only_observed_states",
        "fault_only_site_downstream_transitions",
        "divergence_values",
        "downstream_depth",
    ):

        if (
            token
            and token
            in serialized
        ):

            raise WorkflowError(
                "LOCALIZED_DOWNSTREAM "
                "leaked hidden token "
                f"{token!r}"
            )

    return (
        internal,
        model,
    )


def target_miss_repair(
    previous_internal:
        Mapping[str, Any],
    previous_model:
        Mapping[str, Any],
    previous_round_index: int,
    previous_property_body: str,
    history:
        list[dict[str, Any]],
) -> tuple[
    dict[str, Any],
    dict[str, Any],
]:

    internal = core_context(
        previous_internal
    )

    model = core_context(
        previous_model
    )

    previous = {
        "round":
            previous_round_index,

        "property":
            previous_property_body,

        "verdict":
            "TARGET_NOT_DETECTED",
    }

    feedback = {
        "type":
            "TARGET_MISS_REPAIR",

        "meaning":
            (
                "The previous property was "
                "valid on the fault-free "
                "Golden execution but did "
                "not trigger during the "
                "target faulty execution. "
                "It therefore failed the "
                "diagnostic objective for "
                "this target. Use the "
                "fault-local attempt history "
                "and generate a materially "
                "different property from the "
                "same available evidence "
                "context."
            ),

        "new_signal_information":
            False,

        "new_golden_information":
            False,

        "new_target_fault_information":
            False,

        "exact_counterexample_provided":
            False,
    }

    for target in (
        internal,
        model,
    ):

        target[
            "previous_round"
        ] = copy.deepcopy(
            previous
        )

        target[
            "workflow_feedback"
        ] = copy.deepcopy(
            feedback
        )

    attach_attempt_history(
        internal,
        model,
        history,
    )

    return (
        internal,
        model,
    )


def exact_counterexample(
    previous_internal:
        Mapping[str, Any],
    previous_model:
        Mapping[str, Any],
    previous_round_index: int,
    previous_property_body: str,
    scope:
        Mapping[str, Any],
    scope_kind: str,
    history:
        list[dict[str, Any]],
) -> tuple[
    dict[str, Any],
    dict[str, Any],
]:

    internal = core_context(
        previous_internal
    )

    model = core_context(
        previous_model
    )

    exact = (
        scope.get(
            "exact_evidence"
        )
    )

    if scope_kind == "BASE":

        base = (
            scope.get(
                "base_analysis"
            )
        )

        if not isinstance(
            base,
            dict,
        ):

            raise WorkflowError(
                "BASE scope has "
                "no base_analysis"
            )

        exact = (
            base.get(
                "exact_evidence"
            )
        )

    if not isinstance(
        exact,
        dict,
    ):

        raise WorkflowError(
            "frozen scope has "
            "no exact evidence"
        )

    previous = {
        "round":
            previous_round_index,

        "property":
            previous_property_body,

        "verdict":
            "TARGET_NOT_DETECTED",
    }

    diagnostic = {
        "level":
            "EXACT_COUNTEREXAMPLE",

        "scope":
            scope_kind,

        **copy.deepcopy(
            exact
        ),
    }

    for target in (
        internal,
        model,
    ):

        target[
            "previous_round"
        ] = copy.deepcopy(
            previous
        )

        target[
            "diagnostic_feedback"
        ] = copy.deepcopy(
            diagnostic
        )

    attach_attempt_history(
        internal,
        model,
        history,
    )

    serialized = json.dumps(
        model,
        sort_keys=True,
    ).lower()

    if (
        '"cycle"'
        in serialized
        or '"time"'
        in serialized
    ):

        raise WorkflowError(
            "counterexample context "
            "leaked absolute cycle/time"
        )

    return (
        internal,
        model,
    )


def choose_feedback(
    *,
    pilot_dir: Path,
    next_round: int,
    previous_verdict: str,
    previous_internal:
        Mapping[str, Any],
    previous_model:
        Mapping[str, Any],
    previous_property_body: str,
    history:
        list[dict[str, Any]],
) -> tuple[
    str,
    dict[str, Any],
    dict[str, Any],
    str | None,
    str | None,
    int | None,
]:

    previous_round_index = (
        next_round - 1
    )

    existing_scope = (
        frozen_scope_meta(
            pilot_dir,
            previous_round_index,
        )
    )

    if (
        previous_verdict
        == "COMPILE_FAILED"
    ):

        (
            internal,
            model,
        ) = compile_repair(
            previous_internal,
            previous_model,
            previous_round_index,
            previous_property_body,
            history,
            pilot_dir,
        )

        scope_path = (
            str(
                existing_scope[
                    1
                ]
            )
            if existing_scope
            else None
        )

        scope_kind = (
            str(
                existing_scope[
                    0
                ].get(
                    "observation_scope"
                )
            )
            if existing_scope
            else None
        )

        raw_depth = (
            existing_scope[
                0
            ].get(
                "scope_depth"
            )
            if existing_scope
            else None
        )

        scope_depth = (
            raw_depth
            if isinstance(
                raw_depth,
                int,
            )
            else None
        )

        return (
            "COMPILE_REPAIR",
            internal,
            model,
            scope_path,
            scope_kind,
            scope_depth,
        )

    if (
        previous_verdict
        == "GOLDEN_FALSE_POSITIVE"
    ):

        (
            internal,
            model,
        ) = build_golden_repair(
            previous_internal,
            previous_model,
            previous_round_index,
            previous_property_body,
            history,
        )

        scope_path = (
            str(
                existing_scope[
                    1
                ]
            )
            if existing_scope
            else None
        )

        scope_kind = (
            str(
                existing_scope[
                    0
                ].get(
                    "observation_scope"
                )
            )
            if existing_scope
            else None
        )

        raw_depth = (
            existing_scope[
                0
            ].get(
                "scope_depth"
            )
            if existing_scope
            else None
        )

        scope_depth = (
            raw_depth
            if isinstance(
                raw_depth,
                int,
            )
            else None
        )

        return (
            "GOLDEN_REPAIR",
            internal,
            model,
            scope_path,
            scope_kind,
            scope_depth,
        )

    if (
        previous_verdict
        != "TARGET_NOT_DETECTED"
    ):

        raise WorkflowError(
            "previous verdict is "
            "terminal/unsupported for "
            "next generation: "
            f"{previous_verdict!r}"
        )

    previous_meta = (
        generation_meta(
            pilot_dir,
            previous_round_index,
        )
    )

    previous_feedback = (
        "NONE"
        if previous_round_index == 0
        else str(
            previous_meta.get(
                "feedback_type"
            )
        )
        if previous_meta
        else ""
    )

    if (
        previous_feedback
        == "LOCALIZED_BASE"
    ):

        if not existing_scope:

            raise WorkflowError(
                "LOCALIZED_BASE has "
                "no frozen scope record"
            )

        scope = load_scope_feedback(
            existing_scope[
                1
            ]
        )

        (
            internal,
            model,
        ) = exact_counterexample(
            previous_internal,
            previous_model,
            previous_round_index,
            previous_property_body,
            scope,
            "BASE",
            history,
        )

        return (
            "COUNTEREXAMPLE_BASE",
            internal,
            model,
            str(
                existing_scope[
                    1
                ]
            ),
            "BASE",
            0,
        )

    if (
        previous_feedback
        == "LOCALIZED_DOWNSTREAM"
    ):

        if not existing_scope:

            raise WorkflowError(
                "LOCALIZED_DOWNSTREAM "
                "has no frozen scope record"
            )

        scope = load_scope_feedback(
            existing_scope[
                1
            ]
        )

        (
            internal,
            model,
        ) = exact_counterexample(
            previous_internal,
            previous_model,
            previous_round_index,
            previous_property_body,
            scope,
            "DOWNSTREAM",
            history,
        )

        depth = (
            scope.get(
                "scope_depth"
            )
        )

        return (
            "COUNTEREXAMPLE_DOWNSTREAM",
            internal,
            model,
            str(
                existing_scope[
                    1
                ]
            ),
            "DOWNSTREAM",
            int(
                depth
            )
            if isinstance(
                depth,
                int,
            )
            else None,
        )

    if (
        previous_feedback
        == "TARGET_MISS_REPAIR"
    ):

        (
            internal,
            model,
        ) = target_miss_repair(
            previous_internal,
            previous_model,
            previous_round_index,
            previous_property_body,
            history,
        )

        scope_path = (
            str(
                existing_scope[
                    1
                ]
            )
            if existing_scope
            else None
        )

        return (
            "TARGET_MISS_REPAIR",
            internal,
            model,
            scope_path,
            "NONE",
            None,
        )

    # First Golden-safe target miss.
    scope_path = (
        pilot_dir
        / (
            f"round{next_round}"
            "_scope_feedback.json"
        )
    )

    if not scope_path.is_file():

        raise WorkflowError(
            "first TARGET_NOT_DETECTED "
            "requires observation-scope "
            "diagnosis before Round "
            f"{next_round}; missing "
            f"{scope_path}"
        )

    scope = load_scope_feedback(
        scope_path
    )

    status = (
        scope.get(
            "status"
        )
    )

    if (
        status
        == "BASE_EVIDENCE_FOUND"
    ):

        (
            internal,
            model,
        ) = base_coarse_context(
            previous_internal,
            previous_model,
            previous_round_index,
            previous_property_body,
            scope,
            history,
        )

        return (
            "LOCALIZED_BASE",
            internal,
            model,
            str(
                scope_path
            ),
            "BASE",
            0,
        )

    if (
        status
        == "DOWNSTREAM_EVIDENCE_FOUND"
    ):

        (
            internal,
            model,
        ) = downstream_coarse_context(
            previous_internal,
            previous_model,
            previous_round_index,
            previous_property_body,
            scope,
            history,
        )

        depth = (
            scope.get(
                "scope_depth"
            )
        )

        return (
            "LOCALIZED_DOWNSTREAM",
            internal,
            model,
            str(
                scope_path
            ),
            "DOWNSTREAM",
            int(
                depth
            )
            if isinstance(
                depth,
                int,
            )
            else None,
        )

    if (
        status
        == "NO_DISCRIMINATIVE_EVIDENCE"
    ):

        (
            internal,
            model,
        ) = target_miss_repair(
            previous_internal,
            previous_model,
            previous_round_index,
            previous_property_body,
            history,
        )

        return (
            "TARGET_MISS_REPAIR",
            internal,
            model,
            str(
                scope_path
            ),
            "NONE",
            None,
        )

    raise WorkflowError(
        "unsupported scope status: "
        f"{status!r}"
    )


def render_prompt(
    round_index: int,
    feedback_type: str,
    knowledge: str,
    model_context:
        Mapping[str, Any],
) -> str:

    common = f"""Generate exactly one diagnostic SystemVerilog Assertion property body.

This is Stage-6 generation Round {round_index}. The maximum round index is 2.
Every API generation attempt consumes one round; there is no hidden retry.

Scientific objective:
- remain silent on valid fault-free Golden execution;
- detect the target faulty execution when possible;
- use only the aliases supplied in MODEL-VISIBLE CONTEXT.

Golden behavior in the context is valid observed workload behavior and must not
be contradicted.

ATTEMPT HISTORY is fault-local. It contains only properties previously generated
for THIS fault and their simulator-derived scientific verdicts. Treat a
TARGET_NOT_DETECTED property as Golden-safe but unsuccessful for the diagnostic
objective. Do not repeat any prior property and do not merely restate one with
syntactic changes.

Do not use raw hierarchy names, raw implementation net names, absolute cycle
numbers, or simulation time.

Do not emit clock/reset syntax, assert property, modules, semicolons,
explanations, or Markdown.

Useful SVA knowledge:
{knowledge.strip()}
"""

    specific = {
        "COMPILE_REPAIR":
            """
FEEDBACK TYPE: COMPILE_REPAIR
The immediately previous generated property failed Stage-6 compilation or
elaboration. Compiler diagnostics are provided in
workflow_feedback.compiler_diagnostics.

Repair the SystemVerilog Assertion syntax or property semantics that caused the
compiler failure. Preserve the same diagnostic objective, Golden constraints,
available aliases, and observation scope. A compiler failure is NOT evidence
about the target fault, so do not infer new faulty behavior from it.

Do not repeat the previous property. Produce a corrected property body that can
compile under the same Stage-6 checker.
""",

        "GOLDEN_REPAIR":
            """
FEEDBACK TYPE: GOLDEN_REPAIR
The immediately previous property produced a false positive during valid
fault-free Golden execution. Re-read the available Golden context and the
fault-local attempt history, then repair the property. No new target-fault
evidence is introduced in this round.
""",

        "LOCALIZED_BASE":
            """
FEEDBACK TYPE: LOCALIZED_BASE
The immediately previous property was Golden-safe but did not detect the target
fault. Deterministic simulator analysis established that the CURRENT observation
set already contains supported fault-specific behavior. Do not move the
observation scope. Evidence types are provided, but exact faulty
states/transitions are hidden. Use the attempt history to avoid previously
failed property strategies.
""",

        "LOCALIZED_DOWNSTREAM":
            """
FEEDBACK TYPE: LOCALIZED_DOWNSTREAM
The immediately previous property was Golden-safe but did not detect the target
fault. The current base observation set contained no supported discriminative
evidence, so the shallowest bounded downstream observation with such evidence
was added. Use the expanded Golden behavior and coarse evidence type. Exact
faulty states/transitions and exact divergence values remain hidden. Use the
attempt history to avoid previously failed property strategies.
""",

        "TARGET_MISS_REPAIR":
            """
FEEDBACK TYPE: TARGET_MISS_REPAIR
The immediately previous property was valid on the fault-free Golden execution
but did not trigger during the target faulty execution. It therefore failed the
diagnostic objective for this target fault.

No new Golden behavior, fault behavior, observation signal, or exact
counterexample is introduced in this round. Review ALL entries in
attempt_history. Generate a materially different property using the SAME
available evidence context. Do not repeat any prior property and do not produce
a merely syntactic restatement of a prior property.
""",

        "COUNTEREXAMPLE_BASE":
            """
FEEDBACK TYPE: COUNTEREXAMPLE_BASE
The previous property was Golden-safe but still missed the target after coarse
BASE localization. Exact target-fault counterexample evidence for the SAME BASE
scope is now provided in diagnostic_feedback. Preserve Golden safety, do not
expand the observation scope, and use attempt_history to avoid already failed
properties.
""",

        "COUNTEREXAMPLE_DOWNSTREAM":
            """
FEEDBACK TYPE: COUNTEREXAMPLE_DOWNSTREAM
The previous property was Golden-safe but still missed the target after coarse
downstream localization. Exact target-fault counterexample evidence for the
SAME frozen downstream scope is now provided in diagnostic_feedback. Preserve
Golden safety, do not change the observation scope, and use attempt_history to
avoid already failed properties.
""",
    }[
        feedback_type
    ]

    return (
        common
        + specific
        + "\nMODEL-VISIBLE CONTEXT\n"
        + json.dumps(
            model_context,
            indent=2,
            ensure_ascii=False,
        )
        + "\n\nReturn exactly:\n\n"
        + "BEGIN_SVA\n"
        + "<one property expression body>\n"
        + "END_SVA\n"
    )


def usage_line(
    usage: Any,
) -> str:

    if not isinstance(
        usage,
        dict,
    ):

        return (
            "API tokens      : unavailable"
        )

    input_tokens = (
        usage.get(
            "input_tokens"
        )
    )

    output_tokens = (
        usage.get(
            "output_tokens"
        )
    )

    total_tokens = (
        usage.get(
            "total_tokens"
        )
    )

    input_details = (
        usage.get(
            "input_tokens_details"
        )
    )

    cached = (
        input_details.get(
            "cached_tokens"
        )
        if isinstance(
            input_details,
            dict,
        )
        else None
    )

    output_details = (
        usage.get(
            "output_tokens_details"
        )
    )

    reasoning = (
        output_details.get(
            "reasoning_tokens"
        )
        if isinstance(
            output_details,
            dict,
        )
        else None
    )

    return (
        "API tokens      : "
        f"input={input_tokens} "
        f"output={output_tokens} "
        f"total={total_tokens} "
        f"cached={cached} "
        f"reasoning={reasoning}"
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
        "--next-round",
        required=True,
        type=int,
        choices=(
            1,
            2,
        ),
    )

    parser.add_argument(
        "--pilot-dir",
        type=Path,
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
        "--model-policy",
        type=Path,
        default=(
            root
            / "assertion_generation"
            / "model_policy.json"
        ),
    )

    parser.add_argument(
        "--sva-knowledge",
        type=Path,
        default=(
            root
            / "assertion_generation"
            / "sva_knowledge.md"
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

    fault_id = (
        args.fault_id
        .strip()
    )

    next_round = (
        args.next_round
    )

    previous_round_index = (
        next_round - 1
    )

    if (
        FAULT_ID_RE.fullmatch(
            fault_id
        )
        is None
    ):

        raise WorkflowError(
            f"invalid fault ID: "
            f"{fault_id!r}"
        )

    pilot_dir = (
        args.pilot_dir
        .expanduser()
        .resolve()
        if args.pilot_dir
        is not None
        else (
            root
            / "runs"
            / "stage6"
            / f"pilot_{fault_id}"
        ).resolve()
    )

    if not pilot_dir.is_dir():

        raise WorkflowError(
            "pilot directory "
            f"not found: {pilot_dir}"
        )

    previous_sim = load_json(
        pilot_dir
        / (
            f"round{previous_round_index}"
            "_simulation.json"
        ),
        (
            f"Round-{previous_round_index} "
            "simulation"
        ),
    )

    previous_verdict = str(
        previous_sim.get(
            "verdict",
            "",
        )
    )

    if (
        previous_verdict
        == "TARGET_DETECTED"
    ):

        raise WorkflowError(
            "previous round already "
            "TARGET_DETECTED; "
            "workflow must stop"
        )

    (
        previous_internal,
        previous_model,
    ) = previous_contexts(
        pilot_dir,
        previous_round_index,
    )

    previous_property_body = (
        read_property(
            pilot_dir,
            previous_round_index,
        )
    )

    history = (
        build_attempt_history(
            pilot_dir,
            previous_round_index,
            fault_id,
        )
    )

    (
        feedback_type,
        internal_context,
        model_context,
        scope_feedback,
        observation_scope,
        scope_depth,
    ) = choose_feedback(
        pilot_dir=
            pilot_dir,

        next_round=
            next_round,

        previous_verdict=
            previous_verdict,

        previous_internal=
            previous_internal,

        previous_model=
            previous_model,

        previous_property_body=
            previous_property_body,

        history=
            history,
    )

    if (
        feedback_type
        not in
        VALID_FEEDBACK
    ):

        raise WorkflowError(
            "invalid feedback type: "
            f"{feedback_type}"
        )

    if (
        model_context.get(
            "attempt_history"
        )
        != history
    ):

        raise WorkflowError(
            "fault-local attempt_history "
            "construction mismatch"
        )

    knowledge_path = (
        args.sva_knowledge
        .expanduser()
        .resolve()
    )

    if not knowledge_path.is_file():

        raise WorkflowError(
            "SVA knowledge file "
            f"not found: {knowledge_path}"
        )

    prompt = render_prompt(
        next_round,
        feedback_type,
        knowledge_path.read_text(
            encoding="utf-8"
        ),
        model_context,
    )

    prefix = (
        f"round{next_round}"
    )

    outputs = {
        "context":
            pilot_dir
            / f"{prefix}_context.json",

        "model_context":
            pilot_dir
            / (
                f"{prefix}"
                "_model_context.json"
            ),

        "prompt":
            pilot_dir
            / f"{prefix}_prompt.txt",

        "request":
            pilot_dir
            / f"{prefix}_request.json",

        "response":
            pilot_dir
            / f"{prefix}_response.json",

        "response_text":
            pilot_dir
            / f"{prefix}_response.txt",

        "api_status":
            pilot_dir
            / f"{prefix}_api_status.json",

        "property":
            pilot_dir
            / f"{prefix}_property.sva",

        "meta":
            pilot_dir
            / (
                f"{prefix}"
                "_generation_meta.json"
            ),
    }

    existing = [
        path
        for path
        in outputs.values()
        if path.exists()
    ]

    if existing:

        raise WorkflowError(
            "refusing to overwrite "
            "existing next-round "
            "artifacts:\n  "
            + "\n  ".join(
                str(
                    path
                )
                for path
                in existing
            )
        )

    write_json(
        outputs[
            "context"
        ],
        internal_context,
    )

    write_json(
        outputs[
            "model_context"
        ],
        model_context,
    )

    write_text(
        outputs[
            "prompt"
        ],
        prompt,
    )

    policy_path = (
        args.model_policy
        .expanduser()
        .resolve()
    )

    policy = load_json(
        policy_path,
        "model policy",
    )

    if (
        policy.get(
            "api"
        )
        != "responses"
    ):

        raise WorkflowError(
            "model policy must "
            "use api='responses'"
        )

    model = str(
        policy.get(
            "model",
            "",
        )
    ).strip()

    effort = str(
        policy.get(
            "reasoning_effort",
            "medium",
        )
    ).strip()

    max_tokens = int(
        policy.get(
            "max_output_tokens",
            32768,
        )
    )

    store = bool(
        policy.get(
            "store",
            False,
        )
    )

    if (
        not model
        or max_tokens <= 0
    ):

        raise WorkflowError(
            "invalid model policy"
        )

    credentials = parse_env_file(
        args.credential_file
        .expanduser()
        .resolve()
    )

    api_key = (
        credentials.get(
            "OPENAI_API_KEY",
            "",
        )
        .strip()
    )

    if (
        not api_key
        or api_key
        == "REPLACE_WITH_REAL_OPENAI_API_KEY"
    ):

        raise WorkflowError(
            "OPENAI_API_KEY "
            "missing or placeholder"
        )

    try:

        from openai import OpenAI

    except ImportError as exc:

        raise WorkflowError(
            "OpenAI Python SDK "
            "is not installed"
        ) from exc

    request_record = {
        "schema_version":
            "3.0",

        "stage":
            "stage_06_generation_attempt",

        "fault_id":
            fault_id,

        "round":
            next_round,

        "max_round":
            2,

        "previous_round":
            previous_round_index,

        "previous_verdict":
            previous_verdict,

        "feedback_type":
            feedback_type,

        "scope_feedback":
            scope_feedback,

        "observation_scope":
            observation_scope,

        "scope_depth":
            scope_depth,

        "attempt_history_rounds":
            [
                item[
                    "round"
                ]
                for item
                in history
            ],

        "conversation_linkage": {
            "previous_response_id_used":
                False,

            "conversation_object_used":
                False,

            "history_source":
                "fault_local_artifacts_only",
        },

        "model":
            model,

        "reasoning_effort":
            effort,

        "max_output_tokens":
            max_tokens,

        "store":
            store,

        "model_context_sha256":
            sha256_file(
                outputs[
                    "model_context"
                ]
            ),

        "prompt_sha256":
            sha256_file(
                outputs[
                    "prompt"
                ]
            ),

        "requested_at_utc":
            utc_now(),
    }

    client = OpenAI(
        api_key=
            api_key,

        timeout=
            300.0,

        max_retries=
            2,
    )

    # Intentionally independent request:
    #
    # no previous_response_id
    # no conversation
    #
    # Same-fault history is explicitly
    # serialized into this prompt only.
    response = (
        client.responses.create(
            model=
                model,

            input=
                prompt,

            reasoning={
                "effort":
                    effort
            },

            max_output_tokens=
                max_tokens,

            store=
                store,
        )
    )

    response_payload = (
        response.model_dump(
            mode="json"
        )
    )

    response_text = (
        response.output_text
        or ""
    ).strip()

    write_json(
        outputs[
            "request"
        ],
        request_record,
    )

    write_json(
        outputs[
            "response"
        ],
        response_payload,
    )

    write_text(
        outputs[
            "response_text"
        ],
        response_text
        + (
            "\n"
            if response_text
            else ""
        ),
    )

    usage = (
        response_payload.get(
            "usage"
        )
    )

    write_json(
        outputs[
            "api_status"
        ],
        {
            "schema_version":
                "3.0",

            "stage":
                "stage_06_api_status",

            "fault_id":
                fault_id,

            "round":
                next_round,

            "feedback_type":
                feedback_type,

            "observation_scope":
                observation_scope,

            "scope_depth":
                scope_depth,

            "model_requested":
                model,

            "model_returned":
                response_payload.get(
                    "model"
                ),

            "response_id":
                response_payload.get(
                    "id"
                ),

            "response_status":
                response_payload.get(
                    "status"
                ),

            "incomplete_details":
                response_payload.get(
                    "incomplete_details"
                ),

            "usage":
                usage,

            "nonempty_output_text":
                bool(
                    response_text
                ),

            "conversation_linkage": {
                "previous_response_id_used":
                    False,

                "conversation_object_used":
                    False,
            },

            "recorded_at_utc":
                utc_now(),
        },
    )

    if not response_text:

        raise WorkflowError(
            "OpenAI response "
            "contained no output_text"
        )

    prior_properties = [
        item[
            "property"
        ]
        for item
        in history
    ]

    property_body = (
        extract_property(
            response_text,
            prior_properties,
        )
    )

    write_text(
        outputs[
            "property"
        ],
        property_body
        + "\n",
    )

    meta = {
        "schema_version":
            "3.0",

        "stage":
            "stage_06_generation_metadata",

        "fault_id":
            fault_id,

        "round":
            next_round,

        "max_round":
            2,

        "previous_round":
            previous_round_index,

        "previous_verdict":
            previous_verdict,

        "feedback_type":
            feedback_type,

        "scope_feedback":
            scope_feedback,

        "observation_scope":
            observation_scope,

        "scope_depth":
            scope_depth,

        "attempt_history_rounds":
            [
                item[
                    "round"
                ]
                for item
                in history
            ],

        "generation_consumes_budget":
            True,

        "conversation_linkage": {
            "previous_response_id_used":
                False,

            "conversation_object_used":
                False,

            "history_source":
                "fault_local_artifacts_only",
        },

        "generated_at_utc":
            utc_now(),

        "model_context_sha256":
            sha256_file(
                outputs[
                    "model_context"
                ]
            ),

        "prompt_sha256":
            sha256_file(
                outputs[
                    "prompt"
                ]
            ),

        "property_sha256":
            sha256_file(
                outputs[
                    "property"
                ]
            ),
    }

    write_json(
        outputs[
            "meta"
        ],
        meta,
    )

    print()
    print("=" * 96)

    print(
        f"Stage-6 generation "
        f"Round {next_round}: PASS"
    )

    print("=" * 96)

    print(
        f"Fault ID          : "
        f"{fault_id}"
    )

    print(
        f"Previous verdict  : "
        f"{previous_verdict}"
    )

    print(
        f"Feedback type     : "
        f"{feedback_type}"
    )

    print(
        f"Observation scope : "
        f"{observation_scope}"
    )

    print(
        f"Scope depth       : "
        f"{scope_depth}"
    )

    print(
        "Attempt history   : "
        + ", ".join(
            f"R{x['round']}="
            f"{x['verdict']}"
            for x
            in history
        )
    )

    print(
        "Conversation link : "
        "NONE "
        "(fault-local explicit context only)"
    )

    print(
        f"Generation budget : "
        f"{next_round + 1}/3"
    )

    print(
        f"Property          : "
        f"{property_body}"
    )

    print(
        usage_line(
            usage
        )
    )

    return 0


if __name__ == "__main__":

    try:

        raise SystemExit(
            main()
        )

    except WorkflowError as exc:

        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )

        raise SystemExit(2)
