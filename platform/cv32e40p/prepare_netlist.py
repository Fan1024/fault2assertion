#!/usr/bin/env python3

"""
Prepare a CV32E40P mapped netlist for zero-delay gate-level simulation.

The source Genus netlist leaves five inactive COREV_CLUSTER inputs
unconnected on the COREV_CLUSTER=0 sleep-unit specialization. In Verilog,
unconnected input ports resolve to Z and may propagate X into the clock
gating logic.

The original mapped netlist is never modified. A run-local simulation copy
is generated with those inactive inputs explicitly tied to 1'b0.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


INACTIVE_PORTS = (
    "apu_busy_i",
    "pulp_clock_en_i",
    "p_elw_start_i",
    "p_elw_finish_i",
    "debug_p_elw_no_sleep_i",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.source.is_file():
        raise FileNotFoundError(f"Source netlist not found: {args.source}")

    text = args.source.read_text(errors="strict")

    # Locate the COREV_CLUSTER=0 sleep-unit instance. Insert the inactive
    # inputs between lsu_busy_i and wake_from_sleep_i.
    pattern = re.compile(
        r"(?s)"
        r"("
        r"cv32e40p_sleep_unit_COREV_CLUSTER0"
        r"\s+sleep_unit_i\s*\("
        r".*?"
        r"\.lsu_busy_i\s*\(\s*lsu_busy\s*\)\s*,"
        r")"
        r"(\s*\.wake_from_sleep_i)"
    )

    matches = list(pattern.finditer(text))

    if len(matches) != 1:
        raise RuntimeError(
            "Expected exactly one COREV_CLUSTER0 sleep_unit_i instance, "
            f"but found {len(matches)}."
        )

    tieoffs = """
       .apu_busy_i (1'b0),
       .pulp_clock_en_i (1'b0),
       .p_elw_start_i (1'b0),
       .p_elw_finish_i (1'b0),
       .debug_p_elw_no_sleep_i (1'b0),"""

    patched, count = pattern.subn(
        lambda match: match.group(1) + tieoffs + match.group(2),
        text,
        count=1,
    )

    if count != 1:
        raise RuntimeError("Failed to patch sleep_unit_i.")

    # Validate that all five named connections appear in the patched instance.
    instance_pattern = re.compile(
        r"(?s)"
        r"cv32e40p_sleep_unit_COREV_CLUSTER0"
        r"\s+sleep_unit_i\s*\("
        r"(.*?)"
        r"\);"
    )

    instance_match = instance_pattern.search(patched)

    if instance_match is None:
        raise RuntimeError("Patched sleep_unit_i instance could not be found.")

    instance_text = instance_match.group(1)

    missing = [
        port
        for port in INACTIVE_PORTS
        if not re.search(
            rf"\.{re.escape(port)}\s*\(\s*1'b0\s*\)",
            instance_text,
        )
    ]

    if missing:
        raise RuntimeError(
            "Patched instance is missing tie-offs: " + ", ".join(missing)
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(patched)

    print(f"Source netlist : {args.source}")
    print(f"Output netlist : {args.output}")
    print("Added inactive COREV_CLUSTER=0 tie-offs:")

    for port in INACTIVE_PORTS:
        print(f"  {port}=1'b0")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
