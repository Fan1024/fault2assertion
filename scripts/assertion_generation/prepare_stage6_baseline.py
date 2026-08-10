#!/usr/bin/env python3
"""Prepare the frozen Stage-6 Round-0 baseline context and prompt.

The full golden_behavior.json remains the durable scientific record.

Only a compact observational view enters visible_context.json:
- signal ordering;
- observed same-cycle joint states, without counts;
- observed per-receiver one-cycle transitions, without counts.

No OpenAI call and no simulation are performed here.
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


PASS_STATE = "ORACLE_VALIDATED_CLEANED"


class PrepareError(RuntimeError):
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
        raise PrepareError(
            f"{label} not found: {path}"
        ) from exc

    except json.JSONDecodeError as exc:
        raise PrepareError(
            f"invalid {label} JSON "
            f"{path}: {exc}"
        ) from exc

    if not isinstance(
        value,
        dict,
    ):
        raise PrepareError(
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


def canonical_digest(
    value: Any,
) -> str:

    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode(
        "utf-8"
    )

    return hashlib.sha256(
        encoded
    ).hexdigest()


def locate_fault(
    campaign_root: Path,
    fault_id: str,
) -> tuple[Path, Path]:

    matches = list(
        campaign_root.glob(
            f"sites/*/{fault_id}/fault.json"
        )
    )

    if len(matches) != 1:
        raise PrepareError(
            "expected exactly one Stage-5 "
            f"fault.json for {fault_id}; "
            f"found {len(matches)}"
        )

    fault_json = (
        matches[0]
        .resolve()
    )

    return (
        fault_json.parent,
        fault_json,
    )


def build_signal_map(
    fault_spec: Mapping[str, Any],
) -> dict[str, Any]:

    site = fault_spec.get(
        "site"
    )

    receivers = fault_spec.get(
        "receiver_signals"
    )

    if not isinstance(
        site,
        dict,
    ):
        raise PrepareError(
            "fault spec has no site object"
        )

    if (
        not isinstance(
            receivers,
            list,
        )
        or not receivers
    ):
        raise PrepareError(
            "fault spec has no "
            "receiver_signals"
        )

    source_net = site.get(
        "source_net"
    )

    if (
        not isinstance(
            source_net,
            str,
        )
        or not source_net
    ):
        raise PrepareError(
            "fault site.source_net "
            "is missing"
        )

    signals: dict[str, Any] = {
        "site_i": {
            "role":
                "fault_site",

            "netlist_expression":
                source_net,
        }
    }

    for index, raw in enumerate(
        receivers
    ):

        if not isinstance(
            raw,
            dict,
        ):
            raise PrepareError(
                "receiver_signals contains "
                "a non-object"
            )

        expression = raw.get(
            "expression"
        )

        if (
            not isinstance(
                expression,
                str,
            )
            or not expression
        ):
            raise PrepareError(
                "receiver signal has "
                "no expression"
            )

        signals[
            f"recv_{index}_i"
        ] = {
            "role":
                "direct_receiver",

            "netlist_expression":
                expression,
        }

    return signals


def require_binary_string(
    value: Any,
    width: int,
    label: str,
) -> str:

    if not isinstance(
        value,
        str,
    ):
        raise PrepareError(
            f"{label} must be a string"
        )

    normalized = (
        value.strip()
        .lower()
    )

    if (
        len(normalized)
        != width
        or any(
            char not in {"0", "1"}
            for char in normalized
        )
    ):
        raise PrepareError(
            f"invalid {label}: "
            f"{value!r}; expected "
            f"{width} binary bits"
        )

    return normalized


def compact_golden_behavior(
    profile: Mapping[str, Any],
    expected_aliases: list[str],
) -> dict[str, Any]:

    if (
        profile.get("stage")
        != "stage_06_golden_behavior_profile"
    ):
        raise PrepareError(
            "Golden behavior profile "
            "stage mismatch: "
            f"{profile.get('stage')!r}"
        )

    behavior = profile.get(
        "behavior"
    )

    if not isinstance(
        behavior,
        dict,
    ):
        raise PrepareError(
            "golden_behavior.json has "
            "no behavior object"
        )

    signal_order = behavior.get(
        "signal_order"
    )

    if signal_order != expected_aliases:
        raise PrepareError(
            "Golden behavior alias order "
            "does not match current "
            "fault context\n"
            f"  expected: {expected_aliases}\n"
            f"  actual:   {signal_order}"
        )

    sampling = behavior.get(
        "sampling"
    )

    if not isinstance(
        sampling,
        dict,
    ):
        raise PrepareError(
            "Golden behavior sampling "
            "metadata is missing"
        )

    binary_samples = sampling.get(
        "binary_samples"
    )

    if (
        not isinstance(
            binary_samples,
            int,
        )
        or binary_samples <= 0
    ):
        raise PrepareError(
            "Golden behavior contains "
            "no valid binary samples"
        )

    observed_raw = behavior.get(
        "observed_states"
    )

    if (
        not isinstance(
            observed_raw,
            list,
        )
        or not observed_raw
    ):
        raise PrepareError(
            "Golden behavior "
            "observed_states is "
            "missing or empty"
        )

    compact_states: list[str] = []
    seen_states: set[str] = set()

    state_width = len(
        expected_aliases
    )

    for index, raw in enumerate(
        observed_raw
    ):

        if not isinstance(
            raw,
            dict,
        ):
            raise PrepareError(
                "Golden observed state "
                f"{index} is not an object"
            )

        state = require_binary_string(
            raw.get(
                "values"
            ),
            state_width,
            (
                "Golden observed state "
                f"{index}"
            ),
        )

        count = raw.get(
            "count"
        )

        if (
            not isinstance(
                count,
                int,
            )
            or count <= 0
        ):
            raise PrepareError(
                "Golden observed state "
                f"{state!r} has invalid "
                f"count {count!r}"
            )

        if state in seen_states:
            raise PrepareError(
                "duplicate Golden "
                "observed state: "
                f"{state}"
            )

        seen_states.add(
            state
        )

        compact_states.append(
            state
        )

    one_cycle_raw = behavior.get(
        "one_cycle_behavior"
    )

    if not isinstance(
        one_cycle_raw,
        dict,
    ):
        raise PrepareError(
            "Golden behavior "
            "one_cycle_behavior "
            "is missing"
        )

    expected_receivers = (
        expected_aliases[1:]
    )

    if set(
        one_cycle_raw.keys()
    ) != set(
        expected_receivers
    ):
        raise PrepareError(
            "Golden one-cycle receiver "
            "set does not match visible "
            "receiver aliases\n"
            f"  expected: "
            f"{expected_receivers}\n"
            f"  actual:   "
            f"{sorted(one_cycle_raw.keys())}"
        )

    compact_transitions: dict[
        str,
        list[str],
    ] = {}

    for alias in expected_receivers:

        raw_record = one_cycle_raw.get(
            alias
        )

        if not isinstance(
            raw_record,
            dict,
        ):
            raise PrepareError(
                "Golden one-cycle record "
                f"for {alias} is invalid"
            )

        expected_pair_order = [
            "site_i",
            alias,
        ]

        if (
            raw_record.get(
                "signal_order"
            )
            != expected_pair_order
        ):
            raise PrepareError(
                "Golden one-cycle "
                "signal_order mismatch "
                f"for {alias}"
            )

        transitions = raw_record.get(
            "transitions"
        )

        if not isinstance(
            transitions,
            list,
        ):
            raise PrepareError(
                "Golden transitions "
                f"for {alias} are invalid"
            )

        compact_list: list[str] = []
        seen: set[str] = set()

        for index, raw in enumerate(
            transitions
        ):

            if not isinstance(
                raw,
                dict,
            ):
                raise PrepareError(
                    "Golden transition "
                    f"{alias}[{index}] "
                    "is not an object"
                )

            before = require_binary_string(
                raw.get(
                    "from"
                ),
                2,
                (
                    f"{alias} transition "
                    f"{index} from"
                ),
            )

            after = require_binary_string(
                raw.get(
                    "to"
                ),
                2,
                (
                    f"{alias} transition "
                    f"{index} to"
                ),
            )

            count = raw.get(
                "count"
            )

            if (
                not isinstance(
                    count,
                    int,
                )
                or count <= 0
            ):
                raise PrepareError(
                    f"{alias} transition "
                    f"{before}->{after} "
                    "has invalid count "
                    f"{count!r}"
                )

            compact = (
                f"{before}->{after}"
            )

            if compact in seen:
                raise PrepareError(
                    "duplicate Golden "
                    "transition for "
                    f"{alias}: {compact}"
                )

            seen.add(
                compact
            )

            compact_list.append(
                compact
            )

        compact_transitions[
            alias
        ] = compact_list

    return {
        "signal_order":
            list(
                expected_aliases
            ),

        "observed_states":
            compact_states,

        "one_cycle_transitions":
            compact_transitions,
    }


def training_observation(
    native_status: str,
) -> dict[str, Any]:

    table = {
        "OUTPUT_MISMATCH": {
            "workload_completed":
                True,

            "architectural_output_corrupted":
                True,
        },

        "OUTPUT_MATCH": {
            "workload_completed":
                True,

            "architectural_output_corrupted":
                False,
        },

        "TIMEOUT": {
            "workload_completed":
                False,

            "architectural_output_corrupted":
                None,
        },
    }

    if native_status not in table:
        raise PrepareError(
            "this first Stage-6 "
            "baseline currently supports "
            "only NATIVE_ONLY "
            "OUTPUT_MATCH/"
            "OUTPUT_MISMATCH/TIMEOUT; "
            f"got {native_status!r}"
        )

    return table[
        native_status
    ]


def render_prompt(
    template: str,
    knowledge: str,
    visible_context: Mapping[str, Any],
) -> str:

    markers = [
        "{{SVA_KNOWLEDGE}}",
        "{{VISIBLE_CONTEXT_JSON}}",
    ]

    for marker in markers:

        if template.count(
            marker
        ) != 1:
            raise PrepareError(
                "prompt template must "
                "contain exactly one "
                f"{marker}"
            )

    context_text = json.dumps(
        visible_context,
        indent=2,
        ensure_ascii=False,
    )

    prompt = template.replace(
        "{{SVA_KNOWLEDGE}}",
        knowledge.strip(),
    )

    prompt = prompt.replace(
        "{{VISIBLE_CONTEXT_JSON}}",
        context_text,
    )

    return (
        prompt.rstrip()
        + "\n"
    )


def parse_args() -> argparse.Namespace:

    root_default = (
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
            root_default
            / "runs"
            / "stage5_campaign_v3"
            / "cv32e40p"
            / "crc32"
            / "sites_all"
        ),
    )

    parser.add_argument(
        "--pilot-dir",
        type=Path,
        default=None,
    )

    parser.add_argument(
        "--prompt-template",
        type=Path,
        default=(
            root_default
            / "assertion_generation"
            / "prompt_v002.txt"
        ),
    )

    parser.add_argument(
        "--sva-knowledge",
        type=Path,
        default=(
            root_default
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
        args.fault_id.strip()
    )

    if (
        re.fullmatch(
            r"TF\d{6}_SA[01]",
            fault_id,
        )
        is None
    ):
        raise PrepareError(
            f"invalid fault ID: "
            f"{fault_id!r}"
        )

    campaign_root = (
        args.campaign_root
        .expanduser()
        .resolve()
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
        raise PrepareError(
            "pilot directory not found: "
            f"{pilot_dir}; run Golden "
            "behavior profiling first"
        )

    golden_behavior_path = (
        pilot_dir
        / "golden_behavior.json"
    )

    manifest_path = (
        pilot_dir
        / "manifest.json"
    )

    visible_context_path = (
        pilot_dir
        / "visible_context.json"
    )

    prompt_path = (
        pilot_dir
        / "prompt.txt"
    )

    for path in (
        manifest_path,
        visible_context_path,
        prompt_path,
    ):

        if path.exists():
            raise PrepareError(
                "refusing to overwrite "
                "existing baseline "
                f"artifact: {path}"
            )

    (
        fault_dir,
        fault_json,
    ) = locate_fault(
        campaign_root,
        fault_id,
    )

    fault_spec = load_json(
        fault_json,
        "Stage-5 fault spec",
    )

    status = load_json(
        fault_dir
        / "status.json",
        "Stage-5 fault status",
    )

    routing = load_json(
        fault_dir
        / "routing.json",
        "Stage-5 routing",
    )

    golden_profile = load_json(
        golden_behavior_path,
        "Stage-6 Golden behavior",
    )

    if (
        status.get(
            "state"
        )
        != PASS_STATE
    ):
        raise PrepareError(
            "Stage-5 fault is "
            "not complete: "
            f"state="
            f"{status.get('state')!r}"
        )

    if (
        routing.get(
            "route"
        )
        != "NATIVE_ONLY"
    ):
        raise PrepareError(
            "this first Stage-6 "
            "baseline is intentionally "
            "NATIVE_ONLY; Stage-5 "
            "route is "
            f"{routing.get('route')!r}"
        )

    if (
        golden_profile.get(
            "fault_id"
        )
        != fault_id
    ):
        raise PrepareError(
            "golden_behavior.json "
            "fault_id mismatch"
        )

    signals = build_signal_map(
        fault_spec
    )

    expected_aliases = list(
        signals.keys()
    )

    compact_behavior = (
        compact_golden_behavior(
            golden_profile,
            expected_aliases,
        )
    )

    site = fault_spec.get(
        "site"
    )

    if not isinstance(
        site,
        dict,
    ):
        raise PrepareError(
            "fault spec site object "
            "is missing"
        )

    native_status = str(
        status.get(
            "native_status",
            "",
        )
    )

    visible_context = {
        "design":
            str(
                fault_spec.get(
                    "design",
                    "cv32e40p",
                )
            ),

        "workload":
            str(
                fault_spec.get(
                    "workload",
                    "crc32",
                )
            ),

        "fault": {
            "fault_class":
                fault_spec.get(
                    "fault_class"
                ),

            "polarity":
                fault_spec.get(
                    "polarity"
                ),

            "stuck_at":
                fault_spec.get(
                    "stuck_at"
                ),

            "module":
                site.get(
                    "module"
                ),

            "source_kind":
                site.get(
                    "source_kind"
                ),
        },

        "signals":
            signals,

        "golden_behavior":
            compact_behavior,

        "training_observation":
            training_observation(
                native_status
            ),
    }

    template_path = (
        args.prompt_template
        .expanduser()
        .resolve()
    )

    knowledge_path = (
        args.sva_knowledge
        .expanduser()
        .resolve()
    )

    if not template_path.is_file():
        raise PrepareError(
            "prompt template "
            f"not found: {template_path}"
        )

    if not knowledge_path.is_file():
        raise PrepareError(
            "SVA knowledge file "
            f"not found: {knowledge_path}"
        )

    template = (
        template_path
        .read_text(
            encoding="utf-8"
        )
    )

    knowledge = (
        knowledge_path
        .read_text(
            encoding="utf-8"
        )
    )

    prompt = render_prompt(
        template,
        knowledge,
        visible_context,
    )

    write_json(
        visible_context_path,
        visible_context,
    )

    write_text(
        prompt_path,
        prompt,
    )

    manifest = {
        "schema_version":
            "1.0",

        "stage":
            "stage_06_baseline_ready",

        "fault_id":
            fault_id,

        "prepared_at_utc":
            utc_now(),

        "baseline_definition":
            "LOCAL_STATIC_PLUS_GOLDEN_OBSERVED_BEHAVIOR_V1",

        "stage5": {
            "fault_json":
                str(
                    fault_json
                ),

            "fault_json_sha256":
                sha256_file(
                    fault_json
                ),

            "route":
                routing.get(
                    "route"
                ),

            "native_status":
                native_status,
        },

        "golden_behavior_full": {
            "path":
                str(
                    golden_behavior_path
                ),

            "sha256":
                sha256_file(
                    golden_behavior_path
                ),

            "model_visible_compaction": {
                "counts_removed":
                    True,

                "sampling_metadata_removed":
                    True,

                "scope_removed":
                    True,

                "provenance_removed":
                    True,

                "state_representation":
                    "binary_string",

                "transition_representation":
                    "FROM->TO",
            },
        },

        "prompt_inputs": {
            "template":
                str(
                    template_path
                ),

            "template_sha256":
                sha256_file(
                    template_path
                ),

            "sva_knowledge":
                str(
                    knowledge_path
                ),

            "sva_knowledge_sha256":
                sha256_file(
                    knowledge_path
                ),
        },

        "visible_context": {
            "path":
                str(
                    visible_context_path
                ),

            "digest_sha256":
                canonical_digest(
                    visible_context
                ),
        },

        "prompt": {
            "path":
                str(
                    prompt_path
                ),

            "sha256":
                sha256_file(
                    prompt_path
                ),
        },

        "baseline_frozen":
            True,
    }

    write_json(
        manifest_path,
        manifest,
    )

    print()
    print("=" * 80)
    print(
        "Stage-6 baseline "
        "preparation: PASS"
    )
    print("=" * 80)

    print(
        f"Fault ID           : "
        f"{fault_id}"
    )

    print(
        f"Stage-5 baseline   : "
        f"{native_status}"
    )

    print(
        "Visible aliases    : "
        + ", ".join(
            expected_aliases
        )
    )

    print(
        "Observed states    : "
        f"{len(compact_behavior['observed_states'])}"
    )

    for alias, transitions in (
        compact_behavior[
            "one_cycle_transitions"
        ].items()
    ):

        print(
            f"{alias:18s}: "
            f"{len(transitions)} "
            "observed transitions"
        )

    print(
        f"Full Golden record : "
        f"{golden_behavior_path}"
    )

    print(
        f"Visible context    : "
        f"{visible_context_path}"
    )

    print(
        f"Prompt             : "
        f"{prompt_path}"
    )

    print(
        f"Manifest           : "
        f"{manifest_path}"
    )

    return 0


if __name__ == "__main__":

    try:
        raise SystemExit(
            main()
        )

    except PrepareError as exc:

        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )

        raise SystemExit(2)
