#!/usr/bin/env python3
"""Run Stage-6 Round-1 generated assertion.

Sequence:
1. compile/elaborate checker
2. run complete Golden workload
3. if Golden-safe, replay target faulty execution
4. produce TARGET_DETECTED / TARGET_NOT_DETECTED / GOLDEN_FALSE_POSITIVE

Current scope: Stage-5 NATIVE_ONLY faults.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


PASS_STATE = "ORACLE_VALIDATED_CLEANED"

SUPPORTED_BASELINES = {
    "OUTPUT_MATCH",
    "OUTPUT_MISMATCH",
    "TIMEOUT",
}

GENERATED_MARKER = (
    "F2A_STAGE6_ASSERTION_TRIGGERED"
)

GENERATED_FATAL = (
    "F2A_STAGE6_GENERATED_ASSERTION_FATAL"
)

FAULT_ID_RE = re.compile(
    r"^TF\d{6}_SA[01]$"
)

ALIAS_RE = re.compile(
    r"^[A-Za-z_$][A-Za-z0-9_$]*$"
)


class SimulationError(RuntimeError):
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
        raise SimulationError(
            f"{label} not found: {path}"
        ) from exc

    except json.JSONDecodeError as exc:
        raise SimulationError(
            f"invalid {label} JSON "
            f"{path}: {exc}"
        ) from exc

    if not isinstance(
        value,
        dict,
    ):
        raise SimulationError(
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


def import_helpers(
    root: Path,
) -> Any:

    path = (
        root
        / "scripts"
        / "fault_characterization"
        / "stage5_faults_v107_impl.py"
    )

    if not path.is_file():
        raise SimulationError(
            "Stage-5 helper "
            f"not found: {path}"
        )

    name = (
        "f2a_stage6_round1_helpers"
    )

    spec = (
        importlib.util
        .spec_from_file_location(
            name,
            path,
        )
    )

    if (
        spec is None
        or spec.loader is None
    ):
        raise SimulationError(
            "cannot import Stage-5 "
            f"helper: {path}"
        )

    module = (
        importlib.util
        .module_from_spec(
            spec
        )
    )

    sys.modules[
        name
    ] = module

    spec.loader.exec_module(
        module
    )

    return module


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
        raise SimulationError(
            "expected exactly one "
            "fault.json for "
            f"{fault_id}; "
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


def read_property(
    path: Path,
) -> str:

    if (
        not path.is_file()
        or path.stat().st_size == 0
    ):
        raise SimulationError(
            "Round-1 property "
            f"missing/empty: {path}"
        )

    body = (
        path.read_text(
            encoding="utf-8"
        )
        .strip()
    )

    if (
        "BEGIN_SVA" in body
        or "END_SVA" in body
    ):
        raise SimulationError(
            "property file contains "
            "protocol markers"
        )

    if re.search(
        r"\bassert\s+property\b",
        body,
        flags=re.IGNORECASE,
    ):
        raise SimulationError(
            "property file contains "
            "full assert property syntax"
        )

    if body.endswith(";"):
        raise SimulationError(
            "property body ends "
            "with semicolon"
        )

    return body


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
        raise SimulationError(
            "Round-1 context has "
            "no signals"
        )

    output: list[
        dict[str, str]
    ] = []

    for alias, record in signals.items():

        if (
            not isinstance(alias, str)
            or ALIAS_RE.fullmatch(
                alias
            ) is None
        ):
            raise SimulationError(
                f"invalid alias: "
                f"{alias!r}"
            )

        if not isinstance(
            record,
            dict,
        ):
            raise SimulationError(
                f"invalid signal "
                f"record: {alias}"
            )

        expression = record.get(
            "netlist_expression"
        )

        if (
            not isinstance(
                expression,
                str,
            )
            or not expression.strip()
        ):
            raise SimulationError(
                f"alias {alias} "
                "has no expression"
            )

        output.append(
            {
                "alias":
                    alias,

                "expression":
                    expression.strip(),
            }
        )

    aliases = [
        item["alias"]
        for item in output
    ]

    if (
        aliases[0] != "site_i"
        or "down_0_i" not in aliases
    ):
        raise SimulationError(
            "Round-1 aliases must "
            "start with site_i and "
            "include down_0_i"
        )

    return output


def sv_string(
    value: str,
) -> str:

    return (
        value
        .replace(
            "\\",
            "\\\\",
        )
        .replace(
            '"',
            '\\"',
        )
    )


def build_checker(
    *,
    helpers: Any,
    fault_id: str,
    module_name: str,
    signals: list[dict[str, str]],
    property_body: str,
    trace_path: Path,
) -> str:

    tag = hashlib.sha256(
        (
            fault_id
            + ":round1"
        ).encode(
            "utf-8"
        )
    ).hexdigest()[:10]

    monitor_name = (
        f"f2a_stage6_round1_{tag}"
    )

    instance_name = (
        f"{monitor_name}_i"
    )

    ports = [
        "    input wire        f2a_clk_i",
        "    input wire        f2a_rst_ni",
        "    input wire [31:0] f2a_cycle_i",
    ]

    for item in signals:

        ports.append(
            "    input wire        "
            f"{item['alias']}"
        )

    binds = [
        (
            "    .f2a_clk_i("
            f"{helpers.STAGE5_CLOCK_EXPRESSION}"
            ")"
        ),
        (
            "    .f2a_rst_ni("
            "$root.tb_top.rst_n"
            ")"
        ),
        (
            "    .f2a_cycle_i("
            "$root.tb_top.cycle_cnt_q"
            ")"
        ),
    ]

    for item in signals:

        binds.append(
            (
                f"    .{item['alias']}("
                f"{helpers.sv_expression(item['expression'])}"
                ")"
            )
        )

    property_lines = "\n".join(
        "      " + line
        for line
        in property_body.splitlines()
    )

    display_format = (
        f"{GENERATED_MARKER} "
        f"fault_id={fault_id} "
        "round=1 "
        "cycle=%0d time=%0t"
    )

    display_args = [
        "$sampled(f2a_cycle_i)",
        "$time",
    ]

    for item in signals:

        display_format += (
            f" {item['alias']}=%b"
        )

        display_args.append(
            "$sampled("
            f"{item['alias']}"
            ")"
        )

    port_text = ",\n".join(
        ports
    )

    bind_text = ",\n".join(
        binds
    )

    display_arg_text = ",\n      ".join(
        display_args
    )

    trace_literal = sv_string(
        str(
            trace_path.resolve()
        )
    )

    fatal_text = sv_string(
        f"{GENERATED_FATAL} "
        f"fault_id={fault_id} "
        "round=1"
    )

    display_literal = sv_string(
        display_format
    )

    module_sv = (
        helpers.sv_identifier(
            module_name,
            "module",
        )
    )

    return f"""// Auto-generated Stage-6 Round-1 executable checker.

module {monitor_name} (
{port_text}
);

  integer f2a_trace_fd;

  initial begin

    f2a_trace_fd = $fopen(
      "{trace_literal}",
      "w"
    );

    if (f2a_trace_fd == 0) begin

      $fatal(
        1,
        "F2A_STAGE6_TRACE_OPEN_FAILED"
      );

    end

    $fwrite(
      f2a_trace_fd,
      "H\\tF2A_STAGE6_ROUND1_TRACE\\t1\\n"
    );

    $fflush(
      f2a_trace_fd
    );

  end


  a_f2a_stage6_round1: assert property (

    @(posedge f2a_clk_i)

    disable iff (!f2a_rst_ni)

    (
{property_lines}
    )

  )

  else begin

    $display(
      "{display_literal}",
      {display_arg_text}
    );

    $fatal(
      1,
      "{fatal_text}"
    );

  end

endmodule


bind {module_sv} {monitor_name} {instance_name} (
{bind_text}
);
"""


def clean_env() -> dict[str, str]:

    env = dict(
        os.environ
    )

    # Never leak API credentials into
    # Xcelium environment/history.
    for key in list(env):

        if (
            key == "OPENAI_API_KEY"
            or key.startswith(
                "OPENAI_"
            )
            or key == "F2A_OPENAI_ENV"
        ):
            env.pop(
                key,
                None,
            )

    return env


def run_command(
    command: Sequence[str],
    env: Mapping[str, str],
) -> int:

    print(
        "+ "
        + " ".join(
            str(item)
            for item in command
        ),
        flush=True,
    )

    completed = subprocess.run(
        [
            str(item)
            for item in command
        ],
        env=dict(env),
        check=False,
    )

    return int(
        completed.returncode
    )


def runner_status(
    run_dir: Path,
) -> str | None:

    path = (
        run_dir
        / "result.json"
    )

    if not path.is_file():
        return None

    payload = load_json(
        path,
        "runner result",
    )

    value = payload.get(
        "status"
    )

    if value is None:
        return None

    return str(value)


def copy_run_artifacts(
    run_dir: Path,
    result_destination: Path,
    log_destination: Path,
) -> str:

    result_source = (
        run_dir
        / "result.json"
    )

    log_source = (
        run_dir
        / "xrun.log"
    )

    if result_source.is_file():

        shutil.copy2(
            result_source,
            result_destination,
        )

    else:

        write_json(
            result_destination,
            {
                "status":
                    "MISSING",

                "source":
                    str(result_source),
            },
        )

    if log_source.is_file():

        shutil.copy2(
            log_source,
            log_destination,
        )

        return log_source.read_text(
            encoding="utf-8",
            errors="replace",
        )

    log_destination.write_text(
        "F2A_STAGE6_ERROR: "
        "xrun.log missing\n",
        encoding="utf-8",
    )

    return ""


def parse_trigger(
    text: str,
    fault_id: str,
) -> dict[str, Any] | None:

    matches: list[
        dict[str, Any]
    ] = []

    for raw_line in (
        text.splitlines()
    ):

        position = raw_line.find(
            GENERATED_MARKER
        )

        if position < 0:
            continue

        line = raw_line[
            position:
        ].strip()

        tokens = line.split()

        if (
            not tokens
            or tokens[0]
            != GENERATED_MARKER
        ):
            continue

        fields: dict[
            str,
            str,
        ] = {}

        for token in tokens[
            1:
        ]:

            if "=" not in token:
                continue

            key, value = token.split(
                "=",
                1,
            )

            fields[key] = value

        if (
            fields.get("fault_id")
            != fault_id
            or fields.get("round")
            != "1"
        ):
            continue

        try:
            cycle = int(
                fields["cycle"]
            )

        except (
            KeyError,
            ValueError,
        ):
            continue

        sampled = {
            key:
                value
            for key, value
            in fields.items()
            if key not in {
                "fault_id",
                "round",
                "cycle",
                "time",
            }
        }

        matches.append(
            {
                "triggered":
                    True,

                "cycle":
                    cycle,

                "time":
                    fields.get(
                        "time"
                    ),

                "sampled_values":
                    sampled,

                "raw_marker_line":
                    line,
            }
        )

    if not matches:
        return None

    first = dict(
        matches[0]
    )

    first[
        "event_count_in_log"
    ] = len(matches)

    return first


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
        "--pilot-dir",
        type=Path,
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
        args.fault_id.strip()
    )

    if (
        FAULT_ID_RE.fullmatch(
            fault_id
        )
        is None
    ):
        raise SimulationError(
            f"invalid fault ID: "
            f"{fault_id!r}"
        )

    if args.maxcycles <= 0:
        raise SimulationError(
            "--maxcycles must "
            "be positive"
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
        if args.pilot_dir is not None
        else (
            root
            / "runs"
            / "stage6"
            / f"pilot_{fault_id}"
        ).resolve()
    )

    context_path = (
        pilot_dir
        / "round1_context.json"
    )

    property_path = (
        pilot_dir
        / "round1_property.sva"
    )

    feedback_path = (
        pilot_dir
        / "round1_downstream_feedback.json"
    )

    context = load_json(
        context_path,
        "Round-1 context",
    )

    feedback = load_json(
        feedback_path,
        "Round-1 feedback",
    )

    if (
        feedback.get(
            "privileged_train_only"
        )
        is not True
    ):
        raise SimulationError(
            "Round-1 feedback is not "
            "marked Train-only"
        )

    property_body = read_property(
        property_path
    )

    signals = signal_records(
        context
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
        "Stage-5 status",
    )

    routing = load_json(
        fault_dir
        / "routing.json",
        "Stage-5 routing",
    )

    if (
        status.get("state")
        != PASS_STATE
    ):
        raise SimulationError(
            "Stage-5 fault is not "
            "ORACLE_VALIDATED_CLEANED"
        )

    baseline_status = str(
        status.get(
            "native_status",
            "",
        )
    )

    if (
        baseline_status
        not in SUPPORTED_BASELINES
    ):
        raise SimulationError(
            "unsupported Stage-5 "
            "baseline status: "
            f"{baseline_status}"
        )

    if (
        routing.get("route")
        != "NATIVE_ONLY"
    ):
        raise SimulationError(
            "first Round-1 runner "
            "supports NATIVE_ONLY only"
        )

    site = fault_spec.get(
        "site"
    )

    mapped = fault_spec.get(
        "mapped_netlist"
    )

    if (
        not isinstance(site, dict)
        or not isinstance(mapped, dict)
    ):
        raise SimulationError(
            "fault spec missing "
            "site/mapped netlist"
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

    expected_sha = str(
        mapped.get(
            "sha256",
            "",
        )
    )

    if not golden_netlist.is_file():
        raise SimulationError(
            "Golden netlist not found: "
            f"{golden_netlist}"
        )

    if (
        sha256_file(
            golden_netlist
        )
        != expected_sha
    ):
        raise SimulationError(
            "Golden netlist SHA changed"
        )

    checker_path = (
        pilot_dir
        / "round1_checker.sv"
    )

    compile_result_path = (
        pilot_dir
        / "round1_compile.json"
    )

    compile_log_path = (
        pilot_dir
        / "round1_compile.log"
    )

    golden_result_path = (
        pilot_dir
        / "round1_golden.json"
    )

    golden_log_path = (
        pilot_dir
        / "round1_golden.log"
    )

    faulty_result_path = (
        pilot_dir
        / "round1_faulty.json"
    )

    faulty_log_path = (
        pilot_dir
        / "round1_faulty.log"
    )

    simulation_path = (
        pilot_dir
        / "round1_simulation.json"
    )

    outputs = [
        checker_path,
        compile_result_path,
        compile_log_path,
        golden_result_path,
        golden_log_path,
        faulty_result_path,
        faulty_log_path,
        simulation_path,
    ]

    existing = [
        path
        for path in outputs
        if path.exists()
    ]

    if existing:
        raise SimulationError(
            "refusing to overwrite "
            "existing Round-1 "
            "simulation artifacts:\n  "
            + "\n  ".join(
                str(path)
                for path in existing
            )
        )

    helpers = import_helpers(
        root
    )

    trace_path = (
        pilot_dir
        / ".round1_stage6.trace.tsv"
    )

    trace_path.unlink(
        missing_ok=True
    )

    checker = build_checker(
        helpers=helpers,
        fault_id=fault_id,
        module_name=module_name,
        signals=signals,
        property_body=property_body,
        trace_path=trace_path,
    )

    checker_path.write_text(
        checker,
        encoding="utf-8",
    )

    timestamp = (
        datetime.now(
            timezone.utc
        ).strftime(
            "%Y%m%dT%H%M%SZ"
        )
    )

    scratch = (
        root
        / "runs"
        / "stage6"
        / (
            f".scratch_round1_"
            f"{fault_id}_"
            f"{timestamp}_"
            f"{os.getpid()}"
        )
    ).resolve()

    scratch.mkdir(
        parents=True,
        exist_ok=False,
    )

    compile_run = (
        scratch
        / "compile"
    )

    golden_run = (
        scratch
        / "golden"
    )

    faulty_run = (
        scratch
        / "faulty"
    )

    golden_wrapper = (
        root
        / "scripts"
        / "run_xrun_stage5_golden.sh"
    )

    fault_wrapper = (
        root
        / "scripts"
        / "run_xrun_stage5_fault.sh"
    )

    base_env = clean_env()

    summary: dict[
        str,
        Any,
    ] = {
        "schema_version":
            "1.0",

        "stage":
            "stage_06_round1_simulation",

        "fault_id":
            fault_id,

        "round":
            1,

        "started_at_utc":
            utc_now(),

        "feedback_type":
            "bounded_downstream_strong_teacher_v1",

        "privileged_train_only":
            True,

        "signal_aliases": [
            item["alias"]
            for item in signals
        ],

        "property": {
            "path":
                str(property_path),

            "sha256":
                sha256_file(
                    property_path
                ),
        },

        "context": {
            "path":
                str(context_path),

            "sha256":
                sha256_file(
                    context_path
                ),
        },

        "stage5_reference": {
            "route":
                routing.get("route"),

            "native_status":
                baseline_status,
        },

        "compile":
            None,

        "golden":
            None,

        "faulty":
            None,

        "verdict":
            "NOT_COMPLETED",

        "retention": {
            "vcd":
                False,

            "fault_netlist":
                False,

            "trace":
                False,

            "xcelium_work":
                False,

            "scratch":
                False,
        },
    }

    try:

        # -------------------------------------------------------------
        # Gate 1: compile/elaboration
        # -------------------------------------------------------------

        compile_env = dict(
            base_env
        )

        compile_env.update(
            {
                "STAGE5_PHASE":
                    "compile",

                "STAGE5_RUN_PURPOSE":
                    "COMPILE_CHECK",

                "STAGE5_TRACE_OUTPUT":
                    str(trace_path),

                "GOLDEN_NETLIST":
                    str(golden_netlist),

                "MAXCYCLES":
                    str(args.maxcycles),

                "VCD":
                    "0",

                "KEEP_WORK":
                    "0",
            }
        )

        compile_rc = run_command(
            [
                "bash",
                str(golden_wrapper),
                str(checker_path),
                str(compile_run),
            ],
            compile_env,
        )

        compile_text = (
            copy_run_artifacts(
                compile_run,
                compile_result_path,
                compile_log_path,
            )
        )

        del compile_text

        compile_status = runner_status(
            compile_run
        )

        summary["compile"] = {
            "wrapper_return_code":
                compile_rc,

            "runner_status":
                compile_status,
        }

        if (
            compile_status
            != "COMPILE_PASS"
        ):
            summary["verdict"] = (
                "COMPILE_FAILED"
            )

            summary[
                "completed_at_utc"
            ] = utc_now()

            write_json(
                simulation_path,
                summary,
            )

            print()
            print("=" * 80)
            print(
                "Stage-6 Round-1 verdict: "
                "COMPILE_FAILED"
            )
            print("=" * 80)

            return 2

        trace_path.unlink(
            missing_ok=True
        )

        # -------------------------------------------------------------
        # Gate 2: complete Golden execution
        # -------------------------------------------------------------

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
                    str(trace_path),

                "GOLDEN_NETLIST":
                    str(golden_netlist),

                "MAXCYCLES":
                    str(args.maxcycles),

                "VCD":
                    "0",

                "KEEP_WORK":
                    "0",
            }
        )

        golden_rc = run_command(
            [
                "bash",
                str(golden_wrapper),
                str(checker_path),
                str(golden_run),
            ],
            golden_env,
        )

        golden_text = (
            copy_run_artifacts(
                golden_run,
                golden_result_path,
                golden_log_path,
            )
        )

        golden_status = runner_status(
            golden_run
        )

        golden_trigger = parse_trigger(
            golden_text,
            fault_id,
        )

        summary["golden"] = {
            "wrapper_return_code":
                golden_rc,

            "runner_status":
                golden_status,

            "generated_assertion":
                (
                    golden_trigger
                    if golden_trigger
                    is not None
                    else {
                        "triggered":
                            False
                    }
                ),
        }

        trace_path.unlink(
            missing_ok=True
        )

        if (
            golden_trigger
            is not None
        ):

            summary["verdict"] = (
                "GOLDEN_FALSE_POSITIVE"
            )

            summary[
                "completed_at_utc"
            ] = utc_now()

            write_json(
                simulation_path,
                summary,
            )

            print()
            print("=" * 80)
            print(
                "Stage-6 Round-1 verdict: "
                "GOLDEN_FALSE_POSITIVE"
            )
            print("=" * 80)

            print(
                "Trigger cycle : "
                f"{golden_trigger['cycle']}"
            )

            print(
                "Values        : "
                f"{golden_trigger['sampled_values']}"
            )

            print(
                f"Result JSON   : "
                f"{simulation_path}"
            )

            return 0

        if (
            golden_status
            not in {
                "PASS",
                "OUTPUT_MATCH",
            }
        ):

            summary["verdict"] = (
                "GOLDEN_EXECUTION_FAILED"
            )

            summary[
                "completed_at_utc"
            ] = utc_now()

            write_json(
                simulation_path,
                summary,
            )

            print()
            print("=" * 80)
            print(
                "Stage-6 Round-1 verdict: "
                "GOLDEN_EXECUTION_FAILED"
            )
            print("=" * 80)

            return 2

        # -------------------------------------------------------------
        # Gate 3: target faulty execution
        # -------------------------------------------------------------

        faulty_env = dict(
            base_env
        )

        faulty_env.update(
            {
                "STAGE5_PHASE":
                    "run",

                "STAGE5_RUN_PURPOSE":
                    "NATIVE_CHARACTERIZATION",

                "STAGE5_TRACE_OUTPUT":
                    str(trace_path),

                "MAXCYCLES":
                    str(args.maxcycles),

                "VCD":
                    "0",

                "KEEP_WORK":
                    "0",
            }
        )

        faulty_rc = run_command(
            [
                "bash",
                str(fault_wrapper),
                str(fault_json),
                str(checker_path),
                str(faulty_run),
            ],
            faulty_env,
        )

        faulty_text = (
            copy_run_artifacts(
                faulty_run,
                faulty_result_path,
                faulty_log_path,
            )
        )

        faulty_status = runner_status(
            faulty_run
        )

        faulty_trigger = parse_trigger(
            faulty_text,
            fault_id,
        )

        summary["faulty"] = {
            "wrapper_return_code":
                faulty_rc,

            "runner_status":
                faulty_status,

            "expected_stage5_native_status":
                baseline_status,

            "generated_assertion":
                (
                    faulty_trigger
                    if faulty_trigger
                    is not None
                    else {
                        "triggered":
                            False
                    }
                ),
        }

        trace_path.unlink(
            missing_ok=True
        )

        if (
            faulty_trigger
            is not None
        ):

            summary["verdict"] = (
                "TARGET_DETECTED"
            )

        elif (
            faulty_status
            == baseline_status
        ):

            summary["verdict"] = (
                "TARGET_NOT_DETECTED"
            )

        else:

            summary["verdict"] = (
                "FAULT_EXECUTION_REPLAY_MISMATCH"
            )

        summary[
            "completed_at_utc"
        ] = utc_now()

        write_json(
            simulation_path,
            summary,
        )

        print()
        print("=" * 80)

        print(
            "Stage-6 Round-1 verdict: "
            f"{summary['verdict']}"
        )

        print("=" * 80)

        print(
            f"Fault ID        : "
            f"{fault_id}"
        )

        print(
            "Aliases         : "
            + ", ".join(
                item["alias"]
                for item in signals
            )
        )

        print(
            f"Stage-5 baseline: "
            f"{baseline_status}"
        )

        print(
            f"Compile         : "
            f"{compile_status}"
        )

        print(
            f"Golden          : "
            f"{golden_status}"
        )

        print(
            f"Faulty runner   : "
            f"{faulty_status}"
        )

        if (
            faulty_trigger
            is not None
        ):

            print(
                "Detection cycle : "
                f"{faulty_trigger['cycle']}"
            )

            print(
                "Detection time  : "
                f"{faulty_trigger['time']}"
            )

            print(
                "Sampled values  : "
                f"{faulty_trigger['sampled_values']}"
            )

        print(
            f"Result JSON     : "
            f"{simulation_path}"
        )

        return (
            0
            if summary["verdict"]
            in {
                "TARGET_DETECTED",
                "TARGET_NOT_DETECTED",
            }
            else 2
        )

    finally:

        trace_path.unlink(
            missing_ok=True
        )

        shutil.rmtree(
            scratch,
            ignore_errors=True,
        )


if __name__ == "__main__":

    try:
        raise SystemExit(
            main()
        )

    except SimulationError as exc:

        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )

        raise SystemExit(2)
