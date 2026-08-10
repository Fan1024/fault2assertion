#!/usr/bin/env python3
"""Diagnose Stage-6 observation sufficiency after the first Golden-safe target miss.

Methodology:
  Generate first -> verify -> only after TARGET_NOT_DETECTED diagnose scope.

The current observation scope is tested first. It is sufficient when the
supported evidence family contains either:
  1. a fault-only same-cycle joint state over the current aliases, or
  2. a fault-only one-cycle (site_i, alias) transition.

Only when neither exists does this tool invoke the already validated bounded
receiver-output downstream analyzer. The shallowest sufficient scope is then
frozen for later rounds.

The output is:
  round<consumer>_scope_feedback.json

No API call is made here. Exact fault evidence is retained internally for a
possible later counterexample round, but coarse localization rounds expose only
its evidence type(s).
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


PASS_STATE = "ORACLE_VALIDATED_CLEANED"

REQUIRED_VERDICT = (
    "TARGET_NOT_DETECTED"
)

VALID_GOLDEN_STATUSES = {
    "PASS",
    "OUTPUT_MATCH",
}

SUPPORTED_BASELINES = {
    "OUTPUT_MATCH",
    "OUTPUT_MISMATCH",
    "TIMEOUT",
}


class ScopeError(RuntimeError):
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

        raise ScopeError(
            f"{label} not found: "
            f"{path}"
        ) from exc

    except json.JSONDecodeError as exc:

        raise ScopeError(
            f"invalid {label} JSON "
            f"{path}: {exc}"
        ) from exc

    if not isinstance(
        value,
        dict,
    ):

        raise ScopeError(
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


def import_module(
    path: Path,
    module_name: str,
) -> Any:

    if not path.is_file():

        raise ScopeError(
            "Python module not found: "
            f"{path}"
        )

    spec = (
        importlib.util
        .spec_from_file_location(
            module_name,
            path,
        )
    )

    if (
        spec is None
        or spec.loader is None
    ):

        raise ScopeError(
            "cannot import Python module: "
            f"{path}"
        )

    module = (
        importlib.util
        .module_from_spec(
            spec
        )
    )

    sys.modules[
        module_name
    ] = module

    spec.loader.exec_module(
        module
    )

    return module


def clean_env() -> dict[str, str]:

    env = dict(
        os.environ
    )

    for key in list(env):

        if (
            key == "OPENAI_API_KEY"
            or key.startswith(
                "OPENAI_"
            )
            or key
            == "F2A_OPENAI_ENV"
        ):

            env.pop(
                key,
                None,
            )

    return env


def current_context(
    pilot_dir: Path,
    source_round: int,
) -> tuple[
    Path,
    dict[str, Any],
]:

    if source_round == 0:

        path = (
            pilot_dir
            / "visible_context.json"
        )

    else:

        path = (
            pilot_dir
            / (
                f"round{source_round}"
                "_context.json"
            )
        )

    return (
        path,
        load_json(
            path,
            (
                f"Round-{source_round} "
                "internal context"
            ),
        ),
    )


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

        raise ScopeError(
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


def signal_records(
    context: Mapping[str, Any],
) -> list[dict[str, str]]:

    signals = context.get(
        "signals"
    )

    if (
        not isinstance(
            signals,
            dict,
        )
        or not signals
    ):

        raise ScopeError(
            "context has no signals object"
        )

    records: list[
        dict[str, str]
    ] = []

    for alias, raw in (
        signals.items()
    ):

        if (
            not isinstance(
                alias,
                str,
            )
            or not isinstance(
                raw,
                dict,
            )
        ):

            raise ScopeError(
                "invalid signal record: "
                f"{alias!r}"
            )

        expression = raw.get(
            "netlist_expression"
        )

        if (
            not isinstance(
                expression,
                str,
            )
            or not expression.strip()
        ):

            raise ScopeError(
                f"signal {alias!r} "
                "has no "
                "netlist_expression"
            )

        records.append(
            {
                "alias":
                    alias,

                "expression":
                    expression.strip(),
            }
        )

    if (
        records[0][
            "alias"
        ]
        != "site_i"
    ):

        raise ScopeError(
            "site_i must be the "
            "first observation alias"
        )

    if any(
        item["alias"]
        == "down_0_i"
        for item
        in records
    ):

        raise ScopeError(
            "scope diagnosis must run "
            "before downstream expansion; "
            "current context already "
            "contains down_0_i"
        )

    return records


def runner_status(
    path: Path,
) -> str | None:

    result = (
        path
        / "result.json"
    )

    if not result.is_file():
        return None

    value = load_json(
        result,
        "runner result",
    ).get(
        "status"
    )

    return (
        str(value)
        if value is not None
        else None
    )


def run_command(
    command: list[str],
    env: Mapping[str, str],
) -> int:

    print(
        "+ "
        + " ".join(
            command
        ),
        flush=True,
    )

    completed = subprocess.run(
        command,
        env=dict(env),
        check=False,
    )

    return int(
        completed.returncode
    )


def flatten_transition_novelty(
    analyzer: Any,
    golden_rows:
        list[
            tuple[int, int, str]
        ],
    fault_rows:
        list[
            tuple[int, int, str]
        ],
    aliases: list[str],
) -> dict[
    str,
    list[str],
]:

    result: dict[
        str,
        list[str],
    ] = {}

    # Keep the evidence family
    # consistent with the existing
    # Golden profiler:
    #
    # (site_i, alias) one-cycle
    # transitions.
    for index in range(
        1,
        len(
            aliases
        ),
    ):

        golden = (
            analyzer
            .pair_transition_set(
                golden_rows,
                0,
                index,
            )
        )

        faulty = (
            analyzer
            .pair_transition_set(
                fault_rows,
                0,
                index,
            )
        )

        novelty = sorted(
            faulty
            - golden
        )

        if novelty:

            result[
                aliases[
                    index
                ]
            ] = novelty

    return result


def compact_divergence(
    analyzer: Any,
    aliases: list[str],
    first_any:
        dict[str, Any]
        | None,
) -> dict[str, Any] | None:

    if not isinstance(
        first_any,
        dict,
    ):
        return None

    golden_bits = (
        first_any.get(
            "golden_bits"
        )
    )

    fault_bits = (
        first_any.get(
            "fault_bits"
        )
    )

    if (
        not isinstance(
            golden_bits,
            str,
        )
        or not isinstance(
            fault_bits,
            str,
        )
    ):
        return None

    return {
        "golden_values":
            analyzer.values_by_alias(
                aliases,
                golden_bits,
            ),

        "fault_values":
            analyzer.values_by_alias(
                aliases,
                fault_bits,
            ),
    }


def profile_base_scope(
    *,
    root: Path,
    campaign_root: Path,
    pilot_dir: Path,
    fault_id: str,
    source_round: int,
    maxcycles: int,
) -> dict[str, Any]:

    analyzer = import_module(
        root
        / "scripts"
        / "assertion_generation"
        / "analyze_stage6_downstream.py",
        "f2a_stage6_scope_analyzer",
    )

    (
        context_path,
        context,
    ) = current_context(
        pilot_dir,
        source_round,
    )

    aliases_records = (
        signal_records(
            context
        )
    )

    aliases = [
        item[
            "alias"
        ]
        for item
        in aliases_records
    ]

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
        "Stage-5 status",
    )

    routing = load_json(
        fault_dir
        / "routing.json",
        "Stage-5 routing",
    )

    if (
        status.get(
            "state"
        )
        != PASS_STATE
    ):

        raise ScopeError(
            "Stage-5 fault is not "
            "ORACLE_VALIDATED_CLEANED"
        )

    if (
        routing.get(
            "route"
        )
        != "NATIVE_ONLY"
    ):

        raise ScopeError(
            "scope diagnosis currently "
            "supports NATIVE_ONLY faults"
        )

    expected_fault_status = str(
        status.get(
            "native_status",
            "",
        )
    )

    if (
        expected_fault_status
        not in
        SUPPORTED_BASELINES
    ):

        raise ScopeError(
            "unsupported Stage-5 "
            "native status: "
            f"{expected_fault_status!r}"
        )

    site = fault_spec.get(
        "site"
    )

    mapped = fault_spec.get(
        "mapped_netlist"
    )

    if (
        not isinstance(
            site,
            dict,
        )
        or not isinstance(
            mapped,
            dict,
        )
    ):

        raise ScopeError(
            "fault spec is missing "
            "site/mapped_netlist metadata"
        )

    module_name = str(
        site.get(
            "module",
            "",
        )
    )

    golden_netlist = Path(
        str(
            mapped.get(
                "path",
                "",
            )
        )
    ).expanduser().resolve()

    if (
        not module_name
        or not golden_netlist.is_file()
    ):

        raise ScopeError(
            "fault module or mapped "
            "Golden netlist is invalid"
        )

    helpers = import_module(
        root
        / "scripts"
        / "fault_characterization"
        / "stage5_faults_v107_impl.py",
        "f2a_stage6_scope_stage5_helpers",
    )

    scratch_parent = (
        root
        / "runs"
        / "stage6"
    )

    scratch_parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    scratch = Path(
        tempfile.mkdtemp(
            prefix=(
                ".scratch_scope_base_"
                f"{fault_id}_"
                f"r{source_round}_"
            ),
            dir=scratch_parent,
        )
    ).resolve()

    try:

        golden_trace = (
            scratch
            / "golden.trace.tsv"
        )

        fault_trace = (
            scratch
            / "fault.trace.tsv"
        )

        golden_monitor = (
            scratch
            / "golden_monitor.sv"
        )

        fault_monitor = (
            scratch
            / "fault_monitor.sv"
        )

        golden_run = (
            scratch
            / "golden_run"
        )

        fault_run = (
            scratch
            / "fault_run"
        )

        golden_monitor.write_text(
            analyzer.build_monitor(
                helpers=
                    helpers,

                module_name=
                    module_name,

                alias_records=
                    aliases_records,

                trace_path=
                    golden_trace,
            ),
            encoding="utf-8",
        )

        fault_monitor.write_text(
            analyzer.build_monitor(
                helpers=
                    helpers,

                module_name=
                    module_name,

                alias_records=
                    aliases_records,

                trace_path=
                    fault_trace,
            ),
            encoding="utf-8",
        )

        base_env = clean_env()

        golden_env = dict(
            base_env
        )

        golden_env.update(
            {
                "STAGE5_PHASE":
                    "run",

                "STAGE5_RUN_PURPOSE":
                    "NATIVE_CHARACTERIZATION",

                "STAGE5_TRACE_OUTPUT":
                    str(
                        golden_trace
                    ),

                "GOLDEN_NETLIST":
                    str(
                        golden_netlist
                    ),

                "MAXCYCLES":
                    str(
                        maxcycles
                    ),

                "VCD":
                    "0",

                "KEEP_WORK":
                    "0",
            }
        )

        golden_rc = run_command(
            [
                "bash",

                str(
                    root
                    / "scripts"
                    / "run_xrun_stage5_golden.sh"
                ),

                str(
                    golden_monitor
                ),

                str(
                    golden_run
                ),
            ],
            golden_env,
        )

        golden_status = (
            runner_status(
                golden_run
            )
        )

        if (
            golden_status
            not in
            VALID_GOLDEN_STATUSES
        ):

            raise ScopeError(
                "Golden base profiling "
                "failed: "
                f"rc={golden_rc}, "
                f"status={golden_status!r}"
            )

        fault_env = dict(
            base_env
        )

        fault_env.update(
            {
                "STAGE5_PHASE":
                    "run",

                "STAGE5_RUN_PURPOSE":
                    "NATIVE_CHARACTERIZATION",

                "STAGE5_TRACE_OUTPUT":
                    str(
                        fault_trace
                    ),

                "MAXCYCLES":
                    str(
                        maxcycles
                    ),

                "VCD":
                    "0",

                "KEEP_WORK":
                    "0",
            }
        )

        fault_rc = run_command(
            [
                "bash",

                str(
                    root
                    / "scripts"
                    / "run_xrun_stage5_fault.sh"
                ),

                str(
                    fault_json
                ),

                str(
                    fault_monitor
                ),

                str(
                    fault_run
                ),
            ],
            fault_env,
        )

        fault_status = (
            runner_status(
                fault_run
            )
        )

        if (
            fault_status
            != expected_fault_status
        ):

            raise ScopeError(
                "fault base profiling did "
                "not replay Stage-5 outcome: "
                "expected="
                f"{expected_fault_status}, "
                "actual="
                f"{fault_status}, "
                f"rc={fault_rc}"
            )

        golden_series = (
            analyzer.parse_trace(
                golden_trace,
                aliases,
            )
        )

        fault_series = (
            analyzer.parse_trace(
                fault_trace,
                aliases,
            )
        )

        common_scopes = sorted(
            set(
                golden_series
            )
            & set(
                fault_series
            )
        )

        if (
            len(
                common_scopes
            )
            != 1
        ):

            raise ScopeError(
                "expected exactly one "
                "common bound scope; "
                f"found {common_scopes}"
            )

        scope = (
            common_scopes[
                0
            ]
        )

        golden_rows = (
            golden_series[
                scope
            ]
        )

        fault_rows = (
            fault_series[
                scope
            ]
        )

        indices = list(
            range(
                len(
                    aliases
                )
            )
        )

        golden_states = (
            analyzer.state_set(
                golden_rows,
                indices,
            )
        )

        fault_states = (
            analyzer.state_set(
                fault_rows,
                indices,
            )
        )

        fault_only_states = sorted(
            fault_states
            - golden_states
        )

        fault_only_transitions = (
            flatten_transition_novelty(
                analyzer,
                golden_rows,
                fault_rows,
                aliases,
            )
        )

        (
            _,
            first_any,
        ) = (
            analyzer
            .aligned_first_divergences(
                golden_rows,
                fault_rows,
                len(
                    aliases
                ),
            )
        )

        evidence_types: list[
            str
        ] = []

        if fault_only_states:

            evidence_types.append(
                "SAME_CYCLE_JOINT_STATE_NOVELTY"
            )

        if fault_only_transitions:

            evidence_types.append(
                "ONE_CYCLE_TRANSITION_NOVELTY"
            )

        return {
            "context_path":
                str(
                    context_path
                ),

            "aliases":
                aliases,

            "evidence_types":
                evidence_types,

            "exact_evidence": {
                "fault_only_observed_states":
                    fault_only_states,

                "fault_only_site_alias_transitions":
                    fault_only_transitions,

                "divergence_values":
                    compact_divergence(
                        analyzer,
                        aliases,
                        first_any,
                    ),
            },

            "simulation_replay": {
                "golden_status":
                    golden_status,

                "fault_status":
                    fault_status,

                "expected_fault_status":
                    expected_fault_status,
            },
        }

    finally:

        shutil.rmtree(
            scratch,
            ignore_errors=True,
        )


def run_downstream_search(
    *,
    root: Path,
    campaign_root: Path,
    pilot_dir: Path,
    fault_id: str,
    source_round: int,
    max_depth: int,
    max_candidates: int,
    maxcycles: int,
    stage1_catalog: Path,
) -> dict[str, Any]:

    analyzer_path = (
        root
        / "scripts"
        / "assertion_generation"
        / "analyze_stage6_downstream.py"
    )

    (
        context_path,
        _,
    ) = current_context(
        pilot_dir,
        source_round,
    )

    source_sim = (
        pilot_dir
        / (
            f"round{source_round}"
            "_simulation.json"
        )
    )

    scratch_parent = (
        root
        / "runs"
        / "stage6"
    )

    scratch_parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    scratch = Path(
        tempfile.mkdtemp(
            prefix=(
                ".scratch_scope_downstream_"
                f"{fault_id}_"
                f"r{source_round}_"
            ),
            dir=scratch_parent,
        )
    ).resolve()

    try:

        shutil.copy2(
            source_sim,
            scratch
            / "round0_simulation.json",
        )

        shutil.copy2(
            context_path,
            scratch
            / "visible_context.json",
        )

        command = [
            sys.executable,

            str(
                analyzer_path
            ),

            "--fault-id",
            fault_id,

            "--campaign-root",
            str(
                campaign_root
            ),

            "--pilot-dir",
            str(
                scratch
            ),

            "--stage1-catalog",
            str(
                stage1_catalog
            ),

            "--max-depth",
            str(
                max_depth
            ),

            "--max-candidates",
            str(
                max_candidates
            ),

            "--maxcycles",
            str(
                maxcycles
            ),
        ]

        rc = run_command(
            command,
            clean_env(),
        )

        output = (
            scratch
            / "round1_downstream_feedback.json"
        )

        if (
            rc != 0
            or not output.is_file()
        ):

            error = (
                scratch
                / "round1_downstream_error.log"
            )

            if error.is_file():

                retained = (
                    pilot_dir
                    / "scope_diagnosis_error.log"
                )

                shutil.copy2(
                    error,
                    retained,
                )

            raise ScopeError(
                "bounded downstream "
                "analyzer failed with "
                f"rc={rc}"
            )

        return copy.deepcopy(
            load_json(
                output,
                "downstream analyzer output",
            )
        )

    finally:

        shutil.rmtree(
            scratch,
            ignore_errors=True,
        )


def compact_downstream(
    raw: Mapping[str, Any],
) -> dict[str, Any]:

    status = raw.get(
        "status"
    )

    if (
        status
        != "DOWNSTREAM_CANDIDATE_FOUND"
    ):

        return {
            "status":
                "NO_DISCRIMINATIVE_EVIDENCE",

            "scope_decision":
                "NONE",

            "scope_depth":
                None,

            "evidence_types":
                [],

            "selected":
                None,
        }

    selected = raw.get(
        "selected"
    )

    if not isinstance(
        selected,
        dict,
    ):

        raise ScopeError(
            "downstream analyzer "
            "reported success without "
            "selected candidate"
        )

    states = selected.get(
        "candidate_added_fault_only_states",
        [],
    )

    transitions = selected.get(
        "fault_only_site_candidate_transitions",
        [],
    )

    evidence_types: list[
        str
    ] = []

    if (
        isinstance(
            states,
            list,
        )
        and states
    ):

        evidence_types.append(
            "SAME_CYCLE_JOINT_STATE_NOVELTY"
        )

    if (
        isinstance(
            transitions,
            list,
        )
        and transitions
    ):

        evidence_types.append(
            "ONE_CYCLE_TRANSITION_NOVELTY"
        )

    if not evidence_types:

        raise ScopeError(
            "selected downstream "
            "candidate has no "
            "supported evidence"
        )

    divergence = selected.get(
        "earliest_divergence"
    )

    divergence_values = None

    if isinstance(
        divergence,
        dict,
    ):

        gv = divergence.get(
            "golden_values"
        )

        fv = divergence.get(
            "fault_values"
        )

        if (
            isinstance(
                gv,
                dict,
            )
            and isinstance(
                fv,
                dict,
            )
        ):

            divergence_values = {
                "golden_values":
                    copy.deepcopy(
                        gv
                    ),

                "fault_values":
                    copy.deepcopy(
                        fv
                    ),
            }

    return {
        "status":
            "DOWNSTREAM_EVIDENCE_FOUND",

        "scope_decision":
            "DOWNSTREAM",

        "scope_depth":
            selected.get(
                "depth"
            ),

        "evidence_types":
            evidence_types,

        "selected": {
            "alias":
                "down_0_i",

            "internal_signal":
                selected.get(
                    "expression"
                ),

            "expanded_golden_behavior":
                copy.deepcopy(
                    selected.get(
                        "expanded_golden_behavior"
                    )
                ),
        },

        "exact_evidence": {
            "fault_only_observed_states":
                copy.deepcopy(
                    states
                ),

            "fault_only_site_downstream_transitions":
                copy.deepcopy(
                    transitions
                ),

            "divergence_values":
                divergence_values,
        },
    }


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
        "--source-round",
        type=int,
        required=True,
        choices=(
            0,
            1,
        ),
    )

    parser.add_argument(
        "--consumer-round",
        type=int,
        required=True,
        choices=(
            1,
            2,
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
        "--pilot-dir",
        type=Path,
    )

    parser.add_argument(
        "--stage1-catalog",
        type=Path,
        default=(
            root
            / "faults"
            / "cv32e40p"
            / "site_catalog"
            / "stage_01_raw_sites.json"
        ),
    )

    parser.add_argument(
        "--max-depth",
        type=int,
        default=3,
        choices=(
            2,
            3,
        ),
    )

    parser.add_argument(
        "--max-candidates",
        type=int,
        default=128,
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

    fault_id = (
        args.fault_id
        .strip()
    )

    if (
        args.consumer_round
        != args.source_round + 1
    ):

        raise ScopeError(
            "consumer round must equal "
            "source round + 1"
        )

    if (
        args.max_candidates <= 0
        or args.maxcycles <= 0
    ):

        raise ScopeError(
            "--max-candidates and "
            "--maxcycles must be positive"
        )

    campaign_root = (
        args.campaign_root
        .expanduser()
        .resolve()
    )

    stage1_catalog = (
        args.stage1_catalog
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

        raise ScopeError(
            "pilot directory not found: "
            f"{pilot_dir}"
        )

    source_sim = (
        pilot_dir
        / (
            f"round{args.source_round}"
            "_simulation.json"
        )
    )

    simulation = load_json(
        source_sim,
        (
            f"Round-{args.source_round} "
            "simulation"
        ),
    )

    if (
        simulation.get(
            "verdict"
        )
        != REQUIRED_VERDICT
    ):

        raise ScopeError(
            "scope diagnosis requires "
            f"{REQUIRED_VERDICT}; got "
            f"{simulation.get('verdict')!r}"
        )

    output = (
        pilot_dir
        / (
            f"round{args.consumer_round}"
            "_scope_feedback.json"
        )
    )

    if output.exists():

        raise ScopeError(
            "refusing to overwrite "
            "existing scope feedback: "
            f"{output}"
        )

    base = profile_base_scope(
        root=root,
        campaign_root=campaign_root,
        pilot_dir=pilot_dir,
        fault_id=fault_id,
        source_round=args.source_round,
        maxcycles=args.maxcycles,
    )

    if base[
        "evidence_types"
    ]:

        payload: dict[
            str,
            Any,
        ] = {
            "schema_version":
                "2.0",

            "stage":
                "stage_06_scope_diagnosis",

            "fault_id":
                fault_id,

            "source_round":
                args.source_round,

            "consumer_round":
                args.consumer_round,

            "source_verdict":
                REQUIRED_VERDICT,

            "status":
                "BASE_EVIDENCE_FOUND",

            "scope_decision":
                "BASE",

            "scope_depth":
                0,

            "evidence_types":
                copy.deepcopy(
                    base[
                        "evidence_types"
                    ]
                ),

            "base_analysis": {
                "aliases":
                    copy.deepcopy(
                        base[
                            "aliases"
                        ]
                    ),

                "exact_evidence":
                    copy.deepcopy(
                        base[
                            "exact_evidence"
                        ]
                    ),

                "simulation_replay":
                    copy.deepcopy(
                        base[
                            "simulation_replay"
                        ]
                    ),
            },

            "selected":
                None,

            "analysis_policy": {
                "principle":
                    "shallowest_sufficient_observation_scope",

                "base_evidence_family": [
                    "same_cycle_joint_state_novelty",
                    "one_cycle_site_alias_transition_novelty"
                ],

                "downstream_search_performed":
                    False,

                "max_downstream_depth":
                    args.max_depth,
            },

            "generated_at_utc":
                utc_now(),
        }

    else:

        downstream_raw = (
            run_downstream_search(
                root=root,
                campaign_root=
                    campaign_root,
                pilot_dir=
                    pilot_dir,
                fault_id=
                    fault_id,
                source_round=
                    args.source_round,
                max_depth=
                    args.max_depth,
                max_candidates=
                    args.max_candidates,
                maxcycles=
                    args.maxcycles,
                stage1_catalog=
                    stage1_catalog,
            )
        )

        downstream = (
            compact_downstream(
                downstream_raw
            )
        )

        payload = {
            "schema_version":
                "2.0",

            "stage":
                "stage_06_scope_diagnosis",

            "fault_id":
                fault_id,

            "source_round":
                args.source_round,

            "consumer_round":
                args.consumer_round,

            "source_verdict":
                REQUIRED_VERDICT,

            **downstream,

            "base_analysis": {
                "aliases":
                    copy.deepcopy(
                        base[
                            "aliases"
                        ]
                    ),

                "exact_evidence":
                    copy.deepcopy(
                        base[
                            "exact_evidence"
                        ]
                    ),

                "simulation_replay":
                    copy.deepcopy(
                        base[
                            "simulation_replay"
                        ]
                    ),
            },

            "analysis_policy": {
                "principle":
                    "shallowest_sufficient_observation_scope",

                "base_evidence_family": [
                    "same_cycle_joint_state_novelty",
                    "one_cycle_site_alias_transition_novelty"
                ],

                "downstream_search_performed":
                    True,

                "max_downstream_depth":
                    args.max_depth,
            },

            "generated_at_utc":
                utc_now(),
        }

    write_json(
        output,
        payload,
    )

    print()
    print("=" * 88)

    print(
        "Stage-6 observation-scope "
        "diagnosis: PASS"
    )

    print("=" * 88)

    print(
        f"Fault ID       : "
        f"{fault_id}"
    )

    print(
        f"Source round   : "
        f"{args.source_round}"
    )

    print(
        f"Consumer round : "
        f"{args.consumer_round}"
    )

    print(
        f"Status         : "
        f"{payload['status']}"
    )

    print(
        f"Scope          : "
        f"{payload['scope_decision']}"
    )

    print(
        f"Scope depth    : "
        f"{payload.get('scope_depth')}"
    )

    print(
        "Evidence       : "
        f"{payload.get('evidence_types', []) or 'NONE'}"
    )

    if (
        payload.get(
            "scope_decision"
        )
        == "DOWNSTREAM"
    ):

        selected = (
            payload.get(
                "selected"
            )
            or {}
        )

        print(
            f"Alias          : "
            f"{selected.get('alias')}"
        )

        print(
            f"Internal net   : "
            f"{selected.get('internal_signal')}"
        )

    print(
        f"Feedback JSON  : "
        f"{output}"
    )

    return 0


if __name__ == "__main__":

    try:

        raise SystemExit(
            main()
        )

    except ScopeError as exc:

        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )

        raise SystemExit(2)
