#!/usr/bin/env python3
"""Generate Stage-6 Round 1 or 2 under the frozen three-generation budget.

Routing:

GOLDEN_FALSE_POSITIVE
    -> GOLDEN_REPAIR.

TARGET_NOT_DETECTED before scope diagnosis
    -> consume round<next>_scope_feedback.json:

       BASE_EVIDENCE_FOUND
           -> LOCALIZED_BASE

       DOWNSTREAM_EVIDENCE_FOUND
           -> LOCALIZED_DOWNSTREAM

       NO_DISCRIMINATIVE_EVIDENCE
           -> SAME_CONTEXT_RETRY

TARGET_NOT_DETECTED after coarse localization
    -> exact counterexample at the SAME frozen scope:

       LOCALIZED_BASE
           -> COUNTEREXAMPLE_BASE

       LOCALIZED_DOWNSTREAM
           -> COUNTEREXAMPLE_DOWNSTREAM

TARGET_NOT_DETECTED after SAME_CONTEXT_RETRY
    -> SAME_CONTEXT_RETRY again.

Round number, feedback type, and observation scope are separate variables.
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
    "GOLDEN_REPAIR",
    "LOCALIZED_BASE",
    "LOCALIZED_DOWNSTREAM",
    "SAME_CONTEXT_RETRY",
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


def extract_property(
    response_text: str,
    previous_property: str,
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

    if (
        body
        == previous_property.strip()
    ):

        raise WorkflowError(
            "new property is identical "
            "to previous property; "
            "this API attempt is not "
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

    if (
        "training_observation"
        in context
    ):

        result[
            "training_observation"
        ] = copy.deepcopy(
            context[
                "training_observation"
            ]
        )

    if (
        "diagnostic_feedback"
        in context
    ):

        result[
            "diagnostic_feedback"
        ] = copy.deepcopy(
            context[
                "diagnostic_feedback"
            ]
        )

    if (
        "workflow_feedback"
        in context
    ):

        result[
            "workflow_feedback"
        ] = copy.deepcopy(
            context[
                "workflow_feedback"
            ]
        )

    if (
        "diagnostic_scope"
        in context
    ):

        result[
            "diagnostic_scope"
        ] = copy.deepcopy(
            context[
                "diagnostic_scope"
            ]
        )

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


def previous_property(
    pilot_dir: Path,
    previous_round: int,
) -> str:

    path = (
        pilot_dir
        / (
            f"round{previous_round}"
            "_property.sva"
        )
    )

    if (
        not path.is_file()
        or path.stat().st_size
        == 0
    ):

        raise WorkflowError(
            "previous property "
            f"missing/empty: {path}"
        )

    return (
        path.read_text(
            encoding="utf-8"
        )
        .strip()
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
                "The previous property "
                "was violated during valid "
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

    internal[
        "previous_round"
    ] = copy.deepcopy(
        previous
    )

    model[
        "previous_round"
    ] = copy.deepcopy(
        previous
    )

    internal[
        "workflow_feedback"
    ] = copy.deepcopy(
        feedback
    )

    model[
        "workflow_feedback"
    ] = copy.deepcopy(
        feedback
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
    scope: Mapping[str, Any],
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
    scope: Mapping[str, Any],
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

    selected = scope.get(
        "selected"
    )

    if not isinstance(
        selected,
        dict,
    ):

        raise WorkflowError(
            "DOWNSTREAM scope "
            "has no selected record"
        )

    expression = selected.get(
        "internal_signal"
    )

    expanded = selected.get(
        "expanded_golden_behavior"
    )

    depth = scope.get(
        "scope_depth"
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
            "before downstream localization"
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


def same_context_retry(
    previous_internal:
        Mapping[str, Any],
    previous_model:
        Mapping[str, Any],
    previous_round_index: int,
    previous_property_body: str,
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
            "SAME_CONTEXT_RETRY",

        "meaning":
            (
                "The previous property "
                "was Golden-safe but did not "
                "detect the target fault. "
                "Generate a different property "
                "using the same available "
                "evidence context."
            ),

        "new_signal_information":
            False,

        "new_target_fault_information":
            False,
    }

    internal[
        "previous_round"
    ] = copy.deepcopy(
        previous
    )

    model[
        "previous_round"
    ] = copy.deepcopy(
        previous
    )

    internal[
        "workflow_feedback"
    ] = copy.deepcopy(
        feedback
    )

    model[
        "workflow_feedback"
    ] = copy.deepcopy(
        feedback
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
    scope: Mapping[str, Any],
    scope_kind: str,
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

    exact = scope.get(
        "exact_evidence"
    )

    if scope_kind == "BASE":

        base = scope.get(
            "base_analysis"
        )

        if not isinstance(
            base,
            dict,
        ):

            raise WorkflowError(
                "BASE scope has "
                "no base_analysis"
            )

        exact = base.get(
            "exact_evidence"
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

    internal[
        "previous_round"
    ] = copy.deepcopy(
        previous
    )

    model[
        "previous_round"
    ] = copy.deepcopy(
        previous
    )

    internal[
        "diagnostic_feedback"
    ] = copy.deepcopy(
        diagnostic
    )

    model[
        "diagnostic_feedback"
    ] = copy.deepcopy(
        diagnostic
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

        scope_depth = (
            existing_scope[
                0
            ].get(
                "scope_depth"
            )
            if existing_scope
            else None
        )

        return (
            "GOLDEN_REPAIR",
            internal,
            model,
            scope_path,
            scope_kind,
            (
                scope_depth
                if isinstance(
                    scope_depth,
                    int,
                )
                else None
            ),
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
        else (
            str(
                previous_meta.get(
                    "feedback_type"
                )
            )
            if previous_meta
            else ""
        )
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
        )

        depth = scope.get(
            "scope_depth"
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
            (
                int(
                    depth
                )
                if isinstance(
                    depth,
                    int,
                )
                else None
            ),
        )

    if (
        previous_feedback
        == "SAME_CONTEXT_RETRY"
    ):

        (
            internal,
            model,
        ) = same_context_retry(
            previous_internal,
            previous_model,
            previous_round_index,
            previous_property_body,
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
            "SAME_CONTEXT_RETRY",
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

    status = scope.get(
        "status"
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
        )

        depth = scope.get(
            "scope_depth"
        )

        return (
            "LOCALIZED_DOWNSTREAM",
            internal,
            model,
            str(
                scope_path
            ),
            "DOWNSTREAM",
            (
                int(
                    depth
                )
                if isinstance(
                    depth,
                    int,
                )
                else None
            ),
        )

    if (
        status
        == "NO_DISCRIMINATIVE_EVIDENCE"
    ):

        (
            internal,
            model,
        ) = same_context_retry(
            previous_internal,
            previous_model,
            previous_round_index,
            previous_property_body,
        )

        return (
            "SAME_CONTEXT_RETRY",
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

Do not use raw hierarchy names, raw implementation net names, absolute cycle
numbers, or simulation time.

Do not emit clock/reset syntax, assert property, modules, semicolons,
explanations, or Markdown.

Useful SVA knowledge:
{knowledge.strip()}
"""

    specific = {
        "GOLDEN_REPAIR":
            """
FEEDBACK TYPE: GOLDEN_REPAIR
The previous property produced a false positive during valid fault-free Golden
execution. Re-read the same available Golden context and revise the property.
No new target-fault evidence is introduced in this round.
""",

        "LOCALIZED_BASE":
            """
FEEDBACK TYPE: LOCALIZED_BASE
The previous property was Golden-safe but did not detect the target fault.
Deterministic simulator analysis established that the CURRENT observation set
already contains supported fault-specific behavior. Do not move the observation
scope. Evidence types are provided, but exact faulty states/transitions are
hidden. Infer a different property using the existing aliases and Golden
behavior.
""",

        "LOCALIZED_DOWNSTREAM":
            """
FEEDBACK TYPE: LOCALIZED_DOWNSTREAM
The previous property was Golden-safe but did not detect the target fault.
The current base observation set contained no supported discriminative evidence,
so the shallowest bounded downstream observation with such evidence was added.
Use the expanded Golden behavior and the coarse evidence type. Exact faulty
states/transitions and exact divergence values remain hidden.
""",

        "SAME_CONTEXT_RETRY":
            """
FEEDBACK TYPE: SAME_CONTEXT_RETRY
The previous property was Golden-safe but did not detect the target fault.
No additional model-visible signal or exact target-fault evidence is introduced.
Generate a DIFFERENT property using the same available evidence context.
""",

        "COUNTEREXAMPLE_BASE":
            """
FEEDBACK TYPE: COUNTEREXAMPLE_BASE
The previous property was Golden-safe but still missed the target after coarse
BASE localization. Exact target-fault counterexample evidence for the SAME BASE
scope is now provided in diagnostic_feedback. Preserve Golden safety and do not
expand the observation scope.
""",

        "COUNTEREXAMPLE_DOWNSTREAM":
            """
FEEDBACK TYPE: COUNTEREXAMPLE_DOWNSTREAM
The previous property was Golden-safe but still missed the target after coarse
downstream localization. Exact target-fault counterexample evidence for the
SAME frozen downstream scope is now provided in diagnostic_feedback. Preserve
Golden safety and do not change the observation scope.
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
        + (
            "\n\nReturn exactly:\n\n"
            "BEGIN_SVA\n"
            "<one property expression body>\n"
            "END_SVA\n"
        )
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

    input_tokens = usage.get(
        "input_tokens"
    )

    output_tokens = usage.get(
        "output_tokens"
    )

    total_tokens = usage.get(
        "total_tokens"
    )

    cached = (
        usage.get(
            "input_tokens_details",
            {},
        ).get(
            "cached_tokens"
        )
        if isinstance(
            usage.get(
                "input_tokens_details"
            ),
            dict,
        )
        else None
    )

    reasoning = (
        usage.get(
            "output_tokens_details",
            {},
        ).get(
            "reasoning_tokens"
        )
        if isinstance(
            usage.get(
                "output_tokens_details"
            ),
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
        previous_property(
            pilot_dir,
            previous_round_index,
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
            "2.0",

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

    usage = response_payload.get(
        "usage"
    )

    write_json(
        outputs[
            "api_status"
        ],
        {
            "schema_version":
                "2.0",

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

            "recorded_at_utc":
                utc_now(),
        },
    )

    if not response_text:

        raise WorkflowError(
            "OpenAI response "
            "contained no output_text"
        )

    property_body = (
        extract_property(
            response_text,
            previous_property_body,
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
            "2.0",

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

        "generation_consumes_budget":
            True,

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
    print("=" * 88)

    print(
        f"Stage-6 generation "
        f"Round {next_round}: PASS"
    )

    print("=" * 88)

    print(
        f"Fault ID         : "
        f"{fault_id}"
    )

    print(
        f"Previous verdict : "
        f"{previous_verdict}"
    )

    print(
        f"Feedback type    : "
        f"{feedback_type}"
    )

    print(
        f"Observation scope: "
        f"{observation_scope}"
    )

    print(
        f"Scope depth      : "
        f"{scope_depth}"
    )

    print(
        f"Generation budget: "
        f"{next_round + 1}/3"
    )

    print(
        f"Property         : "
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
