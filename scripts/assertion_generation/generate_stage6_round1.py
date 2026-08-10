#!/usr/bin/env python3
"""Generate Stage-6 Train Round-1 from bounded downstream fault feedback."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


BEGIN_MARKER = "BEGIN_SVA"
END_MARKER = "END_SVA"


class GenerationError(RuntimeError):
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
        raise GenerationError(
            f"{label} not found: {path}"
        ) from exc

    except json.JSONDecodeError as exc:
        raise GenerationError(
            f"invalid {label} JSON "
            f"{path}: {exc}"
        ) from exc

    if not isinstance(
        value,
        dict,
    ):
        raise GenerationError(
            f"{label} must contain "
            f"one JSON object: {path}"
        )

    return value


def write_json(
    path: Path,
    payload: Mapping[str, Any],
) -> None:

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

    with path.open("rb") as handle:

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
        raise GenerationError(
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
            or line.startswith(
                "#"
            )
        ):
            continue

        if line.startswith(
            "export "
        ):
            line = (
                line[7:]
                .strip()
            )

        if "=" not in line:
            raise GenerationError(
                "invalid credential "
                f"line {line_number}: "
                f"{path}"
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
    text: str,
    previous: str,
) -> str:

    if (
        text.count(
            BEGIN_MARKER
        )
        != 1
        or text.count(
            END_MARKER
        )
        != 1
    ):
        raise GenerationError(
            "response must contain "
            "exactly one BEGIN_SVA "
            "and END_SVA"
        )

    begin_marker = (
        text.index(
            BEGIN_MARKER
        )
    )

    begin = (
        begin_marker
        + len(
            BEGIN_MARKER
        )
    )

    end = text.index(
        END_MARKER,
        begin,
    )

    if (
        text[
            :begin_marker
        ].strip()
        or text[
            end
            + len(
                END_MARKER
            ):
        ].strip()
    ):
        raise GenerationError(
            "response contains text "
            "outside "
            "BEGIN_SVA/END_SVA"
        )

    body = text[
        begin:end
    ].strip()

    if not body:
        raise GenerationError(
            "generated property "
            "is empty"
        )

    if (
        "assert property"
        in body.lower()
    ):
        raise GenerationError(
            "only a property body "
            "is allowed"
        )

    if body.endswith(
        ";"
    ):
        raise GenerationError(
            "property body must not "
            "end with a semicolon"
        )

    if (
        body
        == previous.strip()
    ):
        raise GenerationError(
            "Round-1 property "
            "is identical to "
            "Round-0"
        )

    return body


def build_prompt(
    context: Mapping[str, Any],
) -> str:

    context_text = json.dumps(
        context,
        indent=2,
        ensure_ascii=False,
    )

    return f"""Generate one repaired diagnostic SystemVerilog Assertion property body for the
known structural fault below.

This is Train Round 1. Round 0 passed the complete fault-free Golden CRC32
execution but did not trigger on the target faulty execution.

The context now includes one additional downstream alias selected by a bounded
fault-propagation analysis. This target-fault information is privileged training
feedback; use it to repair the Round-0 property.

Interpret the behavior fields as follows:
- `golden_behavior.signal_order` defines the bit order of every Golden state;
- every listed Golden state was observed and must not be rejected;
- for `one_cycle_transitions.<alias>`, each two-bit state is ordered as
  `[site_i, <alias>]`;
- `AB->CD` means consecutive sampled-clock states;
- `fault_only_observed_states` were observed in the target faulty execution but
  were not observed in the Golden execution for the expanded signal order;
- `fault_only_site_downstream_transitions` were observed for
  `[site_i, down_0_i]` in the target faulty execution but not in Golden;
- `first_divergence_sample` is a value snapshot, not permission to use an
  absolute simulation cycle number.

Use only the supplied aliases: `site_i`, `recv_N_i`, and `down_0_i`.
The new property must remain consistent with all supplied Golden behavior and
should exploit the downstream fault feedback when useful.

Useful SVA constructs:
- `|->` overlapped implication
- `|=>` next-cycle implication
- `##N` exact cycle delay
- `$past`, `$stable`, `$rose`, `$fell`

Generate only one property-expression body.
Do not emit clock/reset syntax, `assert property`, modules, semicolons, raw
hierarchy names, or absolute simulation cycle numbers.

ROUND-1 CONTEXT
{context_text}

Return exactly:

BEGIN_SVA
<one property expression body>
END_SVA
"""


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
        raise GenerationError(
            "pilot directory "
            f"not found: {pilot_dir}"
        )

    round0_sim = load_json(
        pilot_dir
        / "round0_simulation.json",
        "Round-0 simulation",
    )

    if (
        round0_sim.get(
            "verdict"
        )
        != "TARGET_NOT_DETECTED"
    ):
        raise GenerationError(
            "Round-1 downstream repair "
            "requires "
            "TARGET_NOT_DETECTED; got "
            f"{round0_sim.get('verdict')!r}"
        )

    feedback_path = (
        pilot_dir
        / "round1_downstream_feedback.json"
    )

    feedback = load_json(
        feedback_path,
        "downstream feedback",
    )

    if (
        feedback.get(
            "status"
        )
        != "DOWNSTREAM_CANDIDATE_FOUND"
    ):
        raise GenerationError(
            "no usable downstream "
            "candidate: status="
            f"{feedback.get('status')!r}"
        )

    selected = feedback.get(
        "selected"
    )

    if not isinstance(
        selected,
        dict,
    ):
        raise GenerationError(
            "downstream feedback "
            "has no selected candidate"
        )

    visible = load_json(
        pilot_dir
        / "visible_context.json",
        "baseline visible context",
    )

    signals = visible.get(
        "signals"
    )

    if not isinstance(
        signals,
        dict,
    ):
        raise GenerationError(
            "baseline visible context "
            "has no signals"
        )

    expanded_signals = dict(
        signals
    )

    expanded_signals[
        "down_0_i"
    ] = {
        "role":
            "train_feedback_downstream_observation",

        "netlist_expression":
            selected.get(
                "expression"
            ),

        "downstream_depth":
            selected.get(
                "depth"
            ),
    }

    golden_behavior = (
        selected.get(
            "expanded_golden_behavior"
        )
    )

    if not isinstance(
        golden_behavior,
        dict,
    ):
        raise GenerationError(
            "selected candidate "
            "has no expanded "
            "Golden behavior"
        )

    previous_property_path = (
        pilot_dir
        / "round0_property.sva"
    )

    if not previous_property_path.is_file():
        raise GenerationError(
            "Round-0 property "
            f"not found: "
            f"{previous_property_path}"
        )

    previous_property = (
        previous_property_path
        .read_text(
            encoding="utf-8"
        )
        .strip()
    )

    earliest = selected.get(
        "earliest_divergence"
    )

    if not isinstance(
        earliest,
        dict,
    ):
        raise GenerationError(
            "selected candidate "
            "has no earliest "
            "divergence snapshot"
        )

    round1_context = {
        "design":
            visible.get(
                "design"
            ),

        "workload":
            visible.get(
                "workload"
            ),

        "fault":
            visible.get(
                "fault"
            ),

        "signals":
            expanded_signals,

        "golden_behavior":
            golden_behavior,

        "training_observation":
            visible.get(
                "training_observation"
            ),

        "round0_feedback": {
            "previous_property":
                previous_property,

            "golden_safe":
                True,

            "target_detected":
                False,

            "verdict":
                "TARGET_NOT_DETECTED",
        },

        "privileged_downstream_feedback": {
            "selected_alias":
                "down_0_i",

            "downstream_depth":
                selected.get(
                    "depth"
                ),

            "selection_basis":
                (
                    "earliest bounded downstream "
                    "depth with time-aligned "
                    "divergence and "
                    "Golden-discriminative "
                    "fault behavior"
                ),

            "fault_only_observed_states":
                selected.get(
                    "candidate_added_fault_only_states",
                    [],
                ),

            "fault_only_site_downstream_transitions":
                selected.get(
                    "fault_only_site_candidate_transitions",
                    [],
                ),

            "first_divergence_sample": {
                "golden_values":
                    earliest.get(
                        "golden_values"
                    ),

                "fault_values":
                    earliest.get(
                        "fault_values"
                    ),
            },
        },
    }

    outputs = {
        "context":
            pilot_dir
            / "round1_context.json",

        "prompt":
            pilot_dir
            / "round1_prompt.txt",

        "request":
            pilot_dir
            / "round1_request.json",

        "response":
            pilot_dir
            / "round1_response.json",

        "response_text":
            pilot_dir
            / "round1_response.txt",

        "api_status":
            pilot_dir
            / "round1_api_status.json",

        "property":
            pilot_dir
            / "round1_property.sva",
    }

    existing = [
        path
        for path
        in outputs.values()
        if path.exists()
    ]

    if existing:
        raise GenerationError(
            "refusing to overwrite "
            "existing Round-1 "
            "artifacts:\n  "
            + "\n  ".join(
                str(path)
                for path
                in existing
            )
        )

    prompt = build_prompt(
        round1_context
    )

    write_json(
        outputs[
            "context"
        ],
        round1_context,
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
        raise GenerationError(
            "invalid model policy"
        )

    credentials = (
        parse_env_file(
            args.credential_file
            .expanduser()
            .resolve()
        )
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
        raise GenerationError(
            "OPENAI_API_KEY "
            "is missing or "
            "a placeholder"
        )

    try:
        from openai import OpenAI

    except ImportError as exc:
        raise GenerationError(
            "OpenAI Python SDK "
            "is not installed"
        ) from exc

    request_record = {
        "schema_version":
            "1.0",

        "stage":
            "stage_06_round1_downstream_generation",

        "fault_id":
            fault_id,

        "model":
            model,

        "reasoning_effort":
            effort,

        "max_output_tokens":
            max_tokens,

        "store":
            store,

        "prompt_sha256":
            sha256_file(
                outputs[
                    "prompt"
                ]
            ),

        "downstream_feedback_sha256":
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

            reasoning={
                "effort":
                    effort
            },

            input=prompt,

            max_output_tokens=
                max_tokens,

            store=store,
        )
    )

    response_record = (
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
        response_record,
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

    write_json(
        outputs[
            "api_status"
        ],
        {
            "schema_version":
                "1.0",

            "stage":
                "stage_06_round1_api_status",

            "fault_id":
                fault_id,

            "model_requested":
                model,

            "model_returned":
                response_record.get(
                    "model"
                ),

            "response_id":
                response_record.get(
                    "id"
                ),

            "response_status":
                response_record.get(
                    "status"
                ),

            "incomplete_details":
                response_record.get(
                    "incomplete_details"
                ),

            "usage":
                response_record.get(
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
        raise GenerationError(
            "OpenAI response "
            "contained no output_text"
        )

    property_body = (
        extract_property(
            response_text,
            previous_property,
        )
    )

    write_text(
        outputs[
            "property"
        ],
        property_body
        + "\n",
    )

    print()
    print("=" * 80)

    print(
        "Stage-6 Round-1 "
        "downstream generation: PASS"
    )

    print("=" * 80)

    print(
        f"Fault ID        : "
        f"{fault_id}"
    )

    print(
        "Downstream alias: "
        "down_0_i"
    )

    print(
        f"Depth           : "
        f"{selected.get('depth')}"
    )

    print(
        f"Expression      : "
        f"{selected.get('expression')}"
    )

    print("Property:")

    print(
        property_body
    )

    return 0


if __name__ == "__main__":

    try:
        raise SystemExit(
            main()
        )

    except GenerationError as exc:

        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )

        raise SystemExit(2)
