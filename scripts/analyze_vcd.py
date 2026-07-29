#!/usr/bin/env python3
"""Extract reproducible activity features from a VCD file.

The script keeps the original ``scripts/analyze_vcd.py`` filename and remains
compatible with the old basic invocation::

    python3 scripts/analyze_vcd.py path/to/riscy_tb.vcd

For experiment runs, save both machine-readable JSON and a compact text report::

    python3 scripts/analyze_vcd.py path/to/riscy_tb.vcd \
        --json-output path/to/vcd_features.json \
        --text-output path/to/vcd_features.txt

No external Python packages are required.  ``--delete-vcd`` removes the input
VCD only after parsing and all requested output writes complete successfully.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, TextIO


SCHEMA_VERSION = "1.0"

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
    r"data_wdata_o|"
    r"data_rdata_i|"
    r"data_we_o|"
    r"pc_if|"
    r"pc_id|"
    r"pc_ex|"
    r"pc_wb"
    r")(\s|\[|$)"
)


@dataclass
class SignalInfo:
    code: str
    name: str
    width: int
    observations: int = 0
    changes: int = 0
    first_time: int | None = None
    first_value: str | None = None
    last_time: int | None = None
    last_value: str | None = None
    first_change_time: int | None = None
    last_change_time: int | None = None
    unknown_observations: int = 0
    first_unknown_time: int | None = None
    last_unknown_time: int | None = None
    scalar_0_to_1: int = 0
    scalar_1_to_0: int = 0

    def observe(self, time: int, value: str) -> None:
        value = normalize_value(value)
        self.observations += 1

        if contains_unknown(value):
            self.unknown_observations += 1
            if self.first_unknown_time is None:
                self.first_unknown_time = time
            self.last_unknown_time = time

        if self.first_time is None:
            self.first_time = time
            self.first_value = value
            self.last_time = time
            self.last_value = value
            return

        previous = self.last_value
        if value != previous:
            self.changes += 1
            if self.first_change_time is None:
                self.first_change_time = time
            self.last_change_time = time

            if self.width == 1:
                if previous == "0" and value == "1":
                    self.scalar_0_to_1 += 1
                elif previous == "1" and value == "0":
                    self.scalar_1_to_0 += 1

        self.last_time = time
        self.last_value = value

    def payload(self, end_time: int) -> dict[str, Any]:
        data = asdict(self)
        data["active_span"] = (
            None
            if self.first_time is None or self.last_time is None
            else self.last_time - self.first_time
        )
        data["change_density_per_1m_ticks"] = (
            0.0
            if end_time <= 0
            else self.changes * 1_000_000.0 / end_time
        )
        data["has_unknown"] = self.unknown_observations > 0
        return data


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract selected CV32E40P signal activity from a VCD file."
    )
    parser.add_argument("vcd", type=Path, help="input VCD file")
    parser.add_argument(
        "--name-regex",
        default=DEFAULT_SIGNAL_PATTERN.pattern,
        help="regular expression applied to complete hierarchical signal names",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="write the complete feature payload as JSON",
    )
    parser.add_argument(
        "--text-output",
        type=Path,
        help="write the human-readable activity table",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=0,
        help=(
            "show only the N most-active signals in the text report; "
            "0 reports all selected signals"
        ),
    )
    parser.add_argument(
        "--delete-vcd",
        action="store_true",
        help=(
            "delete the input VCD only after successful parsing and output writes; "
            "requires --json-output or --text-output"
        ),
    )
    return parser.parse_args()


def normalize_value(value: str) -> str:
    return value.strip().lower()


def display_value(value: str | None, limit: int = 80) -> str:
    if value is None:
        return "-"
    if len(value) <= limit:
        return value
    half = max(8, (limit - 3) // 2)
    return value[:half] + "..." + value[-half:]


def contains_unknown(value: str) -> bool:
    return "x" in value or "z" in value


def extract_leaf_name(full_name: str) -> str:
    leaf = full_name.rsplit(".", maxsplit=1)[-1]
    return re.sub(r"\s*\[[^\]]+\]\s*$", "", leaf)


def parse_timescale_line(line: str) -> str | None:
    match = re.search(r"\$timescale\s+(.*?)\s+\$end", line)
    if not match:
        return None
    return " ".join(match.group(1).split())


def parse_vcd(
    path: Path,
    wanted: re.Pattern[str],
) -> tuple[list[SignalInfo], dict[str, Any]]:
    scopes: list[str] = []
    signals_by_code: dict[str, list[SignalInfo]] = {}
    selected_signals: list[SignalInfo] = []

    current_time = 0
    end_time = 0
    header_complete = False
    timescale: str | None = None
    timescale_collecting = False
    timescale_parts: list[str] = []
    declared_variables = 0

    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for raw_line in stream:
            line = raw_line.strip()
            if not line:
                continue

            if not header_complete:
                if timescale_collecting:
                    if "$end" in line:
                        before_end = line.split("$end", maxsplit=1)[0]
                        timescale_parts.append(before_end)
                        timescale = " ".join(" ".join(timescale_parts).split())
                        timescale_collecting = False
                        timescale_parts.clear()
                    else:
                        timescale_parts.append(line)
                    continue

                if line.startswith("$timescale"):
                    parsed = parse_timescale_line(line)
                    if parsed is not None:
                        timescale = parsed
                    else:
                        remainder = line[len("$timescale"):].strip()
                        if remainder:
                            timescale_parts.append(remainder)
                        timescale_collecting = True
                    continue

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

                    declared_variables += 1
                    code = fields[3]
                    reference = " ".join(fields[4:-1])
                    full_name = ".".join(scopes + [reference])
                    if not wanted.search(full_name):
                        continue

                    signal = SignalInfo(code=code, name=full_name, width=width)
                    signals_by_code.setdefault(code, []).append(signal)
                    selected_signals.append(signal)
                    continue

                if line.startswith("$enddefinitions"):
                    header_complete = True
                continue

            if line.startswith("#"):
                try:
                    current_time = int(line[1:])
                    end_time = max(end_time, current_time)
                except ValueError:
                    pass
                continue

            code: str | None = None
            value: str | None = None
            first = line[0]

            if first in "01xXzZ":
                value = first
                code = line[1:].strip()
            elif first in "bBrR":
                fields = line.split(maxsplit=1)
                if len(fields) == 2:
                    value = fields[0][1:]
                    code = fields[1].strip()

            if code is None or value is None:
                continue

            for signal in signals_by_code.get(code, []):
                signal.observe(current_time, value)

    metadata = {
        "timescale": timescale,
        "end_time": end_time,
        "declared_variables": declared_variables,
        "selected_variables": len(selected_signals),
        "selected_value_codes": len(signals_by_code),
    }
    return selected_signals, metadata


def build_payload(
    vcd_path: Path,
    name_regex: str,
    signals: list[SignalInfo],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    end_time = int(metadata["end_time"])
    by_leaf: dict[str, dict[str, int]] = {}

    for signal in signals:
        leaf = extract_leaf_name(signal.name)
        item = by_leaf.setdefault(
            leaf,
            {
                "signal_count": 0,
                "observations": 0,
                "changes": 0,
                "unknown_observations": 0,
            },
        )
        item["signal_count"] += 1
        item["observations"] += signal.observations
        item["changes"] += signal.changes
        item["unknown_observations"] += signal.unknown_observations

    signals_with_activity = sum(signal.changes > 0 for signal in signals)
    signals_with_unknown = sum(signal.unknown_observations > 0 for signal in signals)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": utc_now(),
        "source": {
            "vcd": str(vcd_path.resolve()),
            "size_bytes": vcd_path.stat().st_size,
            "sha256": sha256_file(vcd_path),
            "name_regex": name_regex,
        },
        "vcd": metadata,
        "summary": {
            "selected_signals": len(signals),
            "signals_with_activity": signals_with_activity,
            "signals_without_activity": len(signals) - signals_with_activity,
            "signals_with_unknown": signals_with_unknown,
            "total_observations": sum(signal.observations for signal in signals),
            "total_changes": sum(signal.changes for signal in signals),
            "total_unknown_observations": sum(
                signal.unknown_observations for signal in signals
            ),
        },
        "by_leaf_name": dict(sorted(by_leaf.items())),
        "signals": [
            signal.payload(end_time)
            for signal in sorted(signals, key=lambda item: item.name)
        ],
    }


def select_text_signals(
    signals: Iterable[SignalInfo],
    top: int,
) -> list[SignalInfo]:
    items = list(signals)
    if top <= 0 or top >= len(items):
        return sorted(items, key=lambda item: item.name)
    return sorted(
        items,
        key=lambda item: (-item.changes, item.name),
    )[:top]


def render_text(
    vcd_path: Path,
    payload: dict[str, Any],
    signals: list[SignalInfo],
    top: int,
) -> str:
    summary = payload["summary"]
    vcd = payload["vcd"]
    shown = select_text_signals(signals, top)

    lines = [
        f"VCD: {vcd_path}",
        f"SHA-256: {payload['source']['sha256']}",
        f"Size bytes: {payload['source']['size_bytes']}",
        f"Timescale: {vcd['timescale'] or '-'}",
        f"End time: {vcd['end_time']}",
        f"Selected signals: {summary['selected_signals']}",
        f"Signals with activity: {summary['signals_with_activity']}",
        f"Signals with unknown values: {summary['signals_with_unknown']}",
        f"Total changes: {summary['total_changes']}",
        "",
        (
            f"{'changes':>9}  {'obs':>8}  {'width':>5}  "
            f"{'first_time':>12}  {'last_time':>12}  "
            f"{'unknown':>8}  {'first':>18}  {'last':>18}  name"
        ),
        "-" * 170,
    ]

    for signal in shown:
        lines.append(
            f"{signal.changes:9d}  "
            f"{signal.observations:8d}  "
            f"{signal.width:5d}  "
            f"{str(signal.first_time) if signal.first_time is not None else '-':>12}  "
            f"{str(signal.last_time) if signal.last_time is not None else '-':>12}  "
            f"{signal.unknown_observations:8d}  "
            f"{display_value(signal.first_value):>18}  "
            f"{display_value(signal.last_value):>18}  "
            f"{signal.name}"
        )

    if top > 0 and len(shown) < len(signals):
        lines.extend(
            [
                "",
                f"Text report limited to {len(shown)} of {len(signals)} signals.",
                "The JSON output still contains every selected signal.",
            ]
        )

    return "\n".join(lines) + "\n"


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()
    vcd_path = args.vcd.resolve()

    if not vcd_path.is_file():
        raise SystemExit(f"ERROR: VCD file not found: {vcd_path}")
    if args.top < 0:
        raise SystemExit("ERROR: --top must be zero or a positive integer")
    if args.delete_vcd and args.json_output is None and args.text_output is None:
        raise SystemExit(
            "ERROR: --delete-vcd requires --json-output or --text-output"
        )

    try:
        wanted = re.compile(args.name_regex)
    except re.error as exc:
        raise SystemExit(f"ERROR: invalid --name-regex: {exc}") from exc

    signals, metadata = parse_vcd(vcd_path, wanted)
    if not signals:
        raise SystemExit(
            "ERROR: no signals matched --name-regex; VCD was not deleted"
        )

    payload = build_payload(
        vcd_path=vcd_path,
        name_regex=args.name_regex,
        signals=signals,
        metadata=metadata,
    )
    text_report = render_text(vcd_path, payload, signals, args.top)

    if args.json_output is not None:
        write_json(args.json_output.resolve(), payload)
        print(f"Wrote JSON: {args.json_output.resolve()}", file=sys.stderr)

    if args.text_output is not None:
        write_text(args.text_output.resolve(), text_report)
        print(f"Wrote text: {args.text_output.resolve()}", file=sys.stderr)
    else:
        sys.stdout.write(text_report)

    if args.delete_vcd:
        vcd_path.unlink()
        print(f"Deleted VCD after successful extraction: {vcd_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
