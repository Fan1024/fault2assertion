#!/usr/bin/env python3
"""Generate Stage-6 Train Round-1 using bounded downstream feedback.

Inputs:
- frozen Round-0 visible context
- Round-0 property and simulation verdict
- Train-only downstream feedback
- frozen OpenAI model policy

Outputs:
- round1_context.json
- round1_prompt.txt
- round1_request.json
- round1_response.json
- round1_response.txt
- round1_api_status.json
- round1_property.sva

No simulation is performed here.
"""

from __future__ import annotations

import argparse
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


class Round1Error(RuntimeError):
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
        payload = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

    except FileNotFoundError as exc:
        raise Round1Error(
            f"{label} not found: {path}"
        ) from exc

    except json.JSONDecodeError as exc:
        raise Round1Error(
            f"invalid {label} JSON "
            f"{path}: {exc}"
        ) from exc

    if not isinstance(
        payload,
        dict,
    ):
        raise Round1Error(
            f"{label} must contain "
            f"one JSON object: {path}"
        )

    return payload


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
        raise Round1Error(
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
            line = line[
                len("export "):
            ].strip()

        if "=" not in line:
            raise Round1Error(
                "invalid credential "
                f"line {line_number}: "
                f"{path}"
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
            and value[0] in {"'", '"'}
        ):
            value = value[
                1:-1
            ]

        result[key] = value

    return result


def extract_property(
    response_text: str,
    previous_property: str,
) -> str:

    if (
        response_text.count(
            BEGIN_MARKER
        ) != 1
        or response_text.count(
            END_MARKER
        ) != 1
    ):
        raise Round1Error(
            "response must contain exactly "
            "one BEGIN_SVA and one END_SVA"
        )

    marker_start = (
        response_text.index(
            BEGIN_MARKER
        )
    )

    body_start = (
        marker_start
        + len(BEGIN_MARKER)
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
        + len(END_MARKER):
    ].strip()

    if prefix or suffix:
        raise Round1Error(
            "response contains text "
            "outside BEGIN_SVA/END_SVA"
        )

    body = response_text[
        body_start:body_end
    ].strip()

    if not body:
        raise Round1Error(
            "generated property is empty"
        )

    lowered = body.lower()

    if "assert property" in lowered:
        raise Round1Error(
            "model emitted full "
            "assert property syntax"
        )

    if (
        "module " in lowered
        or "endmodule" in lowered
    ):
        raise Round1Error(
            "model emitted module syntax"
        )

    if body.endswith(";"):
        raise Round1Error(
            "property body must not "
            "end in semicolon"
        )

    if (
        body.strip()
        == previous_property.strip()
    ):
        raise Round1Error(
            "Round-1 property is "
            "identical to Round-0"
        )

    return body


def validate_expanded_behavior(
    behavior: Mapping[str, Any],
    expected_aliases: list[str],
) -> None:

    order = behavior.get(
        "signal_order"
    )

    if order != expected_aliases:
        raise Round1Error(
            "expanded Golden signal "
            "order mismatch\n"
            f"expected={expected_aliases}\n"
            f"actual={order}"
        )

    states = behavior.get(
        "observed_states"
    )

    if (
        not isinstance(states, list)
        or not states
    ):
        raise Round1Error(
            "expanded Golden behavior "
            "has no observed states"
        )

    width = len(
        expected_aliases
    )

    for value in states:

        if (
            not isinstance(value, str)
            or len(value) != width
            or any(
                bit not in {"0", "1"}
                for bit in value
            )
        ):
            raise Round1Error(
                "invalid expanded "
                "Golden state: "
                f"{value!r}"
            )


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

This is Train Round 1.

Round 0 successfully compiled and remained silent during the complete
fault-free Golden CRC32 execution, but it did not trigger during the target
faulty execution.

A bounded downstream fault-propagation analysis has now provided one additional
observation alias, `down_0_i`.

This downstream information is privileged Train-only feedback. Its purpose in
this round is to test whether a more diagnostically useful observation point can
repair the Golden-safe but target-insensitive Round-0 assertion.

Interpret the context carefully:

1. `golden_behavior.signal_order` defines the bit order of every string in
   `golden_behavior.observed_states`.

2. Every listed Golden state was observed during the complete fault-free
   workload execution. The generated property must remain consistent with all
   listed Golden behavior.

3. For `golden_behavior.one_cycle_transitions.<alias>`, each two-bit state is
   ordered as `[site_i, <alias>]`.

4. `"AB->CD"` means state `AB` at one sampled clock edge was followed by state
   `CD` at the immediately following sampled clock edge.

5. `privileged_downstream_feedback.fault_only_observed_states` contains expanded
   joint states that were observed during the target faulty execution but were
   not observed during the Golden execution for the exact expanded signal order.

6. `privileged_downstream_feedback.fault_only_site_downstream_transitions`
   contains `[site_i, down_0_i]` transitions observed in the target faulty
   execution but not in Golden.

7. `first_divergence_sample` is diagnostic evidence only. Do not use its
   absolute simulation cycle number.

Use only these aliases:
- `site_i`
- the supplied `recv_N_i` aliases
- `down_0_i`

Do not use raw hierarchy names or raw net names in the property.

The new property must:
- remain Golden-safe;
- differ from the Round-0 property;
- use the downstream feedback when it provides a meaningful discriminative
  condition;
- aim to trigger on the target faulty execution.

Useful SVA constructs:
- `|->` overlapped implication
- `|=>` next-cycle implication
- `##N` exact cycle delay
- `$past`
- `$stable`
- `$rose`
- `$fell`

Generate only one property-expression body.

Do not emit:
- clock/reset syntax
- `assert property`
- modules
- semicolons
- explanations
- Markdown
- raw hierarchy names
- absolute simulation cycle numbers

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

    if (
        FAULT_ID_RE.fullmatch(
            fault_id
        )
        is None
    ):
        raise Round1Error(
            f"invalid fault ID: "
            f"{fault_id!r}"
        )

    pilot_dir = (
        args.pilot_dir
        .expanduser()
        .resolve()
        if args.pilot_dir is not None
        else (
            root
            / "runs"
            / "stage6"
            / f"pilot_{fault_id}"
        ).resolve()
    )

    if not pilot_dir.is_dir():
        raise Round1Error(
            f"pilot directory "
            f"not found: {pilot_dir}"
        )

    round0_sim = load_json(
        pilot_dir
        / "round0_simulation.json",
        "Round-0 simulation",
    )

    if (
        round0_sim.get("verdict")
        != "TARGET_NOT_DETECTED"
    ):
        raise Round1Error(
            "Round-1 downstream feedback "
            "requires Round-0 "
            "TARGET_NOT_DETECTED; got "
            f"{round0_sim.get('verdict')!r}"
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
        "Round-1 downstream feedback",
    )

    if (
        feedback.get("status")
        != "DOWNSTREAM_CANDIDATE_FOUND"
    ):
        raise Round1Error(
            "downstream analysis did not "
            "find a candidate: "
            f"{feedback.get('status')!r}"
        )

    if (
        feedback.get(
            "privileged_train_only"
        )
        is not True
    ):
        raise Round1Error(
            "downstream feedback must be "
            "explicitly marked "
            "privileged_train_only"
        )

    selected = feedback.get(
        "selected"
    )

    if not isinstance(
        selected,
        dict,
    ):
        raise Round1Error(
            "feedback has no selected "
            "downstream candidate"
        )

    if (
        selected.get("alias")
        != "down_0_i"
    ):
        raise Round1Error(
            "selected downstream alias "
            "must be down_0_i"
        )

    expression = selected.get(
        "expression"
    )

    depth = selected.get(
        "depth"
    )

    if (
        not isinstance(expression, str)
        or not expression.strip()
        or not isinstance(depth, int)
    ):
        raise Round1Error(
            "selected downstream "
            "expression/depth invalid"
        )

    signals = baseline.get(
        "signals"
    )

    if (
        not isinstance(signals, dict)
        or "site_i" not in signals
    ):
        raise Round1Error(
            "baseline signals missing"
        )

    expanded_signals = dict(
        signals
    )

    if "down_0_i" in expanded_signals:
        raise Round1Error(
            "baseline unexpectedly "
            "already contains down_0_i"
        )

    expanded_signals[
        "down_0_i"
    ] = {
        "role":
            "train_feedback_downstream_observation",

        "netlist_expression":
            expression.strip(),

        "downstream_depth":
            depth,
    }

    expected_aliases = list(
        expanded_signals.keys()
    )

    expanded_behavior = selected.get(
        "expanded_golden_behavior"
    )

    if not isinstance(
        expanded_behavior,
        dict,
    ):
        raise Round1Error(
            "selected downstream candidate "
            "has no expanded Golden behavior"
        )

    validate_expanded_behavior(
        expanded_behavior,
        expected_aliases,
    )

    fault_only_states = selected.get(
        "candidate_added_fault_only_states",
        [],
    )

    fault_only_transitions = selected.get(
        "fault_only_site_candidate_transitions",
        [],
    )

    if not isinstance(
        fault_only_states,
        list,
    ):
        raise Round1Error(
            "fault-only states must "
            "be a list"
        )

    if not isinstance(
        fault_only_transitions,
        list,
    ):
        raise Round1Error(
            "fault-only transitions "
            "must be a list"
        )

    if (
        not fault_only_states
        and not fault_only_transitions
    ):
        raise Round1Error(
            "selected candidate has "
            "no discriminative "
            "fault-side feedback"
        )

    round0_property_path = (
        pilot_dir
        / "round0_property.sva"
    )

    if not round0_property_path.is_file():
        raise Round1Error(
            "Round-0 property missing"
        )

    round0_property = (
        round0_property_path
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
        earliest = None

    round1_context = {
        "design":
            baseline.get("design"),

        "workload":
            baseline.get("workload"),

        "fault":
            baseline.get("fault"),

        "signals":
            expanded_signals,

        "golden_behavior":
            expanded_behavior,

        "training_observation":
            baseline.get(
                "training_observation"
            ),

        "round0_feedback": {
            "previous_property":
                round0_property,

            "compile":
                "COMPILE_PASS",

            "golden_safe":
                True,

            "target_detected":
                False,

            "verdict":
                "TARGET_NOT_DETECTED",
        },

        "privileged_downstream_feedback": {
            "train_only":
                True,

            "selected_alias":
                "down_0_i",

            "downstream_depth":
                depth,

            "fault_only_observed_states":
                fault_only_states,

            "fault_only_site_downstream_transitions":
                fault_only_transitions,

            "first_divergence_sample":
                (
                    None
                    if earliest is None
                    else {
                        "golden_values":
                            earliest.get(
                                "golden_values"
                            ),

                        "fault_values":
                            earliest.get(
                                "fault_values"
                            ),
                    }
                ),
        },
    }

    prompt = build_prompt(
        round1_context
    )

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
        for path in outputs.values()
        if path.exists()
    ]

    if existing:
        raise Round1Error(
            "refusing to overwrite "
            "existing Round-1 artifacts:\n  "
            + "\n  ".join(
                str(path)
                for path in existing
            )
        )

    write_json(
        outputs["context"],
        round1_context,
    )

    write_text(
        outputs["prompt"],
        prompt,
    )

    model_policy_path = (
        args.model_policy
        .expanduser()
        .resolve()
    )

    policy = load_json(
        model_policy_path,
        "model policy",
    )

    if (
        policy.get("api")
        != "responses"
    ):
        raise Round1Error(
            "model policy must use "
            "api='responses'"
        )

    model = str(
        policy.get(
            "model",
            "",
        )
    ).strip()

    reasoning_effort = str(
        policy.get(
            "reasoning_effort",
            "medium",
        )
    ).strip()

    max_output_tokens = int(
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
        or max_output_tokens <= 0
    ):
        raise Round1Error(
            "invalid model policy"
        )

    credential_path = (
        args.credential_file
        .expanduser()
        .resolve()
    )

    credentials = parse_env_file(
        credential_path
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
        raise Round1Error(
            "OPENAI_API_KEY missing "
            "or placeholder"
        )

    try:
        from openai import OpenAI

    except ImportError as exc:
        raise Round1Error(
            "OpenAI Python SDK "
            "is not installed"
        ) from exc

    request_record = {
        "schema_version":
            "1.0",

        "stage":
            "stage_06_round1_generation",

        "fault_id":
            fault_id,

        "generated_at_utc":
            utc_now(),

        "feedback_type":
            "bounded_downstream_strong_teacher_v1",

        "privileged_train_only":
            True,

        "model":
            model,

        "reasoning_effort":
            reasoning_effort,

        "max_output_tokens":
            max_output_tokens,

        "store":
            store,

        "prompt_sha256":
            sha256_file(
                outputs["prompt"]
            ),

        "feedback_sha256":
            sha256_file(
                feedback_path
            ),

        "round0_property_sha256":
            sha256_file(
                round0_property_path
            ),
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
                    reasoning_effort
            },

            max_output_tokens=
                max_output_tokens,

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

    # Persist API facts before parsing the property.
    write_json(
        outputs["request"],
        request_record,
    )

    write_json(
        outputs["response"],
        response_payload,
    )

    write_text(
        outputs["response_text"],
        (
            response_text
            + "\n"
            if response_text
            else ""
        ),
    )

    write_json(
        outputs["api_status"],
        {
            "schema_version":
                "1.0",

            "stage":
                "stage_06_round1_api_status",

            "fault_id":
                fault_id,

            "response_id":
                response_payload.get("id"),

            "response_status":
                response_payload.get(
                    "status"
                ),

            "model_requested":
                model,

            "model_returned":
                response_payload.get(
                    "model"
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
        raise Round1Error(
            "OpenAI response "
            "contained no output_text"
        )

    property_body = extract_property(
        response_text,
        round0_property,
    )

    write_text(
        outputs["property"],
        property_body + "\n",
    )

    print()
    print("=" * 80)
    print(
        "Stage-6 Round-1 "
        "generation: PASS"
    )
    print("=" * 80)

    print(
        f"Fault ID          : "
        f"{fault_id}"
    )

    print(
        f"Model             : "
        f"{model}"
    )

    print(
        f"Reasoning         : "
        f"{reasoning_effort}"
    )

    print(
        f"Downstream alias  : "
        f"down_0_i"
    )

    print(
        f"Downstream signal : "
        f"{expression}"
    )

    print(
        f"Depth             : "
        f"{depth}"
    )

    print(
        f"Fault-only states : "
        f"{fault_only_states}"
    )

    print()
    print("Generated property:")
    print(property_body)

    print()
    print(
        f"Context           : "
        f"{outputs['context']}"
    )

    print(
        f"Prompt            : "
        f"{outputs['prompt']}"
    )

    print(
        f"Property          : "
        f"{outputs['property']}"
    )

    return 0


if __name__ == "__main__":

    try:
        raise SystemExit(
            main()
        )

    except Round1Error as exc:

        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )

        raise SystemExit(2)
