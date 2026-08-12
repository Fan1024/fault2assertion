#!/usr/bin/env python3
"""Generate Stage-6 Round-1 localized or Round-2 counterexample feedback.

Round 1 exposes:
- previous TARGET_NOT_DETECTED result and property;
- one selected downstream alias;
- expanded Golden behavior;
- deterministic diagnostic evidence TYPES only.

Round 2 exposes, only after Round-1 TARGET_NOT_DETECTED:
- everything in Round 1;
- exact fault-only state/transition examples;
- exact Golden/Fault divergence VALUES;
- never absolute cycle/time.

The full downstream analysis remains hidden teacher evidence. The script writes
both an internal simulation context and an exact model-visible context.
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


class FeedbackError(RuntimeError):
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
        raise FeedbackError(
            f"{label} not found: {path}"
        ) from exc

    except json.JSONDecodeError as exc:
        raise FeedbackError(
            f"invalid {label} JSON "
            f"{path}: {exc}"
        ) from exc

    if not isinstance(
        value,
        dict,
    ):
        raise FeedbackError(
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

    temporary = path.with_name(
        f".{path.name}.tmp"
    )

    temporary.write_text(
        json.dumps(
            payload,
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    temporary.replace(
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

    temporary = path.with_name(
        f".{path.name}.tmp"
    )

    temporary.write_text(
        text,
        encoding="utf-8",
    )

    temporary.replace(
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
            lambda: handle.read(
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
        raise FeedbackError(
            "credential file "
            f"not found: {path}"
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
            or line.startswith("#")
        ):
            continue

        if line.startswith(
            "export "
        ):
            line = line[7:].strip()

        if "=" not in line:
            raise FeedbackError(
                "invalid credential line "
                f"{line_number}: {path}"
            )

        key, value = line.split(
            "=",
            1,
        )

        key = key.strip()
        value = value.strip()

        if (
            len(value) >= 2
            and value[0] == value[-1]
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
        raise FeedbackError(
            "response must contain exactly "
            "one BEGIN_SVA and END_SVA"
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

    prefix = response_text[
        :marker_start
    ].strip()

    suffix = response_text[
        body_end
        + len(
            END_MARKER
        ):
    ].strip()

    if prefix or suffix:
        raise FeedbackError(
            "response contains text "
            "outside BEGIN_SVA/END_SVA"
        )

    body = response_text[
        body_start:body_end
    ].strip()

    if not body:
        raise FeedbackError(
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
        raise FeedbackError(
            "model emitted wrapper/module "
            "syntax instead of one "
            "property body"
        )

    if body.endswith(";"):
        raise FeedbackError(
            "property body must not "
            "end with a semicolon"
        )

    if (
        body
        == previous_property.strip()
    ):
        raise FeedbackError(
            "new property is identical "
            "to previous-round property"
        )

    return body


def evidence_types(
    selected: Mapping[str, Any],
) -> list[str]:

    types: list[str] = []

    states = selected.get(
        "candidate_added_fault_only_states",
        [],
    )

    transitions = selected.get(
        "fault_only_site_candidate_transitions",
        [],
    )

    if (
        not isinstance(
            states,
            list,
        )
        or not isinstance(
            transitions,
            list,
        )
    ):
        raise FeedbackError(
            "downstream selected evidence "
            "arrays are malformed"
        )

    if states:
        types.append(
            "SAME_CYCLE_JOINT_STATE_NOVELTY"
        )

    if transitions:
        types.append(
            "ONE_CYCLE_TRANSITION_NOVELTY"
        )

    if not types:
        raise FeedbackError(
            "selected downstream candidate "
            "has no diagnostic evidence type"
        )

    return types


def validate_expanded_behavior(
    behavior: Mapping[str, Any],
    aliases: list[str],
) -> None:

    if (
        behavior.get(
            "signal_order"
        )
        != aliases
    ):
        raise FeedbackError(
            "expanded Golden signal_order "
            "mismatch: "
            f"expected={aliases}, "
            "actual="
            f"{behavior.get('signal_order')!r}"
        )

    states = behavior.get(
        "observed_states"
    )

    if (
        not isinstance(
            states,
            list,
        )
        or not states
    ):
        raise FeedbackError(
            "expanded Golden observed_states "
            "is missing/empty"
        )

    width = len(
        aliases
    )

    for state in states:

        if (
            not isinstance(
                state,
                str,
            )
            or len(
                state
            )
            != width
            or any(
                bit not in "01"
                for bit in state
            )
        ):
            raise FeedbackError(
                "invalid expanded "
                "Golden state: "
                f"{state!r}"
            )

    transitions = behavior.get(
        "one_cycle_transitions"
    )

    if not isinstance(
        transitions,
        dict,
    ):
        raise FeedbackError(
            "expanded Golden "
            "one_cycle_transitions "
            "is missing"
        )


def render_template(
    template: str,
    knowledge: str,
    model_context: Mapping[str, Any],
) -> str:

    markers = (
        "{{SVA_KNOWLEDGE}}",
        "{{MODEL_CONTEXT_JSON}}",
    )

    for marker in markers:

        if template.count(
            marker
        ) != 1:
            raise FeedbackError(
                "prompt template must "
                "contain exactly one "
                f"{marker}"
            )

    context_text = json.dumps(
        model_context,
        indent=2,
        ensure_ascii=False,
    )

    prompt = template.replace(
        "{{SVA_KNOWLEDGE}}",
        knowledge.strip(),
    )

    prompt = prompt.replace(
        "{{MODEL_CONTEXT_JSON}}",
        context_text,
    )

    return (
        prompt.rstrip()
        + "\n"
    )


def build_round1(
    pilot_dir: Path,
    baseline: Mapping[str, Any],
    feedback: Mapping[str, Any],
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    str,
]:

    simulation = load_json(
        pilot_dir
        / "round0_simulation.json",
        "Round-0 simulation",
    )

    if (
        simulation.get(
            "verdict"
        )
        != "TARGET_NOT_DETECTED"
    ):
        raise FeedbackError(
            "Round 1 requires Round-0 "
            "TARGET_NOT_DETECTED; got "
            f"{simulation.get('verdict')!r}"
        )

    previous_path = (
        pilot_dir
        / "round0_property.sva"
    )

    if not previous_path.is_file():
        raise FeedbackError(
            "Round-0 property missing: "
            f"{previous_path}"
        )

    previous_property = (
        previous_path
        .read_text(
            encoding="utf-8"
        )
        .strip()
    )

    selected = feedback.get(
        "selected"
    )

    if (
        feedback.get(
            "status"
        )
        != "DOWNSTREAM_CANDIDATE_FOUND"
        or not isinstance(
            selected,
            dict,
        )
    ):
        raise FeedbackError(
            "Round 1 requires "
            "DOWNSTREAM_CANDIDATE_FOUND; "
            "got "
            f"{feedback.get('status')!r}"
        )

    expression = selected.get(
        "expression"
    )

    depth = selected.get(
        "depth"
    )

    expanded_behavior = selected.get(
        "expanded_golden_behavior"
    )

    if (
        not isinstance(
            expression,
            str,
        )
        or not expression.strip()
    ):
        raise FeedbackError(
            "selected downstream "
            "expression is missing"
        )

    if not isinstance(
        depth,
        int,
    ):
        raise FeedbackError(
            "selected downstream "
            "depth is invalid"
        )

    if not isinstance(
        expanded_behavior,
        dict,
    ):
        raise FeedbackError(
            "selected expanded "
            "Golden behavior is missing"
        )

    base_signals = baseline.get(
        "signals"
    )

    if (
        not isinstance(
            base_signals,
            dict,
        )
        or "site_i"
        not in base_signals
    ):
        raise FeedbackError(
            "baseline visible_context "
            "signals are missing"
        )

    internal_signals = (
        copy.deepcopy(
            base_signals
        )
    )

    if "down_0_i" in internal_signals:
        raise FeedbackError(
            "baseline unexpectedly "
            "already contains down_0_i"
        )

    internal_signals[
        "down_0_i"
    ] = {
        "role":
            "downstream_observation",

        "netlist_expression":
            expression.strip(),

        "downstream_depth":
            depth,
    }

    aliases = list(
        internal_signals.keys()
    )

    validate_expanded_behavior(
        expanded_behavior,
        aliases,
    )

    types = evidence_types(
        selected
    )

    internal_context = {
        "design":
            baseline.get(
                "design"
            ),

        "workload":
            baseline.get(
                "workload"
            ),

        "fault":
            copy.deepcopy(
                baseline.get(
                    "fault"
                )
            ),

        "signals":
            internal_signals,

        "golden_behavior":
            copy.deepcopy(
                expanded_behavior
            ),

        "training_observation":
            copy.deepcopy(
                baseline.get(
                    "training_observation"
                )
            ),

        "previous_round": {
            "round":
                0,

            "previous_property":
                previous_property,

            "verdict":
                "TARGET_NOT_DETECTED",
        },

        "diagnostic_feedback": {
            "level":
                "LOCALIZED",

            "selected_observation_alias":
                "down_0_i",

            "evidence_types":
                types,
        },
    }

    model_context = (
        copy.deepcopy(
            internal_context
        )
    )

    # The simulator still needs the actual net
    # name/depth in round1_context.json.
    #
    # The LLM does not.
    model_context[
        "signals"
    ][
        "down_0_i"
    ] = {
        "role":
            "downstream_observation"
    }

    # Strong-fault evidence must never
    # accidentally enter Round 1.
    serialized = json.dumps(
        model_context,
        sort_keys=True,
    )

    forbidden_tokens = [
        "candidate_added_fault_only_states",
        "fault_only_site_candidate_transitions",
        "earliest_divergence",
        "golden_values",
        "fault_values",
    ]

    for token in forbidden_tokens:

        if token in serialized:
            raise FeedbackError(
                "Round-1 model context "
                "leaked strong token "
                f"{token!r}"
            )

    return (
        internal_context,
        model_context,
        previous_property,
    )


def build_round2(
    pilot_dir: Path,
    feedback: Mapping[str, Any],
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    str,
]:

    simulation = load_json(
        pilot_dir
        / "round1_simulation.json",
        "Round-1 simulation",
    )

    verdict = simulation.get(
        "verdict"
    )

    if (
        verdict
        != "TARGET_NOT_DETECTED"
    ):
        raise FeedbackError(
            "Round 2 is intentionally "
            "allowed only after "
            "Round-1 TARGET_NOT_DETECTED; "
            f"got {verdict!r}. "
            "Stop on TARGET_DETECTED or "
            "GOLDEN_FALSE_POSITIVE."
        )

    previous_path = (
        pilot_dir
        / "round1_property.sva"
    )

    if not previous_path.is_file():
        raise FeedbackError(
            "Round-1 property missing: "
            f"{previous_path}"
        )

    previous_property = (
        previous_path
        .read_text(
            encoding="utf-8"
        )
        .strip()
    )

    round1_internal = load_json(
        pilot_dir
        / "round1_context.json",
        "Round-1 internal context",
    )

    selected = feedback.get(
        "selected"
    )

    if (
        feedback.get(
            "status"
        )
        != "DOWNSTREAM_CANDIDATE_FOUND"
        or not isinstance(
            selected,
            dict,
        )
    ):
        raise FeedbackError(
            "Round 2 requires the same "
            "validated downstream "
            "teacher record"
        )

    types = evidence_types(
        selected
    )

    states = selected.get(
        "candidate_added_fault_only_states",
        [],
    )

    transitions = selected.get(
        "fault_only_site_candidate_transitions",
        [],
    )

    earliest = selected.get(
        "earliest_divergence"
    )

    exact_feedback: dict[
        str,
        Any,
    ] = {
        "level":
            "COUNTEREXAMPLE",

        "selected_observation_alias":
            "down_0_i",

        "evidence_types":
            types,

        "fault_only_observed_states":
            list(
                states
            ),

        "fault_only_site_downstream_transitions":
            list(
                transitions
            ),
    }

    # Copy ONLY values.
    # Never copy cycle or time.
    if isinstance(
        earliest,
        dict,
    ):

        golden_values = (
            earliest.get(
                "golden_values"
            )
        )

        fault_values = (
            earliest.get(
                "fault_values"
            )
        )

        if (
            isinstance(
                golden_values,
                dict,
            )
            and isinstance(
                fault_values,
                dict,
            )
        ):
            exact_feedback[
                "divergence_sample"
            ] = {
                "golden_values":
                    copy.deepcopy(
                        golden_values
                    ),

                "fault_values":
                    copy.deepcopy(
                        fault_values
                    ),
            }

    internal_context = {
        "design":
            round1_internal.get(
                "design"
            ),

        "workload":
            round1_internal.get(
                "workload"
            ),

        "fault":
            copy.deepcopy(
                round1_internal.get(
                    "fault"
                )
            ),

        "signals":
            copy.deepcopy(
                round1_internal.get(
                    "signals"
                )
            ),

        "golden_behavior":
            copy.deepcopy(
                round1_internal.get(
                    "golden_behavior"
                )
            ),

        "training_observation":
            copy.deepcopy(
                round1_internal.get(
                    "training_observation"
                )
            ),

        "previous_round": {
            "round":
                1,

            "previous_property":
                previous_property,

            "verdict":
                "TARGET_NOT_DETECTED",
        },

        "diagnostic_feedback":
            exact_feedback,
    }

    model_context = (
        copy.deepcopy(
            internal_context
        )
    )

    # Again, raw implementation identity and
    # structural depth are simulator-internal.
    if (
        isinstance(
            model_context.get(
                "signals"
            ),
            dict,
        )
        and "down_0_i"
        in model_context[
            "signals"
        ]
    ):
        model_context[
            "signals"
        ][
            "down_0_i"
        ] = {
            "role":
                "downstream_observation"
        }

    serialized = json.dumps(
        model_context,
        sort_keys=True,
    ).lower()

    if (
        '"cycle"'
        in serialized
        or '"time"'
        in serialized
        or "downstream_depth"
        in serialized
    ):
        raise FeedbackError(
            "Round-2 model context "
            "leaked cycle/time/depth metadata"
        )

    return (
        internal_context,
        model_context,
        previous_property,
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
        "--round",
        type=int,
        choices=(
            1,
            2,
        ),
        required=True,
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

    parser.add_argument(
        "--round1-template",
        type=Path,
        default=(
            root
            / "assertion_generation"
            / "prompt_round1_localized.txt"
        ),
    )

    parser.add_argument(
        "--round2-template",
        type=Path,
        default=(
            root
            / "assertion_generation"
            / "prompt_round2_counterexample.txt"
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
        args.fault_id.strip()
    )

    if (
        FAULT_ID_RE.fullmatch(
            fault_id
        )
        is None
    ):
        raise FeedbackError(
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
        raise FeedbackError(
            "pilot directory "
            f"not found: {pilot_dir}"
        )

    baseline = load_json(
        pilot_dir
        / "visible_context.json",
        "Round-0 visible context",
    )

    feedback_path = (
        pilot_dir
        / "round1_downstream_feedback.json"
    )

    feedback = load_json(
        feedback_path,
        "hidden downstream teacher record",
    )

    if args.round == 1:

        (
            internal_context,
            model_context,
            previous_property,
        ) = build_round1(
            pilot_dir,
            baseline,
            feedback,
        )

        template_path = (
            args.round1_template
            .expanduser()
            .resolve()
        )

        feedback_name = (
            "LOCALIZED_V1"
        )

    else:

        (
            internal_context,
            model_context,
            previous_property,
        ) = build_round2(
            pilot_dir,
            feedback,
        )

        template_path = (
            args.round2_template
            .expanduser()
            .resolve()
        )

        feedback_name = (
            "COUNTEREXAMPLE_V1"
        )

    knowledge_path = (
        args.sva_knowledge
        .expanduser()
        .resolve()
    )

    if not template_path.is_file():
        raise FeedbackError(
            "prompt template "
            f"not found: {template_path}"
        )

    if not knowledge_path.is_file():
        raise FeedbackError(
            "SVA knowledge "
            f"not found: {knowledge_path}"
        )

    prompt = render_template(
        template_path.read_text(
            encoding="utf-8"
        ),
        knowledge_path.read_text(
            encoding="utf-8"
        ),
        model_context,
    )

    prefix = (
        f"round{args.round}"
    )

    outputs = {
        "context":
            pilot_dir
            / f"{prefix}_context.json",

        "model_context":
            pilot_dir
            / f"{prefix}_model_context.json",

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
    }

    existing = [
        path
        for path in outputs.values()
        if path.exists()
    ]

    if existing:
        raise FeedbackError(
            "refusing to overwrite "
            "existing feedback-round "
            "artifacts:\n  "
            + "\n  ".join(
                str(path)
                for path in existing
            )
        )

    # Internal context is consumed by Xcelium.
    write_json(
        outputs[
            "context"
        ],
        internal_context,
    )

    # Exact auditable content shown to the LLM.
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
        raise FeedbackError(
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
        raise FeedbackError(
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
        raise FeedbackError(
            "OPENAI_API_KEY missing "
            "or placeholder"
        )

    try:
        from openai import OpenAI

    except ImportError as exc:
        raise FeedbackError(
            "OpenAI Python SDK "
            "is not installed"
        ) from exc

    request_record = {
        "schema_version":
            "1.0",

        "stage":
            "stage_06_feedback_generation",

        "fault_id":
            fault_id,

        "round":
            args.round,

        "feedback_level":
            feedback_name,

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

        "hidden_teacher_sha256":
            sha256_file(
                feedback_path
            ),

        "requested_at_utc":
            utc_now(),
    }

    client = OpenAI(
        api_key=api_key,
        timeout=300.0,
        max_retries=2,
    )

    response = (
        client.responses.create(
            model=model,

            input=prompt,

            reasoning={
                "effort":
                    effort
            },

            max_output_tokens=
                max_tokens,

            store=store,
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
        (
            response_text
            + "\n"
            if response_text
            else ""
        ),
    )

    write_json(
        outputs[
            "api_status"
        ],
        {
            "schema_version":
                "1.0",

            "stage":
                "stage_06_feedback_api_status",

            "fault_id":
                fault_id,

            "round":
                args.round,

            "feedback_level":
                feedback_name,

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
                response_payload.get(
                    "usage"
                ),

            "nonempty_output_text":
                bool(
                    response_text
                ),

            "recorded_at_utc":
                utc_now(),
        },
    )

    if not response_text:
        raise FeedbackError(
            "OpenAI response "
            "contained no output_text"
        )

    property_body = extract_property(
        response_text,
        previous_property,
    )

    write_text(
        outputs[
            "property"
        ],
        property_body + "\n",
    )

    print()
    print("=" * 80)

    print(
        f"Stage-6 Round-"
        f"{args.round} generation: PASS"
    )

    print("=" * 80)

    print(
        f"Fault ID       : "
        f"{fault_id}"
    )

    print(
        f"Feedback level : "
        f"{feedback_name}"
    )

    print(
        f"Model          : "
        f"{model}"
    )

    print(
        f"Property       : "
        f"{property_body}"
    )

    print(
        f"Model context  : "
        f"{outputs['model_context']}"
    )

    print(
        f"Prompt         : "
        f"{outputs['prompt']}"
    )

    return 0


if __name__ == "__main__":

    try:
        raise SystemExit(
            main()
        )

    except FeedbackError as exc:

        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )

        raise SystemExit(2)
