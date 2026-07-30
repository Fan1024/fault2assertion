#!/usr/bin/env python3
"""Compare golden and faulty local-probe VCDs for one branch fault.

The script reports three separate questions:

1. Activation: did the golden source net ever take the value opposite to the
   selected stuck-at value?
2. Injection: did the selected receiver pin stay at the stuck value in the
   faulty run?
3. Local propagation: did any receiver-cell output diverge from the golden run?

Only signals whose leaf name begins with ``f2a_`` are loaded, so memory use stays
small even when the ordinary testbench also dumps a few global signals.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


@dataclass
class Trace:
    name: str
    width: int
    events: list[tuple[int, str]] = field(default_factory=list)

    def observe(self, time: int, value: str) -> None:
        normalized = value.strip().lower()
        if self.events and self.events[-1][1] == normalized:
            return
        self.events.append((time, normalized))

    def known_values(self) -> set[str]:
        return {
            value
            for _, value in self.events
            if value and not any(char in value for char in "xz")
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare golden and faulty local-probe VCD traces."
    )
    parser.add_argument("--golden-vcd", type=Path, required=True)
    parser.add_argument("--fault-vcd", type=Path, required=True)
    parser.add_argument("--fault-json", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--text-output", type=Path)
    parser.add_argument(
        "--delete-vcds",
        action="store_true",
        help="delete both VCDs only after comparison outputs are written",
    )
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


def leaf_name(reference: str) -> str:
    leaf = reference.rsplit(".", maxsplit=1)[-1]
    return re.sub(r"\s*\[[^\]]+\]\s*$", "", leaf)


def parse_probe_vcd(path: Path) -> dict[str, Trace]:
    scopes: list[str] = []
    traces_by_code: dict[str, list[Trace]] = {}
    traces_by_leaf: dict[str, Trace] = {}
    current_time = 0
    header_complete = False

    with path.open("r", encoding="utf-8", errors="replace") as stream:
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
                    leaf = leaf_name(full_name)
                    if not leaf.startswith("f2a_"):
                        continue
                    if leaf in traces_by_leaf:
                        raise SystemExit(
                            "ERROR: duplicate local-probe leaf name in VCD. "
                            "The target module may have multiple elaborated instances: "
                            f"{leaf}\n  first: {traces_by_leaf[leaf].name}\n  next:  {full_name}"
                        )
                    trace = Trace(name=full_name, width=width)
                    traces_by_leaf[leaf] = trace
                    traces_by_code.setdefault(code, []).append(trace)
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
                value = first
                code = line[1:].strip()
            elif first in "bBrR":
                fields = line.split(maxsplit=1)
                if len(fields) == 2:
                    value = fields[0][1:]
                    code = fields[1].strip()

            if code is None or value is None:
                continue
            for trace in traces_by_code.get(code, []):
                trace.observe(current_time, value)

    if not traces_by_leaf:
        raise SystemExit(f"ERROR: no f2a_* signals found in VCD: {path}")
    return traces_by_leaf


def earliest_divergence(
    golden: Trace,
    fault: Trace,
) -> tuple[int | None, str | None, str | None]:
    times = sorted(
        {time for time, _ in golden.events}
        | {time for time, _ in fault.events}
    )
    golden_index = 0
    fault_index = 0
    golden_value: str | None = None
    fault_value: str | None = None

    for time in times:
        while (
            golden_index < len(golden.events)
            and golden.events[golden_index][0] == time
        ):
            golden_value = golden.events[golden_index][1]
            golden_index += 1
        while (
            fault_index < len(fault.events)
            and fault.events[fault_index][0] == time
        ):
            fault_value = fault.events[fault_index][1]
            fault_index += 1

        if (
            golden_value is not None
            and fault_value is not None
            and golden_value != fault_value
        ):
            return time, golden_value, fault_value

    return None, None, None


def trace_summary(trace: Trace) -> dict[str, Any]:
    return {
        "name": trace.name,
        "width": trace.width,
        "event_count": len(trace.events),
        "first_time": trace.events[0][0] if trace.events else None,
        "first_value": trace.events[0][1] if trace.events else None,
        "last_time": trace.events[-1][0] if trace.events else None,
        "last_value": trace.events[-1][1] if trace.events else None,
        "known_values": sorted(trace.known_values()),
    }


def build_report(
    fault_metadata: dict[str, Any],
    golden: dict[str, Trace],
    fault: dict[str, Trace],
    golden_vcd: Path,
    fault_vcd: Path,
) -> dict[str, Any]:
    try:
        fault_id = str(fault_metadata["fault_id"])
        stuck_at = int(fault_metadata["stuck_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"ERROR: malformed fault.json: {exc}") from exc
    if stuck_at not in {0, 1}:
        raise SystemExit(f"ERROR: invalid stuck_at value: {stuck_at}")

    required = {"f2a_source_original", "f2a_branch_observed"}
    missing_golden = sorted(required - golden.keys())
    missing_fault = sorted(required - fault.keys())
    if missing_golden or missing_fault:
        raise SystemExit(
            "ERROR: required probe signals are missing. "
            f"golden={missing_golden}, fault={missing_fault}"
        )

    opposite = str(1 - stuck_at)
    source_values = golden["f2a_source_original"].known_values()
    activated = opposite in source_values

    branch_values = fault["f2a_branch_observed"].known_values()
    injection_effective = bool(branch_values) and branch_values <= {str(stuck_at)}

    common = sorted(golden.keys() & fault.keys())
    comparisons: list[dict[str, Any]] = []
    for key in common:
        time, golden_value, fault_value = earliest_divergence(
            golden[key], fault[key]
        )
        comparisons.append(
            {
                "signal": key,
                "role": (
                    "receiver_output"
                    if key.startswith("f2a_output_")
                    else "receiver_input"
                    if key.startswith("f2a_input_")
                    else "source"
                    if key == "f2a_source_original"
                    else "selected_branch"
                    if key == "f2a_branch_observed"
                    else "other"
                ),
                "earliest_divergence_time": time,
                "golden_value": golden_value,
                "fault_value": fault_value,
                "golden": trace_summary(golden[key]),
                "fault": trace_summary(fault[key]),
            }
        )

    output_differences = [
        item
        for item in comparisons
        if item["role"] == "receiver_output"
        and item["earliest_divergence_time"] is not None
    ]
    branch_comparison = next(
        item for item in comparisons if item["signal"] == "f2a_branch_observed"
    )

    if not activated:
        classification = "NOT_ACTIVATED"
    elif not injection_effective:
        classification = "INJECTION_ERROR"
    elif branch_comparison["earliest_divergence_time"] is None:
        classification = "INJECTION_NOT_OBSERVED"
    elif output_differences:
        classification = "LOCALLY_PROPAGATED"
    else:
        classification = "LOCALLY_MASKED_AT_RECEIVER"

    earliest_output = min(
        (
            item["earliest_divergence_time"]
            for item in output_differences
            if item["earliest_divergence_time"] is not None
        ),
        default=None,
    )

    return {
        "schema_version": "1.0",
        "generated_at_utc": utc_now(),
        "fault_id": fault_id,
        "stuck_at": stuck_at,
        "golden_vcd": str(golden_vcd.resolve()),
        "fault_vcd": str(fault_vcd.resolve()),
        "classification": classification,
        "activation": {
            "activated": activated,
            "required_source_value": opposite,
            "observed_golden_source_values": sorted(source_values),
        },
        "injection": {
            "effective": injection_effective,
            "expected_fault_branch_value": str(stuck_at),
            "observed_fault_branch_values": sorted(branch_values),
            "earliest_branch_divergence_time": branch_comparison[
                "earliest_divergence_time"
            ],
        },
        "local_propagation": {
            "receiver_output_diverged": bool(output_differences),
            "earliest_receiver_output_divergence_time": earliest_output,
            "divergent_receiver_outputs": [
                item["signal"] for item in output_differences
            ],
        },
        "signals_only_in_golden": sorted(golden.keys() - fault.keys()),
        "signals_only_in_fault": sorted(fault.keys() - golden.keys()),
        "comparisons": comparisons,
    }


def render_text(report: dict[str, Any]) -> str:
    activation = report["activation"]
    injection = report["injection"]
    propagation = report["local_propagation"]
    lines = [
        f"Fault ID: {report['fault_id']}",
        f"Stuck-at: SA{report['stuck_at']}",
        f"Classification: {report['classification']}",
        "",
        "Activation",
        "----------",
        f"Activated: {activation['activated']}",
        f"Required golden source value: {activation['required_source_value']}",
        "Observed golden source values: "
        + ", ".join(activation["observed_golden_source_values"]),
        "",
        "Injection",
        "---------",
        f"Effective: {injection['effective']}",
        f"Expected fault branch value: {injection['expected_fault_branch_value']}",
        "Observed fault branch values: "
        + ", ".join(injection["observed_fault_branch_values"]),
        "Earliest branch divergence time: "
        + str(injection["earliest_branch_divergence_time"]),
        "",
        "Local propagation",
        "-----------------",
        "Receiver output diverged: "
        + str(propagation["receiver_output_diverged"]),
        "Earliest receiver output divergence time: "
        + str(propagation["earliest_receiver_output_divergence_time"]),
        "Divergent receiver outputs: "
        + ", ".join(propagation["divergent_receiver_outputs"]),
        "",
        "Per-signal earliest divergence",
        "------------------------------",
    ]
    for item in report["comparisons"]:
        lines.append(
            f"{item['signal']}: {item['earliest_divergence_time']} "
            f"({item['golden_value']} -> {item['fault_value']})"
        )
    return "\n".join(lines) + "\n"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> int:
    args = parse_args()
    golden_vcd = args.golden_vcd.resolve()
    fault_vcd = args.fault_vcd.resolve()
    fault_json = args.fault_json.resolve()

    for path, label in (
        (golden_vcd, "golden VCD"),
        (fault_vcd, "fault VCD"),
        (fault_json, "fault JSON"),
    ):
        if not path.is_file():
            raise SystemExit(f"ERROR: {label} not found: {path}")

    metadata = read_json(fault_json)
    golden = parse_probe_vcd(golden_vcd)
    fault = parse_probe_vcd(fault_vcd)
    report = build_report(metadata, golden, fault, golden_vcd, fault_vcd)
    text = render_text(report)

    if args.json_output is not None:
        write_json(args.json_output.resolve(), report)
        print(f"Wrote JSON: {args.json_output.resolve()}")
    if args.text_output is not None:
        write_text(args.text_output.resolve(), text)
        print(f"Wrote text: {args.text_output.resolve()}")
    else:
        print(text, end="")

    if args.delete_vcds:
        golden_vcd.unlink()
        if fault_vcd != golden_vcd:
            fault_vcd.unlink()
        print("Deleted local-probe VCDs after successful comparison.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
