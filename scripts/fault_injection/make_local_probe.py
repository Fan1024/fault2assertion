#!/usr/bin/env python3
"""Generate a bind-based local VCD probe for one branch fault site.

The generated SystemVerilog does not modify functional design behavior.

Important implementation detail
-------------------------------
The receiver/source expressions are connected to probe input ports named
``probe_*_i``.  The monitor then mirrors them into distinct internal procedural
variables named ``f2a_*``.

Using distinct internal variables is intentional.  Simulators may collapse
input-port aliases onto the connected design nets, causing the alias names to
disappear from the VCD.  Procedural ``logic f2a_*`` mirrors remain separate VCD
objects and can therefore be found reliably by compare_local_probe.py.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import branch_fault


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a local bind probe from fault.json and a run-local netlist."
        )
    )
    parser.add_argument("--fault-json", type=Path, required=True)
    parser.add_argument("--netlist", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"ERROR: file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ERROR: invalid JSON in {path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise SystemExit(f"ERROR: expected JSON object: {path}")

    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )


def sv_identifier(name: str, label: str) -> str:
    """Return a legal reference for a normal or escaped Verilog identifier."""

    value = name.strip()

    if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", value):
        return value

    if value.startswith("\\") and not re.search(r"\s", value):
        # Escaped identifiers require trailing whitespace in Verilog source.
        return value + " "

    raise SystemExit(
        f"ERROR: unsupported {label} identifier: {name!r}"
    )


def sv_expression(expression: str) -> str:
    """Preserve the terminator required by escaped identifiers."""

    value = expression.strip()

    if value.startswith("\\") and not re.search(r"\s", value):
        return value + " "

    return value


def sanitize_pin(pin: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_$]", "_", pin)

    if not cleaned or not re.match(r"[A-Za-z_$]", cleaned):
        cleaned = "p_" + cleaned

    return cleaned


def unique_name(base: str, used: set[str]) -> str:
    name = base
    suffix = 2

    while name in used:
        name = f"{base}_{suffix}"
        suffix += 1

    used.add(name)
    return name


def build_probe(
    metadata: dict[str, Any],
    netlist_path: Path,
) -> tuple[str, dict[str, Any]]:
    try:
        fault_id = str(metadata["fault_id"])
        stuck_at = int(metadata["stuck_at"])
        site = metadata["site"]
        module_name = str(site["module"])
        source_net = str(site["source_net"])
        sink_instance = str(site["sink_instance"])
        sink_cell_type = str(site["sink_cell_type"])
        sink_pin = str(site["sink_pin"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(
            f"ERROR: incomplete fault metadata: {exc}"
        ) from exc

    if stuck_at not in {0, 1}:
        raise SystemExit(
            f"ERROR: invalid stuck_at value: {stuck_at}"
        )

    text = netlist_path.read_text(
        encoding="utf-8",
        errors="strict",
    )

    _, cells = branch_fault.parse_standard_cells(text)

    matches = [
        cell
        for cell in cells
        if cell.module == module_name
        and cell.instance == sink_instance
    ]

    if len(matches) != 1:
        raise SystemExit(
            "ERROR: expected exactly one receiver instance in run-local "
            f"netlist; found {len(matches)} for "
            f"{module_name}/{sink_instance}"
        )

    cell = matches[0]

    if cell.cell_type != sink_cell_type:
        raise SystemExit(
            "ERROR: receiver cell type changed: "
            f"fault.json={sink_cell_type}, "
            f"netlist={cell.cell_type}"
        )

    by_pin = {
        connection.pin: connection
        for connection in cell.connections
    }

    if sink_pin not in by_pin:
        raise SystemExit(
            f"ERROR: selected sink pin {sink_pin} "
            f"not found on {sink_instance}"
        )

    output_pin_set = branch_fault.output_pins(cell.cell_type)

    # Each entry has:
    #   signal_name: stable VCD leaf name expected by compare_local_probe.py
    #   port_name:   non-f2a input port connected by bind
    #   role:        semantic role
    #   cell_pin:    receiver pin, when applicable
    #   expression:  expression from the simulation netlist
    signals: list[dict[str, str]] = [
        {
            "signal_name": "f2a_source_original",
            "port_name": "probe_source_original_i",
            "role": "upstream_source",
            "cell_pin": "",
            "expression": source_net,
        },
        {
            "signal_name": "f2a_branch_observed",
            "port_name": "probe_branch_observed_i",
            "role": "selected_sink_pin_after_injection",
            "cell_pin": sink_pin,
            "expression": by_pin[sink_pin].expression.strip(),
        },
    ]

    used_signal_names = {
        item["signal_name"]
        for item in signals
    }
    used_port_names = {
        item["port_name"]
        for item in signals
    }

    for connection in cell.connections:
        if connection.pin == sink_pin:
            continue

        expression = connection.expression.strip()

        if not expression:
            continue

        role_prefix = (
            "output"
            if connection.pin in output_pin_set
            else "input"
        )

        pin_suffix = sanitize_pin(connection.pin)

        signal_name = unique_name(
            f"f2a_{role_prefix}_{pin_suffix}",
            used_signal_names,
        )

        port_name = unique_name(
            f"probe_{role_prefix}_{pin_suffix}_i",
            used_port_names,
        )

        signals.append(
            {
                "signal_name": signal_name,
                "port_name": port_name,
                "role": f"receiver_{role_prefix}",
                "cell_pin": connection.pin,
                "expression": expression,
            }
        )

    if not any(
        item["role"] == "receiver_output"
        for item in signals
    ):
        raise SystemExit(
            "ERROR: no output pin was found for receiver cell "
            f"{cell.cell_type}"
        )

    module_ref = sv_identifier(
        module_name,
        "module",
    )

    port_declarations = ",\n".join(
        f"    input wire {item['port_name']}"
        for item in signals
    )

    # Internal procedural variables are deliberately distinct from input ports.
    # This prevents Xcelium from collapsing the f2a_* VCD names into the
    # connected original net names.
    internal_declarations = "\n".join(
        (
            "  (* keep = \"true\" *) "
            f"logic {item['signal_name']};"
        )
        for item in signals
    )

    mirror_assignments = "\n".join(
        (
            f"    {item['signal_name']} = "
            f"{item['port_name']};"
        )
        for item in signals
    )

    dump_calls = "\n".join(
        f"      $dumpvars(0, {item['signal_name']});"
        for item in signals
    )

    bind_connections = ",\n".join(
        (
            f"    .{item['port_name']}"
            f"({sv_expression(item['expression'])})"
        )
        for item in signals
    )

    signal_name_list = ", ".join(
        item["signal_name"]
        for item in signals
    )

    sv_text = f"""`timescale 1ns/1ps

// Auto-generated local probe for {fault_id}.
//
// This monitor is observation-only. It does not drive design signals.
//
// The probe input ports use probe_* names. Distinct procedural logic variables
// use f2a_* names so Xcelium cannot collapse their VCD names into the original
// connected net names.
module f2a_local_probe (
{port_declarations}
);

{internal_declarations}

  // Procedural mirrors force distinct, named VCD objects.
  always @* begin
{mirror_assignments}
  end

  initial begin
    if ($test$plusargs("local_probe")) begin
      $display("[F2A_PROBE] active fault={fault_id} module={module_name} sink={sink_instance}/{sink_pin}");

      // tb_top normally starts riscy_tb.vcd at time zero. Waiting one
      // precision tick ensures that dumpfile setup has completed.
      #1ps;

      // These calls are kept as an explicit fallback. The ordinary recursive
      // tb_top dump should already include the internal f2a_* variables.
{dump_calls}

      $display(
        "[F2A_PROBE] requested VCD variables: {signal_name_list}"
      );
    end
  end

endmodule

bind {module_ref} f2a_local_probe f2a_local_probe_i (
{bind_connections}
);
"""

    manifest_signals = [
        {
            "name": item["signal_name"],
            "probe_port": item["port_name"],
            "role": item["role"],
            "cell_pin": item["cell_pin"],
            "expression": item["expression"],
        }
        for item in signals
    ]

    manifest = {
        "schema_version": "2.0",
        "generated_at_utc": utc_now(),
        "fault_id": fault_id,
        "stuck_at": stuck_at,
        "netlist": str(netlist_path.resolve()),
        "target": {
            "module": module_name,
            "sink_instance": sink_instance,
            "sink_cell_type": sink_cell_type,
            "sink_pin": sink_pin,
            "source_net": source_net,
        },
        "signals": manifest_signals,
        "implementation": {
            "bind_target": module_name,
            "bound_monitor_module": "f2a_local_probe",
            "bound_instance_name": "f2a_local_probe_i",
            "vcd_name_policy": (
                "f2a_* are distinct internal procedural logic mirrors; "
                "probe_*_i are bind input ports"
            ),
        },
        "interpretation": {
            "activation": (
                "For SA0, f2a_source_original must reach 1; "
                "for SA1, it must reach 0."
            ),
            "injection": (
                "In the fault run, f2a_branch_observed must remain "
                "at the stuck value."
            ),
            "propagation": (
                "Compare f2a_output_* traces between golden and fault runs."
            ),
        },
    }

    return sv_text, manifest


def main() -> int:
    args = parse_args()

    fault_json = args.fault_json.resolve()
    netlist = args.netlist.resolve()
    output = args.output.resolve()

    if not fault_json.is_file():
        raise SystemExit(
            f"ERROR: fault JSON not found: {fault_json}"
        )

    if not netlist.is_file():
        raise SystemExit(
            f"ERROR: netlist not found: {netlist}"
        )

    metadata = read_json(fault_json)

    sv_text, manifest = build_probe(
        metadata,
        netlist,
    )

    output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    output.write_text(
        sv_text,
        encoding="utf-8",
    )

    print(f"Wrote local probe: {output}")

    if args.manifest is not None:
        manifest_path = args.manifest.resolve()

        write_json(
            manifest_path,
            manifest,
        )

        print(
            f"Wrote probe manifest: {manifest_path}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
