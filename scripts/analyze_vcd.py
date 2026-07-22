#!/usr/bin/env python3

"""
Summarize activity of important CV32E40P signals from a VCD file.

The script reports:
  - complete hierarchical signal name
  - width
  - number of value changes
  - first observed value and time
  - final observed value and time

No external Python packages are required.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path


DEFAULT_SIGNAL_PATTERN = re.compile(
    r"(^|\.)("
    r"clk_i|"
    r"rst_ni|"
    r"clk|"
    r"clock_en|"
    r"core_busy_q|"
    r"core_sleep_o|"
    r"fetch_enable_i|"
    r"fetch_enable|"
    r"instr_req_o|"
    r"instr_gnt_i|"
    r"instr_rvalid_i|"
    r"instr_addr_o|"
    r"instr_rdata_i|"
    r"data_req_o|"
    r"data_gnt_i|"
    r"data_rvalid_i|"
    r"data_addr_o|"
    r"pc_if|"
    r"pc_id"
    r")(\s|\[|$)"
)


@dataclass
class SignalInfo:
    code: str
    name: str
    width: int
    changes: int = 0
    first_time: int | None = None
    first_value: str | None = None
    last_time: int | None = None
    last_value: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("vcd", type=Path)
    parser.add_argument(
        "--name-regex",
        default=DEFAULT_SIGNAL_PATTERN.pattern,
        help="Regular expression applied to full hierarchical signal names",
    )
    return parser.parse_args()


def normalize_value(value: str) -> str:
    value = value.strip().lower()

    if len(value) > 80:
        return value[:36] + "..." + value[-36:]

    return value


def main() -> int:
    args = parse_args()

    if not args.vcd.is_file():
        raise FileNotFoundError(f"VCD file not found: {args.vcd}")

    wanted = re.compile(args.name_regex)

    scopes: list[str] = []
    all_signals: dict[str, SignalInfo] = {}
    selected: dict[str, SignalInfo] = {}

    current_time = 0
    header_complete = False

    with args.vcd.open(errors="replace") as stream:
        for raw_line in stream:
            line = raw_line.strip()

            if not line:
                continue

            if not header_complete:
                if line.startswith("$scope"):
                    fields = line.split()

                    if len(fields) >= 3:
                        scopes.append(fields[2])

                    continue

                if line.startswith("$upscope"):
                    if scopes:
                        scopes.pop()

                    continue

                if line.startswith("$var"):
                    fields = line.split()

                    if len(fields) < 6:
                        continue

                    try:
                        width = int(fields[2])
                    except ValueError:
                        continue

                    code = fields[3]
                    reference = " ".join(fields[4:-1])
                    full_name = ".".join(scopes + [reference])

                    signal = SignalInfo(
                        code=code,
                        name=full_name,
                        width=width,
                    )

                    all_signals[code] = signal

                    if wanted.search(full_name):
                        selected[code] = signal

                    continue

                if line.startswith("$enddefinitions"):
                    header_complete = True

                continue

            if line.startswith("#"):
                try:
                    current_time = int(line[1:])
                except ValueError:
                    pass

                continue

            code: str | None = None
            value: str | None = None

            first = line[0]

            if first in "01xXzZ":
                value = first.lower()
                code = line[1:].strip()

            elif first in "bBrR":
                fields = line.split(maxsplit=1)

                if len(fields) == 2:
                    value = fields[0][1:].lower()
                    code = fields[1].strip()

            if code is None or value is None:
                continue

            signal = selected.get(code)

            if signal is None:
                continue

            value = normalize_value(value)

            if signal.first_time is None:
                signal.first_time = current_time
                signal.first_value = value
                signal.last_time = current_time
                signal.last_value = value
                continue

            if value != signal.last_value:
                signal.changes += 1

            signal.last_time = current_time
            signal.last_value = value

    print(f"VCD: {args.vcd}")
    print(f"Selected signals: {len(selected)}")
    print()

    print(
        f"{'changes':>9}  "
        f"{'width':>5}  "
        f"{'first_time':>12}  "
        f"{'first':>18}  "
        f"{'last_time':>12}  "
        f"{'last':>18}  "
        f"name"
    )

    print("-" * 150)

    for signal in sorted(selected.values(), key=lambda item: item.name):
        first_time = (
            str(signal.first_time)
            if signal.first_time is not None
            else "-"
        )

        last_time = (
            str(signal.last_time)
            if signal.last_time is not None
            else "-"
        )

        first_value = signal.first_value or "-"
        last_value = signal.last_value or "-"

        print(
            f"{signal.changes:9d}  "
            f"{signal.width:5d}  "
            f"{first_time:>12}  "
            f"{first_value:>18}  "
            f"{last_time:>12}  "
            f"{last_value:>18}  "
            f"{signal.name}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
