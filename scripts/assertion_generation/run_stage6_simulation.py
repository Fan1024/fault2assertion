#!/usr/bin/env python3
"""Run one Stage-6 generated property for Round 0 or later Train rounds.

The executable checker is infrastructure-owned. Signal aliases and mapped
expressions are read from the round context, so feedback rounds may add bounded
observation aliases such as down_0_i without modifying the DUT or Stage-5 fault
specification.

Current scope: NATIVE_ONLY Stage-5 faults.
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
            "implementation not found: "
            f"{path}"
        )

    name = (
        "f2a_stage6_generic_stage5_helpers"
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
            "cannot import "
            f"Stage-5 helpers: {path}"
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
            "Stage-5 fault.json for "
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
        or path.stat().st_size
        == 0
    ):
        raise SimulationError(
            "generated property "
            f"missing or empty: {path}"
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
            "property file must "
            "contain only the "
            "property body"
        )

    if re.search(
        r"\bassert\s+property\b",
        body,
        flags=re.IGNORECASE,
    ):
        raise SimulationError(
            "property file "
            "unexpectedly contains "
            "assert property"
        )

    if body.endswith(";"):
        raise SimulationError(
            "property body must not "
            "end with a semicolon"
        )

    return body


def context_for_round(
    pilot_dir: Path,
    round_index: int,
) -> Path:

    if round_index == 0:
        return (
            pilot_dir
            / "visible_context.json"
        )

    return (
        pilot_dir
        / f"round{round_index}_context.json"
    )


def signal_records(
    context: Mapping[str, Any],
    module_name: str,
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
            "round context has "
            "no signals object"
        )

    records: list[
        dict[str, str]
    ] = []

    for (
        alias,
        raw,
    ) in signals.items():

        if (
            not isinstance(
                alias,
                str,
            )
            or ALIAS_RE.fullmatch(
                alias
            )
            is None
        ):
            raise SimulationError(
                "invalid checker alias: "
                f"{alias!r}"
            )

        if not isinstance(
            raw,
            dict,
        ):
            raise SimulationError(
                "invalid signal record "
                f"for alias {alias}"
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
            raise SimulationError(
                f"alias {alias} "
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
        not records
        or records[0][
            "alias"
        ]
        != "site_i"
    ):
        raise SimulationError(
            "site_i must be the "
            "first round-context "
            "signal alias"
        )

    fault = context.get(
        "fault"
    )

    if isinstance(
        fault,
        dict,
    ):

        context_module = (
            fault.get(
                "module"
            )
        )

        if (
            context_module
            is not None
            and str(
                context_module
            )
            != module_name
        ):
            raise SimulationError(
                "context/fault module "
                "mismatch: "
                f"{context_module!r} "
                "!= "
                f"{module_name!r}"
            )

    return records


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
    round_index: int,
    module_name: str,
    signals:
        list[dict[str, str]],
    property_body: str,
    trace_path: Path,
) -> str:

    tag = hashlib.sha256(
        (
            f"{fault_id}:"
            f"round{round_index}"
        ).encode(
            "utf-8"
        )
    ).hexdigest()[:10]

    monitor_name = (
        f"f2a_stage6_round"
        f"{round_index}_{tag}"
    )

    instance_name = (
        f"{monitor_name}_i"
    )

    ports = [
        "    input wire        f2a_clk_i",
        "    input wire        f2a_rst_ni",
        "    input wire [31:0] f2a_cycle_i",
    ]

    ports.extend(
        "    input wire        "
        f"{item['alias']}"
        for item in signals
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

    binds.extend(
        (
            f"    .{item['alias']}("
            f"{helpers.sv_expression(item['expression'])}"
            ")"
        )
        for item in signals
    )

    property_lines = "\n".join(
        "      " + line
        for line
        in property_body.splitlines()
    )

    display_format = (
        f"{GENERATED_MARKER} "
        f"fault_id={fault_id} "
        f"round={round_index} "
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

    trace_literal = (
        sv_string(
            str(
                trace_path.resolve()
            )
        )
    )

    fatal_text = (
        sv_string(
            f"{GENERATED_FATAL} "
            f"fault_id={fault_id} "
            f"round={round_index}"
        )
    )

    return f"""// Auto-generated Stage-6 executable checker.

module {monitor_name} (
{',\n'.join(ports)}
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
      "H\\tF2A_STAGE6_TRACE\\t1\\n"
    );

    $fflush(
      f2a_trace_fd
    );

  end

  a_f2a_stage6_round{round_index}: assert property (

    @(posedge f2a_clk_i)

    disable iff (!f2a_rst_ni)

    (
{property_lines}
    )

  )

  else begin

    $display(
      "{sv_string(display_format)}",
      {', '.join(display_args)}
    );

    $fatal(
      1,
      "{fatal_text}"
    );

  end

endmodule


bind {helpers.sv_identifier(module_name, 'module')} {monitor_name} {instance_name} (
{',\n'.join(binds)}
);
"""


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

    result = load_json(
        path,
        "Stage-5 runner result",
    )

    value = result.get(
        "status"
    )

    return (
        str(value)
        if value is not None
        else None
    )


def copy_log(
    run_dir: Path,
    destination: Path,
) -> str:

    source = (
        run_dir
        / "xrun.log"
    )

    if source.is_file():

        shutil.copy2(
            source,
            destination,
        )

        return source.read_text(
            encoding="utf-8",
            errors="replace",
        )

    destination.write_text(
        "F2A_STAGE6_ERROR: "
        "xrun.log missing\n",
        encoding="utf-8",
    )

    return ""


def copy_result(
    run_dir: Path,
    destination: Path,
    label: str,
) -> None:

    source = (
        run_dir
        / "result.json"
    )

    if source.is_file():

        shutil.copy2(
            source,
            destination,
        )

    else:

        write_json(
            destination,
            {
                "status":
                    "MISSING",

                "label":
                    label,

                "source":
                    str(source),
            },
        )


def parse_generated_event(
    text: str,
    fault_id: str,
    round_index: int,
) -> dict[str, Any] | None:
    """Recognize failure of THIS generated Stage-6 assertion.

    Primary path:
        Parse the explicit F2A marker emitted by the generated
        assertion action block.

    Fallback path:
        Xcelium may finalize a strong/liveness assertion at
        simulation termination (for example s_eventually) without
        executing the assertion action block. In that case Xcelium
        emits *F,ASRTST directly.

        Accept that fallback only when the ASRTST line identifies all
        of the following Stage-6-owned objects for this exact fault
        and round:

          * roundN_checker.sv
          * deterministic generated monitor instance
          * a_f2a_stage6_roundN assertion label

        This strict ownership check prevents pre-existing DUT/TB
        assertions from being misclassified as the generated Stage-6
        assertion.
    """

    events: list[
        dict[str, Any]
    ] = []

    # ------------------------------------------------------------
    # Primary path: explicit Stage-6 marker.
    # ------------------------------------------------------------

    for raw in text.splitlines():

        position = raw.find(
            GENERATED_MARKER
        )

        if position < 0:
            continue

        marker = (
            raw[
                position:
            ]
            .strip()
        )

        tokens = marker.split()

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

        for token in (
            tokens[1:]
        ):

            if "=" not in token:
                continue

            (
                key,
                value,
            ) = token.split(
                "=",
                1,
            )

            fields[
                key
            ] = value

        if (
            fields.get(
                "fault_id"
            )
            != fault_id
            or fields.get(
                "round"
            )
            != str(
                round_index
            )
        ):
            continue

        try:

            cycle = int(
                fields[
                    "cycle"
                ]
            )

        except (
            KeyError,
            ValueError,
        ):
            continue

        values = {
            key:
                value

            for (
                key,
                value,
            ) in fields.items()

            if key
            not in {
                "fault_id",
                "round",
                "cycle",
                "time",
            }
        }

        events.append(
            {
                "triggered":
                    True,

                "source":
                    "F2A_MARKER",

                "fault_id":
                    fault_id,

                "round":
                    round_index,

                "cycle":
                    cycle,

                "time":
                    fields.get(
                        "time"
                    ),

                "sampled_values":
                    values,

                "raw_marker_line":
                    marker,
            }
        )

    if events:

        first = dict(
            events[
                0
            ]
        )

        first[
            "event_count_in_log"
        ] = len(
            events
        )

        return first

    # ------------------------------------------------------------
    # Fallback path:
    #
    # A strong/liveness assertion can remain pending until $finish.
    # Xcelium can then report *F,ASRTST directly, without executing
    # the checker action block and therefore without printing the
    # F2A marker above.
    #
    # Match ONLY this exact generated Stage-6 assertion.
    # ------------------------------------------------------------

    tag = hashlib.sha256(
        (
            f"{fault_id}:"
            f"round{round_index}"
        ).encode(
            "utf-8"
        )
    ).hexdigest()[
        :10
    ]

    checker_name = (
        f"round{round_index}"
        "_checker.sv"
    )

    monitor_instance = (
        f"f2a_stage6_round"
        f"{round_index}_"
        f"{tag}_i"
    )

    assertion_label = (
        f"a_f2a_stage6_round"
        f"{round_index}"
    )

    fallback_events: list[
        dict[str, Any]
    ] = []

    for raw in text.splitlines():

        if (
            "*F,ASRTST"
            not in raw
        ):
            continue

        # Strict ownership:
        # all three generated identifiers
        # must be present on the same
        # Xcelium ASRTST diagnostic line.
        if (
            checker_name
            not in raw
            or monitor_instance
            not in raw
            or assertion_label
            not in raw
            or "has failed"
            not in raw
        ):
            continue

        time_match = re.search(
            r"\(time\s+([^)]+)\)",
            raw,
        )

        failure_window = re.search(
            r"has failed\s+\("
            r"(\d+)\s+cycles?"
            r"(?:,\s+starting\s+([^)]+))?"
            r"\)",
            raw,
            flags=re.IGNORECASE,
        )

        event: dict[
            str,
            Any,
        ] = {
            "triggered":
                True,

            "source":
                "XCELIUM_ASRTST_FALLBACK",

            "fault_id":
                fault_id,

            "round":
                round_index,

            # Xcelium does not provide the
            # Stage-6 sampled cycle counter
            # in this termination-time
            # diagnostic. Do not infer one.
            "cycle":
                None,

            "time":
                (
                    time_match.group(
                        1
                    ).strip()
                    if time_match
                    else None
                ),

            "sampled_values":
                {},

            "raw_marker_line":
                raw.strip(),
        }

        if failure_window:

            event[
                "xcelium_failure_window"
            ] = {
                "cycles":
                    int(
                        failure_window.group(
                            1
                        )
                    ),

                "starting":
                    (
                        failure_window.group(
                            2
                        ).strip()
                        if failure_window.group(
                            2
                        )
                        else None
                    ),
            }

        fallback_events.append(
            event
        )

    if not fallback_events:
        return None

    first = dict(
        fallback_events[
            0
        ]
    )

    first[
        "event_count_in_log"
    ] = len(
        fallback_events
    )

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
        "--round",
        type=int,
        default=0,
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

    round_index = (
        args.round
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

    if (
        round_index < 0
        or args.maxcycles <= 0
    ):
        raise SimulationError(
            "--round must be >=0 "
            "and --maxcycles "
            "must be positive"
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
        raise SimulationError(
            "Stage-6 pilot "
            f"not found: {pilot_dir}"
        )

    manifest = load_json(
        pilot_dir
        / "manifest.json",
        "Stage-6 baseline manifest",
    )

    if (
        manifest.get(
            "stage"
        )
        != "stage_06_baseline_ready"
        or manifest.get(
            "baseline_frozen"
        )
        is not True
    ):
        raise SimulationError(
            "Stage-6 baseline "
            "is not frozen/ready"
        )

    if (
        manifest.get(
            "fault_id"
        )
        != fault_id
    ):
        raise SimulationError(
            "baseline manifest "
            "fault_id mismatch"
        )

    context_path = (
        context_for_round(
            pilot_dir,
            round_index,
        )
    )

    context = load_json(
        context_path,
        f"Round-{round_index} context",
    )

    property_path = (
        pilot_dir
        / (
            f"round{round_index}"
            "_property.sva"
        )
    )

    property_body = (
        read_property(
            property_path
        )
    )

    prefix = (
        f"round{round_index}"
    )

    checker_path = (
        pilot_dir
        / f"{prefix}_checker.sv"
    )

    compile_json = (
        pilot_dir
        / f"{prefix}_compile.json"
    )

    compile_log = (
        pilot_dir
        / f"{prefix}_compile.log"
    )

    golden_json = (
        pilot_dir
        / f"{prefix}_golden.json"
    )

    golden_log = (
        pilot_dir
        / f"{prefix}_golden.log"
    )

    faulty_json = (
        pilot_dir
        / f"{prefix}_faulty.json"
    )

    faulty_log = (
        pilot_dir
        / f"{prefix}_faulty.log"
    )

    simulation_json = (
        pilot_dir
        / f"{prefix}_simulation.json"
    )

    outputs = [
        checker_path,
        compile_json,
        compile_log,
        golden_json,
        golden_log,
        faulty_json,
        faulty_log,
        simulation_json,
    ]

    existing = [
        path
        for path in outputs
        if path.exists()
    ]

    if existing:
        raise SimulationError(
            "refusing to overwrite "
            "existing simulation "
            "artifacts:\n  "
            + "\n  ".join(
                str(path)
                for path in existing
            )
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
        raise SimulationError(
            "Stage-5 fault "
            "is not complete"
        )

    baseline_status = str(
        status.get(
            "native_status",
            "",
        )
    )

    if (
        baseline_status
        not in
        SUPPORTED_BASELINES
        or routing.get(
            "route"
        )
        != "NATIVE_ONLY"
    ):
        raise SimulationError(
            "this generic Stage-6 "
            "runner currently supports "
            "NATIVE_ONLY "
            "scientific outcomes"
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
        raise SimulationError(
            "fault spec is missing "
            "site/mapped-netlist "
            "metadata"
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

    if (
        not golden_netlist.is_file()
        or sha256_file(
            golden_netlist
        )
        != expected_sha
    ):
        raise SimulationError(
            "Golden mapped netlist "
            "is missing or changed"
        )

    signals = (
        signal_records(
            context,
            module_name,
        )
    )

    helpers = (
        import_helpers(
            root
        )
    )

    trace_path = (
        pilot_dir
        / (
            f".{prefix}"
            "_stage6.trace.tsv"
        )
    )

    trace_path.unlink(
        missing_ok=True
    )

    checker_path.write_text(
        build_checker(
            helpers=
                helpers,

            fault_id=
                fault_id,

            round_index=
                round_index,

            module_name=
                module_name,

            signals=
                signals,

            property_body=
                property_body,

            trace_path=
                trace_path,
        ),
        encoding="utf-8",
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
            f".scratch_"
            f"{fault_id}_"
            f"round{round_index}_"
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

    base_env = (
        clean_subprocess_env()
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

    result: dict[
        str,
        Any,
    ] = {
        "schema_version":
            "1.0",

        "stage":
            "stage_06_assertion_simulation",

        "fault_id":
            fault_id,

        "round":
            round_index,

        "started_at_utc":
            utc_now(),

        "context": {
            "path":
                str(
                    context_path
                ),

            "sha256":
                sha256_file(
                    context_path
                ),
        },

        "property": {
            "path":
                str(
                    property_path
                ),

            "sha256":
                sha256_file(
                    property_path
                ),
        },

        "checker": {
            "path":
                str(
                    checker_path
                ),

            "sha256":
                sha256_file(
                    checker_path
                ),
        },

        "signal_aliases": [
            item["alias"]
            for item
            in signals
        ],

        "stage5_reference": {
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
                baseline_status,
        },

        "vcd_retained":
            False,

        "fault_netlist_retained":
            False,

        "scratch_retained":
            False,

        "compile":
            None,

        "golden":
            None,

        "faulty":
            None,

        "verdict":
            "NOT_COMPLETED",
    }

    try:

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

        compile_rc = (
            run_command(
                [
                    "bash",

                    str(
                        golden_wrapper
                    ),

                    str(
                        checker_path
                    ),

                    str(
                        compile_run
                    ),
                ],
                compile_env,
            )
        )

        copy_log(
            compile_run,
            compile_log,
        )

        copy_result(
            compile_run,
            compile_json,
            "compile result",
        )

        compile_status = (
            runner_status(
                compile_run
            )
        )

        result[
            "compile"
        ] = {
            "wrapper_return_code":
                compile_rc,

            "runner_status":
                compile_status,
        }

        if (
            compile_status
            != "COMPILE_PASS"
        ):

            result[
                "verdict"
            ] = "COMPILE_FAILED"

            result[
                "completed_at_utc"
            ] = utc_now()

            write_json(
                simulation_json,
                result,
            )

            print(
                "Stage-6 verdict: "
                "COMPILE_FAILED"
            )

            return 2

        trace_path.unlink(
            missing_ok=True
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

        golden_rc = (
            run_command(
                [
                    "bash",

                    str(
                        golden_wrapper
                    ),

                    str(
                        checker_path
                    ),

                    str(
                        golden_run
                    ),
                ],
                golden_env,
            )
        )

        golden_text = (
            copy_log(
                golden_run,
                golden_log,
            )
        )

        copy_result(
            golden_run,
            golden_json,
            "golden result",
        )

        golden_status = (
            runner_status(
                golden_run
            )
        )

        golden_event = (
            parse_generated_event(
                golden_text,
                fault_id,
                round_index,
            )
        )

        result[
            "golden"
        ] = {
            "wrapper_return_code":
                golden_rc,

            "runner_status":
                golden_status,

            "generated_assertion":
                (
                    golden_event
                    or {
                        "triggered":
                            False
                    }
                ),
        }

        trace_path.unlink(
            missing_ok=True
        )

        if (
            golden_event
            is not None
        ):

            result[
                "verdict"
            ] = (
                "GOLDEN_FALSE_POSITIVE"
            )

            result[
                "completed_at_utc"
            ] = utc_now()

            write_json(
                simulation_json,
                result,
            )

            print(
                "Stage-6 verdict: "
                "GOLDEN_FALSE_POSITIVE"
            )

            return 0

        if (
            golden_status
            not in {
                "PASS",
                "OUTPUT_MATCH",
            }
        ):

            result[
                "verdict"
            ] = (
                "GOLDEN_EXECUTION_FAILED"
            )

            result[
                "completed_at_utc"
            ] = utc_now()

            write_json(
                simulation_json,
                result,
            )

            print(
                "Stage-6 verdict: "
                "GOLDEN_EXECUTION_FAILED"
            )

            return 2

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
                    str(
                        trace_path
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

        faulty_rc = (
            run_command(
                [
                    "bash",

                    str(
                        fault_wrapper
                    ),

                    str(
                        fault_json
                    ),

                    str(
                        checker_path
                    ),

                    str(
                        faulty_run
                    ),
                ],
                faulty_env,
            )
        )

        faulty_text = (
            copy_log(
                faulty_run,
                faulty_log,
            )
        )

        copy_result(
            faulty_run,
            faulty_json,
            "faulty result",
        )

        faulty_status = (
            runner_status(
                faulty_run
            )
        )

        faulty_event = (
            parse_generated_event(
                faulty_text,
                fault_id,
                round_index,
            )
        )

        result[
            "faulty"
        ] = {
            "wrapper_return_code":
                faulty_rc,

            "runner_status":
                faulty_status,

            "expected_stage5_native_status":
                baseline_status,

            "generated_assertion":
                (
                    faulty_event
                    or {
                        "triggered":
                            False
                    }
                ),
        }

        trace_path.unlink(
            missing_ok=True
        )

        if (
            faulty_event
            is not None
        ):
            result[
                "verdict"
            ] = "TARGET_DETECTED"

        elif (
            faulty_status
            == baseline_status
        ):
            result[
                "verdict"
            ] = (
                "TARGET_NOT_DETECTED"
            )

        else:
            result[
                "verdict"
            ] = (
                "FAULT_EXECUTION_REPLAY_MISMATCH"
            )

        result[
            "completed_at_utc"
        ] = utc_now()

        write_json(
            simulation_json,
            result,
        )

        print()
        print("=" * 80)

        print(
            "Stage-6 verdict: "
            f"{result['verdict']}"
        )

        print("=" * 80)

        print(
            f"Fault ID        : "
            f"{fault_id}"
        )

        print(
            f"Round           : "
            f"{round_index}"
        )

        print(
            "Aliases         : "
            + ", ".join(
                item["alias"]
                for item
                in signals
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
            faulty_event
            is not None
        ):

            print(
                "Detection cycle : "
                f"{faulty_event['cycle']}"
            )

            print(
                "Detection time  : "
                f"{faulty_event['time']}"
            )

        print(
            f"Result JSON     : "
            f"{simulation_json}"
        )

        return (
            0
            if result[
                "verdict"
            ]
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
