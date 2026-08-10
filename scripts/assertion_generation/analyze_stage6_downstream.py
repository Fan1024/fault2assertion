#!/usr/bin/env python3
"""Stage-6 Train-only bounded downstream diagnostic expansion.

For a Round-0 TARGET_NOT_DETECTED fault, this tool:
1. rebuilds the already-validated Stage-1 structural inventory;
2. follows standard-cell receiver outputs from the injected site to depth 2/3;
3. profiles the original aliases plus all bounded downstream candidates on one
   Golden and one target-fault execution;
4. selects the earliest downstream depth containing a candidate that both
   diverges from Golden and adds fault-only observed behavior;
5. writes one compact round1_downstream_feedback.json.

This is privileged Train feedback. It must not be used for Dev/Test target
faults when target-fault execution feedback is disallowed.
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

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


PASS_STATE = "ORACLE_VALIDATED_CLEANED"
ROUND0_REQUIRED_VERDICT = "TARGET_NOT_DETECTED"
VALID_GOLDEN_STATUSES = {
    "PASS",
    "OUTPUT_MATCH",
}

FAULT_ID_RE = re.compile(
    r"^TF\d{6}_SA[01]$"
)


class DownstreamError(RuntimeError):
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
        raise DownstreamError(
            f"{label} not found: {path}"
        ) from exc

    except json.JSONDecodeError as exc:
        raise DownstreamError(
            f"invalid {label} JSON "
            f"{path}: {exc}"
        ) from exc

    if not isinstance(
        value,
        dict,
    ):
        raise DownstreamError(
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

    temporary.replace(path)


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
            digest.update(block)

    return digest.hexdigest()


def import_module(
    path: Path,
    module_name: str,
) -> Any:

    if not path.is_file():
        raise DownstreamError(
            f"Python module not found: {path}"
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
        raise DownstreamError(
            f"cannot import Python module: {path}"
        )

    module = (
        importlib.util
        .module_from_spec(spec)
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
        raise DownstreamError(
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

    return (
        str(value)
        if value is not None
        else None
    )


def find_instance(
    parsed: Any,
    module_name: str,
    instance_name: str,
) -> Any:

    module = parsed.modules.get(
        module_name
    )

    if module is None:
        raise DownstreamError(
            "module not found in "
            f"parsed netlist: {module_name}"
        )

    matches = [
        item
        for item in module.instances
        if item.instance
        == instance_name
    ]

    if len(matches) != 1:
        raise DownstreamError(
            "expected one instance "
            f"{module_name}/{instance_name}; "
            f"found {len(matches)}"
        )

    return matches[0]


def direct_receiver_outputs(
    *,
    module_name: str,
    source_key: str,
    inventory_by_node:
        Mapping[
            tuple[str, str],
            Mapping[str, Any],
        ],
    parsed: Any,
    policy: Any,
    catalog: Any,
) -> list[dict[str, Any]]:

    raw_site = (
        inventory_by_node.get(
            (
                module_name,
                source_key,
            )
        )
    )

    if raw_site is None:
        return []

    sinks = raw_site.get(
        "sinks"
    )

    if not isinstance(
        sinks,
        list,
    ):
        return []

    found: dict[
        str,
        dict[str, Any],
    ] = {}

    for sink in sinks:

        if (
            not isinstance(
                sink,
                dict,
            )
            or sink.get(
                "kind"
            )
            != "standard_cell_input"
        ):
            continue

        instance_name = str(
            sink.get(
                "instance",
                "",
            )
        )

        instance = find_instance(
            parsed,
            module_name,
            instance_name,
        )

        output_pins = (
            policy.output_pins(
                instance.cell_type
            )
        )

        for connection in (
            instance.connections
        ):

            if (
                connection.pin
                not in output_pins
            ):
                continue

            expression = (
                connection.expression
                .strip()
            )

            if (
                catalog.is_constant(
                    expression
                )
                or not
                catalog.is_simple_signal(
                    expression
                )
            ):
                continue

            key = (
                catalog.canonical_signal(
                    expression
                )
            )

            if not key:
                continue

            found.setdefault(
                key,
                {
                    "module":
                        module_name,

                    "expression":
                        expression,

                    "source_key":
                        key,

                    "receiver_instance":
                        instance_name,

                    "receiver_cell_type":
                        instance.cell_type,

                    "receiver_input_pin":
                        sink.get(
                            "pin"
                        ),

                    "receiver_output_pin":
                        connection.pin,

                    "receiver_input_role":
                        sink.get(
                            "role"
                        ),
                },
            )

    return [
        found[key]
        for key in sorted(
            found
        )
    ]


def discover_layers(
    *,
    start_module: str,
    start_expression: str,
    max_depth: int,
    inventory_by_node:
        Mapping[
            tuple[str, str],
            Mapping[str, Any],
        ],
    parsed: Any,
    policy: Any,
    catalog: Any,
) -> dict[
    int,
    list[dict[str, Any]],
]:

    start_key = (
        catalog.canonical_signal(
            start_expression
        )
    )

    start = (
        start_module,
        start_key,
    )

    seen: set[
        tuple[str, str]
    ] = {
        start
    }

    frontier: list[
        tuple[str, str]
    ] = [
        start
    ]

    layers: dict[
        int,
        list[dict[str, Any]],
    ] = {}

    for depth in range(
        1,
        max_depth + 1,
    ):

        next_by_node: dict[
            tuple[str, str],
            dict[str, Any],
        ] = {}

        for (
            parent_module,
            parent_key,
        ) in frontier:

            children = (
                direct_receiver_outputs(
                    module_name=
                        parent_module,

                    source_key=
                        parent_key,

                    inventory_by_node=
                        inventory_by_node,

                    parsed=
                        parsed,

                    policy=
                        policy,

                    catalog=
                        catalog,
                )
            )

            for child in children:

                node = (
                    str(
                        child[
                            "module"
                        ]
                    ),
                    str(
                        child[
                            "source_key"
                        ]
                    ),
                )

                if node in seen:
                    continue

                row = dict(
                    child
                )

                row[
                    "depth"
                ] = depth

                row[
                    "parent_module"
                ] = parent_module

                row[
                    "parent_source_key"
                ] = parent_key

                next_by_node[
                    node
                ] = row

        layer = [
            next_by_node[node]
            for node in sorted(
                next_by_node
            )
        ]

        layers[
            depth
        ] = layer

        frontier = [
            (
                str(
                    item["module"]
                ),
                str(
                    item[
                        "source_key"
                    ]
                ),
            )
            for item in layer
        ]

        seen.update(
            frontier
        )

        if not frontier:

            for remaining in range(
                depth + 1,
                max_depth + 1,
            ):
                layers[
                    remaining
                ] = []

            break

    return layers


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


def build_monitor(
    *,
    helpers: Any,
    module_name: str,
    alias_records:
        list[dict[str, str]],
    trace_path: Path,
) -> str:

    width = len(
        alias_records
    )

    if width <= 0:
        raise DownstreamError(
            "monitor has no signals"
        )

    tag = hashlib.sha256(
        (
            module_name
            + ":"
            + ",".join(
                item["alias"]
                for item
                in alias_records
            )
        ).encode(
            "utf-8"
        )
    ).hexdigest()[:10]

    package_name = (
        "f2a_stage6_downstream_pkg_"
        f"{tag}"
    )

    monitor_name = (
        "f2a_stage6_downstream_"
        f"{tag}"
    )

    instance_name = (
        f"{monitor_name}_i"
    )

    expressions = [
        helpers.sv_expression(
            item[
                "expression"
            ]
        )
        for item
        in alias_records
    ]

    if width == 1:
        concat = (
            expressions[0]
        )

    else:
        concat = (
            "{"
            + ", ".join(
                expressions
            )
            + "}"
        )

    header = "\t".join(
        [
            "H",
            "F2A_STAGE6_DOWNSTREAM",
            "1",
            "cycle",
            "time",
            "scope",
            *[
                item["alias"]
                for item
                in alias_records
            ],
        ]
    )

    trace_literal = (
        sv_string(
            str(
                trace_path.resolve()
            )
        )
    )

    return f"""`timescale 1ns/1ps

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
          "F2A_STAGE6_DOWNSTREAM_TRACE_OPEN_FAILED"
        );
      end

      opened = 1'b1;

      $fwrite(
        fd,
        "{sv_string(header)}\\n"
      );

      $fflush(fd);

    end

  endtask

endpackage


module {monitor_name} (
    input wire f2a_clk_i,
    input wire f2a_rst_ni,
    input wire [31:0] f2a_cycle_i,
    input wire [{width - 1}:0] signals_i
);

  logic prev_valid = 1'b0;
  logic [{width - 1}:0] prev_signals;

  initial begin
    {package_name}::ensure_open();
  end

  always @(posedge f2a_clk_i) begin

    if (f2a_rst_ni === 1'b1) begin

      if (
        !prev_valid
        || prev_signals !== signals_i
      ) begin

        {package_name}::ensure_open();

        $fwrite(
          {package_name}::fd,
          "S\\t%0d\\t%0t\\t%m\\t%b\\n",
          f2a_cycle_i,
          $time,
          signals_i
        );

        $fflush(
          {package_name}::fd
        );

        prev_valid = 1'b1;
        prev_signals = signals_i;

      end

    end

  end

endmodule


bind {helpers.sv_identifier(module_name, 'module')} {monitor_name} {instance_name} (
    .f2a_clk_i({helpers.STAGE5_CLOCK_EXPRESSION}),
    .f2a_rst_ni($root.tb_top.rst_n),
    .f2a_cycle_i($root.tb_top.cycle_cnt_q),
    .signals_i({concat})
);
"""


def normalize_scope(
    scope: str,
) -> str:

    value = scope.strip()

    if "." in value:
        value = value.rsplit(
            ".",
            1,
        )[0]

    return value


def parse_trace(
    path: Path,
    aliases: list[str],
) -> dict[
    str,
    list[
        tuple[
            int,
            int,
            str,
        ]
    ],
]:

    if (
        not path.is_file()
        or path.stat().st_size
        == 0
    ):
        raise DownstreamError(
            "trace missing or empty: "
            f"{path}"
        )

    expected_header = [
        "H",
        "F2A_STAGE6_DOWNSTREAM",
        "1",
        "cycle",
        "time",
        "scope",
        *aliases,
    ]

    header_seen = False

    series: dict[
        str,
        list[
            tuple[
                int,
                int,
                str,
            ]
        ],
    ] = defaultdict(
        list
    )

    with path.open(
        "r",
        encoding="utf-8",
        errors="replace",
    ) as handle:

        for (
            line_number,
            raw,
        ) in enumerate(
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

            if (
                fields[0]
                == "H"
            ):

                if (
                    header_seen
                    or fields
                    != expected_header
                ):
                    raise DownstreamError(
                        "downstream trace "
                        "header mismatch at "
                        f"line {line_number}: "
                        f"{fields!r}"
                    )

                header_seen = True
                continue

            if not header_seen:
                raise DownstreamError(
                    "downstream trace "
                    "sample appeared "
                    "before header"
                )

            if (
                fields[0]
                != "S"
                or len(fields)
                != 5
            ):
                raise DownstreamError(
                    "malformed downstream "
                    "trace row at line "
                    f"{line_number}: "
                    f"{fields!r}"
                )

            cycle = int(
                fields[1]
            )

            time_value = fields[2].strip()

            if not time_value:
                raise DownstreamError(
                    "empty simulation time "
                    "in downstream trace "
                    f"at line {line_number}"
                )

            scope = (
                normalize_scope(
                    fields[3]
                )
            )

            bits = (
                fields[4]
                .lower()
            )

            if (
                len(bits)
                != len(
                    aliases
                )
            ):
                raise DownstreamError(
                    "trace vector width "
                    "mismatch at line "
                    f"{line_number}: "
                    "expected "
                    f"{len(aliases)}, "
                    "got "
                    f"{len(bits)}"
                )

            rows = series[
                scope
            ]

            if (
                rows
                and cycle
                < rows[-1][0]
            ):
                raise DownstreamError(
                    "non-monotonic trace "
                    "cycle in scope "
                    f"{scope}"
                )

            if (
                rows
                and rows[-1][2]
                == bits
            ):
                continue

            rows.append(
                (
                    cycle,
                    time_value,
                    bits,
                )
            )

    if (
        not header_seen
        or not series
    ):
        raise DownstreamError(
            "trace contains no "
            "usable samples: "
            f"{path}"
        )

    return dict(
        series
    )


def binary(
    bits: str,
) -> bool:

    return (
        bool(bits)
        and all(
            char
            in {
                "0",
                "1",
            }
            for char in bits
        )
    )


def state_set(
    rows:
        list[
            tuple[
                int,
                int,
                str,
            ]
        ],
    indices: list[int],
) -> set[str]:

    result: set[str] = set()

    for _, _, bits in rows:

        value = "".join(
            bits[index]
            for index
            in indices
        )

        if binary(
            value
        ):
            result.add(
                value
            )

    return result


def pair_transition_set(
    rows:
        list[
            tuple[
                int,
                int,
                str,
            ]
        ],
    first_index: int,
    second_index: int,
) -> set[str]:

    result: set[str] = set()

    previous_cycle: (
        int | None
    ) = None

    previous_pair: (
        str | None
    ) = None

    for (
        cycle,
        _,
        bits,
    ) in rows:

        pair = (
            bits[
                first_index
            ]
            + bits[
                second_index
            ]
        )

        if not binary(
            pair
        ):
            previous_cycle = None
            previous_pair = None
            continue

        if (
            previous_cycle
            is not None
            and previous_pair
            is not None
        ):

            if (
                cycle
                > previous_cycle
                + 1
            ):
                result.add(
                    f"{previous_pair}"
                    f"->{previous_pair}"
                )

            if (
                cycle
                > previous_cycle
            ):
                result.add(
                    f"{previous_pair}"
                    f"->{pair}"
                )

        previous_cycle = cycle
        previous_pair = pair

    return result


def aligned_first_divergences(
    golden_rows:
        list[
            tuple[
                int,
                int,
                str,
            ]
        ],
    fault_rows:
        list[
            tuple[
                int,
                int,
                str,
            ]
        ],
    width: int,
) -> tuple[
    dict[
        int,
        dict[str, Any],
    ],
    dict[str, Any] | None,
]:

    g_by_cycle = {
        cycle:
            (
                time_value,
                bits,
            )
        for (
            cycle,
            time_value,
            bits,
        )
        in golden_rows
    }

    f_by_cycle = {
        cycle:
            (
                time_value,
                bits,
            )
        for (
            cycle,
            time_value,
            bits,
        )
        in fault_rows
    }

    max_common = min(
        max(g_by_cycle),
        max(f_by_cycle),
    )

    cycles = sorted(
        cycle
        for cycle in (
            set(g_by_cycle)
            | set(f_by_cycle)
        )
        if cycle <= max_common
    )

    g_bits: (
        str | None
    ) = None

    f_bits: (
        str | None
    ) = None

    g_time = 0
    f_time = 0

    first: dict[
        int,
        dict[str, Any],
    ] = {}

    first_any: (
        dict[str, Any]
        | None
    ) = None

    for cycle in cycles:

        if cycle in g_by_cycle:
            (
                g_time,
                g_bits,
            ) = g_by_cycle[
                cycle
            ]

        if cycle in f_by_cycle:
            (
                f_time,
                f_bits,
            ) = f_by_cycle[
                cycle
            ]

        if (
            g_bits is None
            or f_bits is None
        ):
            continue

        if (
            not binary(
                g_bits
            )
            or not binary(
                f_bits
            )
        ):
            continue

        if (
            g_bits != f_bits
            and first_any
            is None
        ):
            first_any = {
                "cycle":
                    cycle,

                "golden_time":
                    g_time,

                "fault_time":
                    f_time,

                "golden_bits":
                    g_bits,

                "fault_bits":
                    f_bits,
            }

        for index in range(
            width
        ):

            if (
                index
                not in first
                and g_bits[
                    index
                ]
                != f_bits[
                    index
                ]
            ):
                first[
                    index
                ] = {
                    "cycle":
                        cycle,

                    "golden_time":
                        g_time,

                    "fault_time":
                        f_time,

                    "golden_value":
                        g_bits[
                            index
                        ],

                    "fault_value":
                        f_bits[
                            index
                        ],

                    "golden_bits":
                        g_bits,

                    "fault_bits":
                        f_bits,
                }

    return (
        first,
        first_any,
    )


def values_by_alias(
    aliases: list[str],
    bits: str,
) -> dict[str, str]:

    return {
        alias:
            bits[index]
        for (
            index,
            alias,
        ) in enumerate(
            aliases
        )
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
        FAULT_ID_RE.fullmatch(
            fault_id
        )
        is None
    ):
        raise DownstreamError(
            f"invalid fault ID: "
            f"{fault_id!r}"
        )

    if (
        args.max_depth
        not in {
            2,
            3,
        }
    ):
        raise DownstreamError(
            "--max-depth must be "
            "2 or 3 for this "
            "bounded pilot"
        )

    if (
        args.max_candidates
        <= 0
        or args.maxcycles
        <= 0
    ):
        raise DownstreamError(
            "--max-candidates and "
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

    stage1_catalog = (
        args.stage1_catalog
        .expanduser()
        .resolve()
    )

    output_path = (
        pilot_dir
        / "round1_downstream_feedback.json"
    )

    error_log = (
        pilot_dir
        / "round1_downstream_error.log"
    )

    if output_path.exists():
        raise DownstreamError(
            "refusing to overwrite "
            "existing feedback: "
            f"{output_path}"
        )

    error_log.unlink(
        missing_ok=True
    )

    round0 = load_json(
        pilot_dir
        / "round0_simulation.json",
        "Round-0 simulation",
    )

    if (
        round0.get(
            "verdict"
        )
        != ROUND0_REQUIRED_VERDICT
    ):
        raise DownstreamError(
            "downstream feedback "
            "requires Round-0 "
            f"{ROUND0_REQUIRED_VERDICT}; "
            "got "
            f"{round0.get('verdict')!r}"
        )

    visible = load_json(
        pilot_dir
        / "visible_context.json",
        "frozen Stage-6 visible context",
    )

    signals = visible.get(
        "signals"
    )

    if (
        not isinstance(
            signals,
            dict,
        )
        or "site_i"
        not in signals
    ):
        raise DownstreamError(
            "visible_context.json "
            "has no usable "
            "signals object"
        )

    base_aliases = list(
        signals.keys()
    )

    if (
        base_aliases[0]
        != "site_i"
    ):
        raise DownstreamError(
            "site_i must be "
            "the first frozen "
            "baseline alias"
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

    if (
        status.get(
            "state"
        )
        != PASS_STATE
    ):
        raise DownstreamError(
            "Stage-5 fault is not "
            "ORACLE_VALIDATED_CLEANED"
        )

    if (
        routing.get(
            "route"
        )
        != "NATIVE_ONLY"
    ):
        raise DownstreamError(
            "this first downstream "
            "pilot is intentionally "
            "NATIVE_ONLY"
        )

    expected_fault_status = str(
        status.get(
            "native_status",
            "",
        )
    )

    site = fault_spec.get(
        "site"
    )

    receivers = fault_spec.get(
        "receiver_signals"
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
            receivers,
            list,
        )
        or not isinstance(
            mapped,
            dict,
        )
    ):
        raise DownstreamError(
            "fault spec is missing "
            "site/receiver/"
            "mapped-netlist metadata"
        )

    module_name = str(
        site.get(
            "module",
            "",
        )
    )

    site_expression = str(
        site.get(
            "source_net",
            "",
        )
    )

    if (
        not module_name
        or not site_expression
    ):
        raise DownstreamError(
            "fault site "
            "module/source_net "
            "is incomplete"
        )

    catalog = import_module(
        root
        / "scripts"
        / "fault_sites"
        / "site_catalog.py",
        "f2a_stage6_downstream_site_catalog",
    )

    (
        _stage1_payload,
        stage1_netlist,
        policy,
        parsed,
        rebuilt,
    ) = (
        catalog
        .load_and_rebuild_stage1_catalog(
            stage1_catalog
        )
    )

    if (
        sha256_file(
            stage1_netlist
        )
        != str(
            mapped.get(
                "sha256",
                "",
            )
        )
    ):
        raise DownstreamError(
            "Stage-1 catalog and "
            "Stage-5 fault do not "
            "reference the same "
            "mapped netlist"
        )

    inventory_by_node = {
        (
            str(
                item["module"]
            ),
            str(
                item["source_key"]
            ),
        ):
            item
        for item in (
            rebuilt.raw_sites
        )
    }

    layers = discover_layers(
        start_module=
            module_name,

        start_expression=
            site_expression,

        max_depth=
            args.max_depth,

        inventory_by_node=
            inventory_by_node,

        parsed=
            parsed,

        policy=
            policy,

        catalog=
            catalog,
    )

    stage5_direct_keys = {
        catalog.canonical_signal(
            str(
                item.get(
                    "expression",
                    "",
                )
            )
        )
        for item in receivers
        if (
            isinstance(
                item,
                dict,
            )
            and str(
                item.get(
                    "role",
                    "",
                )
            )
            == "direct_receiver_output"
        )
    }

    discovered_depth1_keys = {
        str(
            item[
                "source_key"
            ]
        )
        for item in (
            layers.get(
                1,
                [],
            )
        )
    }

    if (
        stage5_direct_keys
        and stage5_direct_keys
        != discovered_depth1_keys
    ):
        raise DownstreamError(
            "Stage-6 downstream "
            "depth-1 reconstruction "
            "does not match "
            "Stage-5 direct receivers\n"
            "  Stage-5: "
            f"{sorted(stage5_direct_keys)}\n"
            "  rebuilt: "
            f"{sorted(discovered_depth1_keys)}"
        )

    base_keys = {
        catalog.canonical_signal(
            str(
                record.get(
                    "netlist_expression",
                    "",
                )
            )
        )
        for record
        in signals.values()
        if isinstance(
            record,
            dict,
        )
    }

    candidates: list[
        dict[str, Any]
    ] = []

    for depth in range(
        2,
        args.max_depth + 1,
    ):

        for item in (
            layers.get(
                depth,
                [],
            )
        ):

            if (
                str(
                    item["module"]
                )
                != module_name
            ):
                continue

            if (
                str(
                    item[
                        "source_key"
                    ]
                )
                in base_keys
            ):
                continue

            candidates.append(
                dict(item)
            )

    candidates.sort(
        key=lambda item: (
            int(
                item["depth"]
            ),
            str(
                item[
                    "source_key"
                ]
            ),
        )
    )

    if not candidates:

        write_json(
            output_path,
            {
                "schema_version":
                    "1.0",

                "stage":
                    "stage_06_round1_downstream_feedback",

                "fault_id":
                    fault_id,

                "status":
                    "NO_DOWNSTREAM_CANDIDATES",

                "max_depth":
                    args.max_depth,

                "candidate_count_by_depth": {
                    str(depth):
                        len(
                            layers.get(
                                depth,
                                [],
                            )
                        )
                    for depth
                    in range(
                        1,
                        args.max_depth
                        + 1,
                    )
                },

                "selected":
                    None,

                "generated_at_utc":
                    utc_now(),
            },
        )

        print(
            "Stage-6 downstream result: "
            "NO_DOWNSTREAM_CANDIDATES"
        )

        return 0

    if (
        len(candidates)
        > args.max_candidates
    ):
        raise DownstreamError(
            "bounded depth-"
            f"{args.max_depth} cone "
            f"contains {len(candidates)} "
            "new candidates, exceeding "
            "--max-candidates="
            f"{args.max_candidates}; "
            "do not silently truncate "
            "the cone"
        )

    alias_records: list[
        dict[str, str]
    ] = []

    for alias in base_aliases:

        record = signals[
            alias
        ]

        if not isinstance(
            record,
            dict,
        ):
            raise DownstreamError(
                "invalid visible "
                "signal record: "
                f"{alias}"
            )

        expression = str(
            record.get(
                "netlist_expression",
                "",
            )
        )

        if not expression:
            raise DownstreamError(
                "visible alias has "
                "no expression: "
                f"{alias}"
            )

        alias_records.append(
            {
                "alias":
                    alias,

                "expression":
                    expression,
            }
        )

    for (
        index,
        candidate,
    ) in enumerate(
        candidates
    ):

        candidate_alias = (
            f"cand_{index:03d}_i"
        )

        candidate[
            "profile_alias"
        ] = candidate_alias

        alias_records.append(
            {
                "alias":
                    candidate_alias,

                "expression":
                    str(
                        candidate[
                            "expression"
                        ]
                    ),
            }
        )

    helpers = import_module(
        root
        / "scripts"
        / "fault_characterization"
        / "stage5_faults_v107_impl.py",
        "f2a_stage6_downstream_stage5_helpers",
    )

    timestamp = (
        datetime.now(
            timezone.utc
        )
        .strftime(
            "%Y%m%dT%H%M%SZ"
        )
    )

    scratch = (
        root
        / "runs"
        / "stage6"
        / (
            f".scratch_downstream_"
            f"{fault_id}_"
            f"{timestamp}_"
            f"{os.getpid()}"
        )
    ).resolve()

    scratch.mkdir(
        parents=True,
        exist_ok=False,
    )

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

    try:

        golden_monitor.write_text(
            build_monitor(
                helpers=
                    helpers,

                module_name=
                    module_name,

                alias_records=
                    alias_records,

                trace_path=
                    golden_trace,
            ),
            encoding="utf-8",
        )

        fault_monitor.write_text(
            build_monitor(
                helpers=
                    helpers,

                module_name=
                    module_name,

                alias_records=
                    alias_records,

                trace_path=
                    fault_trace,
            ),
            encoding="utf-8",
        )

        base_env = (
            clean_subprocess_env()
        )

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
                        Path(
                            str(
                                mapped[
                                    "path"
                                ]
                            )
                        ).resolve()
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

            log = (
                golden_run
                / "xrun.log"
            )

            if log.is_file():
                shutil.copy2(
                    log,
                    error_log,
                )

            raise DownstreamError(
                "Golden downstream "
                "profiling failed: "
                f"rc={golden_rc}, "
                "status="
                f"{golden_status!r}; "
                f"see {error_log}"
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
                        args.maxcycles
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

            log = (
                fault_run
                / "xrun.log"
            )

            if log.is_file():
                shutil.copy2(
                    log,
                    error_log,
                )

            raise DownstreamError(
                "fault downstream profiling "
                "did not replay the "
                "validated Stage-5 outcome: "
                "expected="
                f"{expected_fault_status}, "
                "actual="
                f"{fault_status}, "
                f"rc={fault_rc}; "
                f"see {error_log}"
            )

        aliases = [
            item["alias"]
            for item
            in alias_records
        ]

        golden_series = (
            parse_trace(
                golden_trace,
                aliases,
            )
        )

        fault_series = (
            parse_trace(
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
            len(common_scopes)
            != 1
        ):
            raise DownstreamError(
                "expected exactly one "
                "common bound scope for "
                "this pilot; found "
                f"{common_scopes}"
            )

        scope = (
            common_scopes[0]
        )

        g_rows = (
            golden_series[
                scope
            ]
        )

        f_rows = (
            fault_series[
                scope
            ]
        )

        (
            first_by_index,
            first_any,
        ) = (
            aligned_first_divergences(
                g_rows,
                f_rows,
                len(aliases),
            )
        )

        base_width = len(
            base_aliases
        )

        base_indices = list(
            range(
                base_width
            )
        )

        golden_base_states = (
            state_set(
                g_rows,
                base_indices,
            )
        )

        fault_base_states = (
            state_set(
                f_rows,
                base_indices,
            )
        )

        base_fault_only_states = (
            sorted(
                fault_base_states
                - golden_base_states
            )
        )

        evaluated: list[
            dict[str, Any]
        ] = []

        for (
            offset,
            candidate,
        ) in enumerate(
            candidates
        ):

            index = (
                base_width
                + offset
            )

            expanded_indices = [
                *base_indices,
                index,
            ]

            g_states = (
                state_set(
                    g_rows,
                    expanded_indices,
                )
            )

            f_states = (
                state_set(
                    f_rows,
                    expanded_indices,
                )
            )

            fault_only = sorted(
                f_states
                - g_states
            )

            added_fault_only = sorted(
                state
                for state
                in fault_only
                if (
                    state[
                        :base_width
                    ]
                    in golden_base_states
                )
            )

            g_transitions = (
                pair_transition_set(
                    g_rows,
                    0,
                    index,
                )
            )

            f_transitions = (
                pair_transition_set(
                    f_rows,
                    0,
                    index,
                )
            )

            fault_only_transitions = (
                sorted(
                    f_transitions
                    - g_transitions
                )
            )

            first = (
                first_by_index.get(
                    index
                )
            )

            evaluated.append(
                {
                    "profile_alias":
                        candidate[
                            "profile_alias"
                        ],

                    "depth":
                        int(
                            candidate[
                                "depth"
                            ]
                        ),

                    "module":
                        str(
                            candidate[
                                "module"
                            ]
                        ),

                    "expression":
                        str(
                            candidate[
                                "expression"
                            ]
                        ),

                    "source_key":
                        str(
                            candidate[
                                "source_key"
                            ]
                        ),

                    "receiver_instance":
                        candidate.get(
                            "receiver_instance"
                        ),

                    "receiver_cell_type":
                        candidate.get(
                            "receiver_cell_type"
                        ),

                    "earliest_divergence_cycle":
                        (
                            first.get(
                                "cycle"
                            )
                            if first
                            else None
                        ),

                    "fault_only_expanded_states":
                        fault_only,

                    "candidate_added_fault_only_states":
                        added_fault_only,

                    "fault_only_site_candidate_transitions":
                        fault_only_transitions,

                    "golden_expanded_states":
                        sorted(
                            g_states
                        ),

                    "golden_site_candidate_transitions":
                        sorted(
                            g_transitions
                        ),
                }
            )

        useful = [
            item
            for item
            in evaluated
            if (
                item[
                    "earliest_divergence_cycle"
                ]
                is not None
                and (
                    item[
                        "candidate_added_fault_only_states"
                    ]
                    or item[
                        "fault_only_site_candidate_transitions"
                    ]
                )
            )
        ]

        selected: (
            dict[str, Any]
            | None
        ) = None

        if useful:

            earliest_depth = min(
                int(
                    item[
                        "depth"
                    ]
                )
                for item
                in useful
            )

            at_depth = [
                item
                for item
                in useful
                if int(
                    item[
                        "depth"
                    ]
                )
                == earliest_depth
            ]

            at_depth.sort(
                key=lambda item: (
                    (
                        0
                        if item[
                            "candidate_added_fault_only_states"
                        ]
                        else 1
                    ),

                    int(
                        item[
                            "earliest_divergence_cycle"
                        ]
                    ),

                    str(
                        item[
                            "source_key"
                        ]
                    ),
                )
            )

            chosen = dict(
                at_depth[0]
            )

            chosen_index = (
                aliases.index(
                    str(
                        chosen[
                            "profile_alias"
                        ]
                    )
                )
            )

            first = (
                first_by_index[
                    chosen_index
                ]
            )

            chosen[
                "alias"
            ] = "down_0_i"

            selected_aliases = [
                *base_aliases,
                "down_0_i",
            ]

            selected_indices = [
                *base_indices,
                chosen_index,
            ]

            golden_bits = "".join(
                first[
                    "golden_bits"
                ][index]
                for index
                in selected_indices
            )

            fault_bits = "".join(
                first[
                    "fault_bits"
                ][index]
                for index
                in selected_indices
            )

            chosen[
                "earliest_divergence"
            ] = {
                "cycle":
                    first[
                        "cycle"
                    ],

                "golden_values":
                    values_by_alias(
                        selected_aliases,
                        golden_bits,
                    ),

                "fault_values":
                    values_by_alias(
                        selected_aliases,
                        fault_bits,
                    ),
            }

            golden_transitions = dict(
                visible.get(
                    "golden_behavior",
                    {},
                ).get(
                    "one_cycle_transitions",
                    {},
                )
            )

            golden_transitions[
                "down_0_i"
            ] = chosen[
                "golden_site_candidate_transitions"
            ]

            chosen[
                "expanded_golden_behavior"
            ] = {
                "signal_order":
                    selected_aliases,

                "observed_states":
                    chosen[
                        "golden_expanded_states"
                    ],

                "one_cycle_transitions":
                    golden_transitions,
            }

            selected = chosen

        compact_evaluated = [
            {
                "depth":
                    item["depth"],

                "expression":
                    item[
                        "expression"
                    ],

                "earliest_divergence_cycle":
                    item[
                        "earliest_divergence_cycle"
                    ],

                "added_fault_only_state_count":
                    len(
                        item[
                            "candidate_added_fault_only_states"
                        ]
                    ),

                "fault_only_transition_count":
                    len(
                        item[
                            "fault_only_site_candidate_transitions"
                        ]
                    ),
            }
            for item
            in evaluated
        ]

        payload = {
            "schema_version":
                "1.0",

            "stage":
                "stage_06_round1_downstream_feedback",

            "fault_id":
                fault_id,

            "generated_at_utc":
                utc_now(),

            "privileged_train_only":
                True,

            "round0_verdict":
                ROUND0_REQUIRED_VERDICT,

            "search_policy": {
                "kind":
                    "bounded_standard_cell_receiver_output_expansion",

                "max_depth":
                    args.max_depth,

                "same_module_only":
                    True,

                "silent_candidate_truncation":
                    False,
            },

            "structural_summary": {
                "depth1_count":
                    len(
                        layers.get(
                            1,
                            [],
                        )
                    ),

                "depth2_count":
                    len(
                        layers.get(
                            2,
                            [],
                        )
                    ),

                "depth3_count":
                    len(
                        layers.get(
                            3,
                            [],
                        )
                    ),

                "profiled_new_candidate_count":
                    len(
                        candidates
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

            "base_aliases":
                base_aliases,

            "base_fault_only_observed_states":
                base_fault_only_states,

            "first_any_monitored_divergence":
                (
                    None
                    if first_any
                    is None
                    else {
                        "cycle":
                            first_any[
                                "cycle"
                            ],

                        "golden_values":
                            values_by_alias(
                                aliases,
                                first_any[
                                    "golden_bits"
                                ],
                            ),

                        "fault_values":
                            values_by_alias(
                                aliases,
                                first_any[
                                    "fault_bits"
                                ],
                            ),
                    }
                ),

            "selection_rule":
                (
                    "earliest downstream depth with "
                    "time-aligned divergence and either "
                    "candidate-added fault-only expanded "
                    "state or fault-only (site,candidate) "
                    "one-cycle transition; prefer state "
                    "novelty, then earliest divergence"
                ),

            "status":
                (
                    "DOWNSTREAM_CANDIDATE_FOUND"
                    if selected
                    else
                    "NO_DISCRIMINATIVE_DOWNSTREAM_CANDIDATE"
                ),

            "selected":
                selected,

            "evaluated_candidates":
                compact_evaluated,

            "retention": {
                "vcd_retained":
                    False,

                "raw_trace_retained":
                    False,

                "temporary_monitor_retained":
                    False,

                "fault_netlist_retained":
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
            "Stage-6 bounded downstream "
            "diagnostic expansion"
        )

        print("=" * 80)

        print(
            f"Fault ID          : "
            f"{fault_id}"
        )

        print(
            f"Depth 1           : "
            f"{len(layers.get(1, []))} "
            "signal(s)"
        )

        print(
            f"Depth 2           : "
            f"{len(layers.get(2, []))} "
            "signal(s)"
        )

        print(
            f"Depth 3           : "
            f"{len(layers.get(3, []))} "
            "signal(s)"
        )

        print(
            f"Profiled new      : "
            f"{len(candidates)} "
            "signal(s)"
        )

        print(
            "Base fault-only   : "
            f"{base_fault_only_states or 'NONE'}"
        )

        print(
            f"Status            : "
            f"{payload['status']}"
        )

        if selected is not None:

            print(
                "Selected alias    : "
                "down_0_i"
            )

            print(
                f"Selected depth    : "
                f"{selected['depth']}"
            )

            print(
                f"Selected signal   : "
                f"{selected['expression']}"
            )

            print(
                "First divergence  : "
                "cycle "
                f"{selected['earliest_divergence_cycle']}"
            )

            print(
                "Added states      : "
                + (
                    ", ".join(
                        selected[
                            "candidate_added_fault_only_states"
                        ]
                    )
                    or "NONE"
                )
            )

            print(
                "Fault-only trans  : "
                + (
                    ", ".join(
                        selected[
                            "fault_only_site_candidate_transitions"
                        ]
                    )
                    or "NONE"
                )
            )

        print(
            f"Feedback JSON     : "
            f"{output_path}"
        )

        return 0

    finally:

        shutil.rmtree(
            scratch,
            ignore_errors=True,
        )


if __name__ == "__main__":

    try:
        raise SystemExit(
            main()
        )

    except DownstreamError as exc:

        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )

        raise SystemExit(2)
