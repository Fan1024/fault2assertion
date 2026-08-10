#!/usr/bin/env python3
"""Profile model-visible local signals on the fault-free Golden execution.

Stage-6 baseline-only tool.

The profiler:
- reads one completed Stage-5 fault specification;
- recovers the target module, site signal, and direct receiver expressions;
- dynamically builds one temporary bind monitor;
- runs only the immutable Golden mapped netlist;
- records local signal values at each positive clock edge after reset;
- aggregates observed joint states and per-receiver one-cycle transitions;
- retains only golden_behavior.json.

It performs:
- no OpenAI/API call;
- no fault simulation;
- no assertion generation;
- no derived Golden-fact inference;
- no VCD retention;
- no permanent raw trace/monitor/Xcelium work retention.
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

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


PASS_STATE = "ORACLE_VALIDATED_CLEANED"

VALID_GOLDEN_STATUSES = {
    "PASS",
    "OUTPUT_MATCH",
}


class ProfileError(RuntimeError):
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
        raise ProfileError(
            f"{label} not found: {path}"
        ) from exc

    except json.JSONDecodeError as exc:
        raise ProfileError(
            f"invalid {label} JSON "
            f"{path}: {exc}"
        ) from exc

    if not isinstance(
        value,
        dict,
    ):
        raise ProfileError(
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


def load_stage5_helpers(
    root: Path,
) -> Any:

    path = (
        root
        / "scripts"
        / "fault_characterization"
        / "stage5_faults_v107_impl.py"
    )

    if not path.is_file():
        raise ProfileError(
            "Stage-5 helper implementation "
            f"not found: {path}"
        )

    module_name = (
        "f2a_stage6_profile_stage5_helpers"
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
        raise ProfileError(
            "cannot import Stage-5 helpers: "
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
        raise ProfileError(
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


def runner_status(
    run_dir: Path,
) -> str | None:

    path = (
        run_dir
        / "result.json"
    )

    if not path.is_file():
        return None

    result = load_json(
        path,
        "Stage-5 Golden runner result",
    )

    value = result.get(
        "status"
    )

    return (
        str(value)
        if value is not None
        else None
    )


def clean_subprocess_env() -> dict[str, str]:

    env = dict(
        os.environ
    )

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


def build_monitor(
    *,
    helpers: Any,
    fault_spec: Mapping[str, Any],
    trace_path: Path,
) -> tuple[str, list[str]]:

    fault_id = str(
        fault_spec.get(
            "fault_id",
            "",
        )
    )

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
        raise ProfileError(
            "fault spec has no site object"
        )

    if (
        not isinstance(
            receivers,
            list,
        )
        or not receivers
    ):
        raise ProfileError(
            "fault spec has no "
            "receiver_signals"
        )

    module_name = str(
        site.get(
            "module",
            "",
        )
    )

    source_net = str(
        site.get(
            "source_net",
            "",
        )
    )

    if (
        not module_name
        or not source_net
    ):
        raise ProfileError(
            "fault site module/source_net "
            "is incomplete"
        )

    aliases = (
        ["site_i"]
        + [
            f"recv_{index}_i"
            for index in range(
                len(receivers)
            )
        ]
    )

    receiver_exprs: list[str] = []

    for raw in receivers:

        if not isinstance(
            raw,
            dict,
        ):
            raise ProfileError(
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
            raise ProfileError(
                "receiver signal has "
                "no expression"
            )

        receiver_exprs.append(
            helpers.sv_expression(
                expression
            )
        )

    tag = hashlib.sha256(
        (
            f"{module_name}:"
            f"{source_net}:"
            + ",".join(
                aliases
            )
        ).encode(
            "utf-8"
        )
    ).hexdigest()[:10]

    package_name = (
        "f2a_stage6_golden_profile_pkg_"
        f"{tag}"
    )

    monitor_name = (
        "f2a_stage6_golden_profile_"
        f"{tag}"
    )

    instance_name = (
        f"{monitor_name}_i"
    )

    ports = [
        "    input wire        f2a_clk_i",
        "    input wire        f2a_rst_ni",
        "    input wire [31:0] f2a_cycle_i",
        "    input wire        site_i",
    ]

    ports.extend(
        f"    input wire        {alias}"
        for alias in aliases[1:]
    )

    bind_lines = [
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
        (
            "    .site_i("
            f"{helpers.sv_expression(source_net)}"
            ")"
        ),
    ]

    bind_lines.extend(
        (
            f"    .{alias}("
            f"{expression}"
            ")"
        )
        for alias, expression in zip(
            aliases[1:],
            receiver_exprs,
        )
    )

    header_fields = [
        "H",
        "F2A_STAGE6_GOLDEN_PROFILE",
        "2",
        "cycle",
        "time",
        "scope",
        *aliases,
    ]

    header_literal = "\\t".join(
        sv_string(
            field
        )
        for field in header_fields
    )

    trace_literal = sv_string(
        str(
            trace_path.resolve()
        )
    )

    format_string = (
        "S\\t%0d\\t%0t\\t%m"
        + "\\t%b" * len(aliases)
        + "\\n"
    )

    value_args = [
        "f2a_cycle_i",
        "$time",
        *aliases,
    ]

    return f"""// Temporary generic Stage-6 Golden local behavioral profiler.
// Generated from Stage-5 fault metadata.
// No fault is injected in this simulation.

package {package_name};

  integer fd = 0;
  bit opened = 1'b0;

  task automatic ensure_open();

    if (!opened) begin

      fd = $fopen(
        "{trace_literal}",
        "w"
      );

      if (fd == 0) begin
        $fatal(
          1,
          "F2A_STAGE6_GOLDEN_PROFILE_OPEN_FAILED"
        );
      end

      opened = 1'b1;

      $fwrite(
        fd,
        "{header_literal}\\n"
      );

      $fflush(fd);

    end

  endtask

endpackage


module {monitor_name} (
{',\n'.join(ports)}
);

  initial begin
    {package_name}::ensure_open();
  end

  always @(posedge f2a_clk_i) begin

    if (f2a_rst_ni === 1'b1) begin

      {package_name}::ensure_open();

      $fwrite(
        {package_name}::fd,
        "{format_string}",
        {', '.join(value_args)}
      );

      $fflush(
        {package_name}::fd
      );

    end

  end

endmodule


bind {helpers.sv_identifier(module_name, 'module')} {monitor_name} {instance_name} (
{',\n'.join(bind_lines)}
);
""", aliases


def is_binary(
    values: list[str],
) -> bool:

    return all(
        value in {
            "0",
            "1",
        }
        for value in values
    )


def parse_trace(
    trace_path: Path,
    expected_aliases: list[str],
) -> dict[str, Any]:

    if (
        not trace_path.is_file()
        or trace_path.stat().st_size == 0
    ):
        raise ProfileError(
            "Golden profile trace "
            "missing or empty: "
            f"{trace_path}"
        )

    state_counts: Counter[str] = (
        Counter()
    )

    transition_counts: dict[
        str,
        Counter[tuple[str, str]],
    ] = {
        alias: Counter()
        for alias
        in expected_aliases[1:]
    }

    previous_binary_by_scope: dict[
        str,
        list[str],
    ] = {}

    scopes: set[str] = set()

    total_samples = 0
    binary_samples = 0
    unknown_samples = 0

    header_seen = False

    with trace_path.open(
        "r",
        encoding="utf-8",
        errors="replace",
    ) as handle:

        for line_number, raw in enumerate(
            handle,
            start=1,
        ):

            line = raw.rstrip(
                "\n"
            )

            if not line:
                continue

            fields = line.split(
                "\t"
            )

            if fields[0] == "H":

                if header_seen:
                    raise ProfileError(
                        "Golden profile trace "
                        "contains multiple headers"
                    )

                expected_header = [
                    "H",
                    "F2A_STAGE6_GOLDEN_PROFILE",
                    "2",
                    "cycle",
                    "time",
                    "scope",
                    *expected_aliases,
                ]

                if fields != expected_header:
                    raise ProfileError(
                        "Golden profile trace "
                        "header mismatch\n"
                        f"  expected: "
                        f"{expected_header}\n"
                        f"  actual:   "
                        f"{fields}"
                    )

                header_seen = True

                continue

            if (
                fields[0] != "S"
                or len(fields)
                != 4 + len(
                    expected_aliases
                )
            ):
                raise ProfileError(
                    "malformed Golden profile "
                    f"sample at line "
                    f"{line_number}: "
                    f"{fields!r}"
                )

            if not header_seen:
                raise ProfileError(
                    "Golden profile sample "
                    "appeared before header"
                )

            scope = fields[3]

            if not scope:
                raise ProfileError(
                    "Golden profile sample "
                    "has empty scope at line "
                    f"{line_number}"
                )

            scopes.add(
                scope
            )

            values = [
                value.lower()
                for value in fields[4:]
            ]

            total_samples += 1

            if not is_binary(
                values
            ):
                unknown_samples += 1

                # Do not create a transition
                # across an X/Z sample.
                previous_binary_by_scope.pop(
                    scope,
                    None,
                )

                continue

            binary_samples += 1

            state = "".join(
                values
            )

            state_counts[
                state
            ] += 1

            previous_binary = (
                previous_binary_by_scope.get(
                    scope
                )
            )

            if (
                previous_binary
                is not None
            ):

                previous_site = (
                    previous_binary[0]
                )

                current_site = (
                    values[0]
                )

                for (
                    index,
                    alias,
                ) in enumerate(
                    expected_aliases[1:],
                    start=1,
                ):

                    before = (
                        previous_site
                        + previous_binary[
                            index
                        ]
                    )

                    after = (
                        current_site
                        + values[
                            index
                        ]
                    )

                    transition_counts[
                        alias
                    ][
                        (
                            before,
                            after,
                        )
                    ] += 1

            previous_binary_by_scope[
                scope
            ] = values

    if not header_seen:
        raise ProfileError(
            "Golden profile trace "
            "has no header"
        )

    if total_samples == 0:
        raise ProfileError(
            "Golden profile trace "
            "contains no samples"
        )

    if binary_samples == 0:
        raise ProfileError(
            "Golden profile contains "
            "no fully binary samples"
        )

    observed_states = [
        {
            "values":
                state,

            "count":
                count,
        }
        for (
            state,
            count,
        ) in sorted(
            state_counts.items()
        )
    ]

    one_cycle_behavior: dict[
        str,
        Any,
    ] = {}

    for alias in (
        expected_aliases[1:]
    ):

        transitions = [
            {
                "from":
                    before,

                "to":
                    after,

                "count":
                    count,
            }
            for (
                (
                    before,
                    after,
                ),
                count,
            ) in sorted(
                transition_counts[
                    alias
                ].items()
            )
        ]

        one_cycle_behavior[
            alias
        ] = {
            "signal_order": [
                "site_i",
                alias,
            ],

            "transitions":
                transitions,
        }

    return {
        "signal_order":
            expected_aliases,

        "sampling": {
            "clock":
                "$root.tb_top.clk",

            "reset":
                "$root.tb_top.rst_n",

            "sampling_event":
                "posedge",

            "sample_source":
                "bound_monitor_procedural_sampling",

            "total_samples":
                total_samples,

            "binary_samples":
                binary_samples,

            "unknown_samples":
                unknown_samples,

            "bound_instance_count":
                len(scopes),
        },

        "bound_scopes":
            sorted(scopes),

        "observed_states":
            observed_states,

        "one_cycle_behavior":
            one_cycle_behavior,
    }


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
        re.fullmatch(
            r"TF\d{6}_SA[01]",
            fault_id,
        )
        is None
    ):
        raise ProfileError(
            f"invalid fault ID: "
            f"{fault_id!r}"
        )

    if args.maxcycles <= 0:
        raise ProfileError(
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
        if args.pilot_dir
        is not None
        else (
            root
            / "runs"
            / "stage6"
            / f"pilot_{fault_id}"
        ).resolve()
    )

    pilot_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path = (
        pilot_dir
        / "golden_behavior.json"
    )

    error_log = (
        pilot_dir
        / "golden_profile_error.log"
    )

    if output_path.exists():
        raise ProfileError(
            "refusing to overwrite "
            "existing Golden behavior: "
            f"{output_path}"
        )

    if error_log.exists():
        error_log.unlink()

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

    if (
        status.get(
            "state"
        )
        != PASS_STATE
    ):
        raise ProfileError(
            "Stage-5 fault is "
            "not complete: "
            f"state="
            f"{status.get('state')!r}"
        )

    mapped = fault_spec.get(
        "mapped_netlist"
    )

    if not isinstance(
        mapped,
        dict,
    ):
        raise ProfileError(
            "fault spec has no "
            "mapped_netlist object"
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
        raise ProfileError(
            "Golden mapped netlist "
            f"not found: "
            f"{golden_netlist}"
        )

    if (
        sha256_file(
            golden_netlist
        )
        != expected_sha
    ):
        raise ProfileError(
            "Golden mapped netlist "
            "SHA-256 mismatch"
        )

    helpers = load_stage5_helpers(
        root
    )

    timestamp = (
        datetime.now(
            timezone.utc
        )
        .strftime(
            "%Y%m%dT%H%M%SZ"
        )
    )

    scratch_root = (
        root
        / "runs"
        / "stage6"
        / (
            f".scratch_profile_"
            f"{fault_id}_"
            f"{timestamp}_"
            f"{os.getpid()}"
        )
    ).resolve()

    scratch_root.mkdir(
        parents=True,
        exist_ok=False,
    )

    monitor_path = (
        scratch_root
        / "golden_profile_monitor.sv"
    )

    trace_path = (
        scratch_root
        / "golden_profile.trace.tsv"
    )

    run_dir = (
        scratch_root
        / "xrun"
    )

    try:

        (
            monitor_text,
            aliases,
        ) = build_monitor(
            helpers=
                helpers,

            fault_spec=
                fault_spec,

            trace_path=
                trace_path,
        )

        monitor_path.write_text(
            monitor_text,
            encoding="utf-8",
        )

        env = clean_subprocess_env()

        env.update(
            {
                "STAGE5_PHASE":
                    "run",

                "STAGE5_RUN_PURPOSE":
                    "NATIVE_CHARACTERIZATION",

                "STAGE5_TRACE_OUTPUT":
                    str(
                        trace_path
                    ),

                "GOLDEN_NETLIST":
                    str(
                        golden_netlist
                    ),

                "MAXCYCLES":
                    str(
                        args.maxcycles
                    ),

                "VCD":
                    "0",

                "KEEP_WORK":
                    "0",
            }
        )

        wrapper = (
            root
            / "scripts"
            / "run_xrun_stage5_golden.sh"
        )

        return_code = (
            run_command(
                [
                    "bash",
                    str(wrapper),
                    str(monitor_path),
                    str(run_dir),
                ],
                env,
            )
        )

        status_value = runner_status(
            run_dir
        )

        if (
            status_value
            not in VALID_GOLDEN_STATUSES
        ):

            xrun_log = (
                run_dir
                / "xrun.log"
            )

            if xrun_log.is_file():
                shutil.copy2(
                    xrun_log,
                    error_log,
                )

            raise ProfileError(
                "Golden profiling "
                "simulation did not "
                "complete cleanly: "
                f"wrapper_return_code="
                f"{return_code}, "
                f"runner_status="
                f"{status_value!r}. "
                f"See {error_log}"
            )

        behavior = parse_trace(
            trace_path,
            aliases,
        )

        payload = {
            "schema_version":
                "1.0",

            "stage":
                "stage_06_golden_behavior_profile",

            "fault_id":
                fault_id,

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

            "profiled_at_utc":
                utc_now(),

            "source": {
                "fault_json":
                    str(
                        fault_json
                    ),

                "fault_json_sha256":
                    sha256_file(
                        fault_json
                    ),

                "golden_netlist":
                    str(
                        golden_netlist
                    ),

                "golden_netlist_sha256":
                    expected_sha,
            },

            "behavior":
                behavior,

            "retention": {
                "vcd_retained":
                    False,

                "raw_trace_retained":
                    False,

                "monitor_retained":
                    False,

                "xcelium_work_retained":
                    False,
            },
        }

        write_json(
            output_path,
            payload,
        )

        print()
        print("=" * 80)

        print(
            "Stage-6 Golden behavioral "
            "profiling: PASS"
        )

        print("=" * 80)

        print(
            f"Fault ID        : "
            f"{fault_id}"
        )

        print(
            "Signals         : "
            + ", ".join(
                aliases
            )
        )

        print(
            "Bound instances : "
            f"{behavior['sampling']['bound_instance_count']}"
        )

        print(
            "Total samples   : "
            f"{behavior['sampling']['total_samples']}"
        )

        print(
            "Binary samples  : "
            f"{behavior['sampling']['binary_samples']}"
        )

        print(
            "Unknown samples : "
            f"{behavior['sampling']['unknown_samples']}"
        )

        print(
            "Observed states : "
            f"{len(behavior['observed_states'])}"
        )

        print(
            f"Output          : "
            f"{output_path}"
        )

        return 0

    finally:

        shutil.rmtree(
            scratch_root,
            ignore_errors=True,
        )


if __name__ == "__main__":

    try:

        raise SystemExit(
            main()
        )

    except ProfileError as exc:

        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )

        raise SystemExit(2)
