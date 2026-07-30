#!/usr/bin/env python3
"""Generate and analyze compact workload activity monitors for fault sites.

Stage 3 consumes the Stage-2 static-safety catalog.  It generates one
observation-only SystemVerilog monitor, runs no simulator by itself, parses the
compact TSV emitted by that monitor, and assigns workload-specific eligible
stuck-at polarities.

The monitor intentionally does not call $dumpfile or $dumpvars.  It records
only compact per-site activity features:

* whether binary 0 was observed;
* whether binary 1 was observed;
* whether X/Z was observed after reset;
* total value-change count;
* binary 0<->1 toggle count;
* first observation, first change, and last change cycle/time.

Only Python's standard library is required.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

PROGRAM_VERSION = "1.0.1"
SCHEMA_VERSION = "1.0"
MONITOR_SCHEMA_VERSION = "1.0"
RAW_FORMAT_MARKER = "#F2A_ACTIVITY_V1"
DEFAULT_GROUP_WIDTH = 256

STAGE2_NAME = "stage_02_static_safety_filtering"
STAGE2_ELIGIBLE = "eligible_for_activity_profile"
STAGE3_NAME = "stage_03_golden_activity_filtering"

SITE_ID_RE = re.compile(r"^RS\d{6}$")
NORMAL_SIGNAL_RE = re.compile(
    r"^[A-Za-z_$][A-Za-z0-9_$]*(?:\s*\[[^\]]+\])*$"
)
ESCAPED_SIGNAL_RE = re.compile(r"^\\\S+(?:\s+\[[^\]]+\])*$")


@dataclass(frozen=True)
class MonitorSite:
    group_id: int
    local_index: int
    site_id: str
    site_key: str
    module: str
    source_net: str
    source_key: str
    source_kind: str
    state_site: bool
    logic_fanout: int
    fanout_bucket: str


@dataclass
class AggregateActivity:
    instance_count: int = 0
    seen_0: bool = False
    seen_1: bool = False
    unknown_seen: bool = False
    value_change_count: int = 0
    binary_toggle_count: int = 0
    first_observation_cycle: int | None = None
    first_change_cycle: int | None = None
    last_change_cycle: int | None = None
    first_observation_time: int | None = None
    first_change_time: int | None = None
    last_change_time: int | None = None
    last_values: set[str] | None = None

    def __post_init__(self) -> None:
        if self.last_values is None:
            self.last_values = set()

    @staticmethod
    def _min_optional(current: int | None, value: int) -> int | None:
        if value < 0:
            return current
        if current is None:
            return value
        return min(current, value)

    @staticmethod
    def _max_optional(current: int | None, value: int) -> int | None:
        if value < 0:
            return current
        if current is None:
            return value
        return max(current, value)

    def add_row(self, row: dict[str, Any]) -> None:
        self.instance_count += 1
        self.seen_0 = self.seen_0 or bool(row["seen_0"])
        self.seen_1 = self.seen_1 or bool(row["seen_1"])
        self.unknown_seen = self.unknown_seen or bool(row["unknown_seen"])
        self.value_change_count += int(row["value_change_count"])
        self.binary_toggle_count += int(row["binary_toggle_count"])

        self.first_observation_cycle = self._min_optional(
            self.first_observation_cycle,
            int(row["first_observation_cycle"]),
        )
        self.first_change_cycle = self._min_optional(
            self.first_change_cycle,
            int(row["first_change_cycle"]),
        )
        self.last_change_cycle = self._max_optional(
            self.last_change_cycle,
            int(row["last_change_cycle"]),
        )
        self.first_observation_time = self._min_optional(
            self.first_observation_time,
            int(row["first_observation_time"]),
        )
        self.first_change_time = self._min_optional(
            self.first_change_time,
            int(row["first_change_time"]),
        )
        self.last_change_time = self._max_optional(
            self.last_change_time,
            int(row["last_change_time"]),
        )

        last_value = str(row["last_value"]).lower()
        if last_value in {"0", "1", "x", "z"}:
            assert self.last_values is not None
            self.last_values.add(last_value)

    def payload(self) -> dict[str, Any]:
        assert self.last_values is not None
        if not self.last_values:
            last_value: str | None = None
        elif len(self.last_values) == 1:
            last_value = next(iter(self.last_values))
        else:
            last_value = "mixed"

        return {
            "instance_count": self.instance_count,
            "seen_0": self.seen_0,
            "seen_1": self.seen_1,
            "unknown_seen": self.unknown_seen,
            "value_change_count": self.value_change_count,
            "binary_toggle_count": self.binary_toggle_count,
            "first_observation_cycle": self.first_observation_cycle,
            "first_change_cycle": self.first_change_cycle,
            "last_change_cycle": self.last_change_cycle,
            "first_observation_time": self.first_observation_time,
            "first_change_time": self.first_change_time,
            "last_change_time": self.last_change_time,
            "last_value": last_value,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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


def _atomic_write(path: Path, data: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def write_text(path: Path, data: str, force: bool = False, mode: int = 0o644) -> None:
    if path.exists() and not force:
        raise SystemExit(
            f"ERROR: output already exists: {path}\n"
            "Use --force only after intentionally preserving or removing the old output."
        )
    _atomic_write(path, data, mode)


def write_json(
    path: Path,
    payload: dict[str, Any],
    force: bool = False,
    mode: int = 0o644,
) -> None:
    write_text(
        path,
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        force=force,
        mode=mode,
    )


def require_nonempty_file(path: Path, label: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise SystemExit(f"ERROR: {label} not found or empty: {path}")


def numeric_site_id(site_id: str) -> int:
    if not SITE_ID_RE.fullmatch(site_id):
        raise ValueError(f"invalid site ID: {site_id}")
    return int(site_id[2:])


def validate_stage2_payload(payload: dict[str, Any], path: Path) -> list[dict[str, Any]]:
    required = {
        "schema_version",
        "program_version",
        "stage",
        "design",
        "source",
        "stage2_summary",
        "static_filter_digest_sha256",
        "sites",
    }
    missing = sorted(required - payload.keys())
    if missing:
        raise SystemExit(
            f"ERROR: Stage-2 JSON is missing fields: {', '.join(missing)}"
        )

    if payload["stage"] != STAGE2_NAME:
        raise SystemExit(
            f"ERROR: expected stage={STAGE2_NAME!r}, got {payload['stage']!r}"
        )

    sites = payload["sites"]
    if not isinstance(sites, list):
        raise SystemExit("ERROR: Stage-2 sites must be a JSON list")

    expected_count = int(payload["stage2_summary"]["raw_site_count"])
    if len(sites) != expected_count:
        raise SystemExit(
            "ERROR: Stage-2 site count mismatch: "
            f"summary={expected_count}, actual={len(sites)}"
        )

    seen_ids: set[str] = set()
    seen_keys: set[str] = set()
    eligible_count = 0

    for site in sites:
        if not isinstance(site, dict):
            raise SystemExit("ERROR: every Stage-2 site must be an object")

        for field in (
            "site_id",
            "site_key",
            "module",
            "source_net",
            "source_key",
            "source_kind",
            "state_site",
            "logic_fanout",
            "fanout_bucket",
            "stage2_status",
        ):
            if field not in site:
                raise SystemExit(
                    f"ERROR: Stage-2 site is missing {field}: {site.get('site_id')}"
                )

        site_id = str(site["site_id"])
        site_key = str(site["site_key"])
        numeric_site_id(site_id)

        if site_id in seen_ids:
            raise SystemExit(f"ERROR: duplicate site ID: {site_id}")
        if site_key in seen_keys:
            raise SystemExit(f"ERROR: duplicate site key: {site_key}")
        seen_ids.add(site_id)
        seen_keys.add(site_key)

        if site["stage2_status"] == STAGE2_ELIGIBLE:
            eligible_count += 1
            if site.get("exclusion_reasons"):
                raise SystemExit(
                    f"ERROR: eligible Stage-2 site has exclusion reasons: {site_id}"
                )
            safety = site.get("static_safety", {})
            if not safety.get("clock_safe", False):
                raise SystemExit(f"ERROR: clock-unsafe site entered Stage 3: {site_id}")
            if not safety.get("reset_set_safe", False):
                raise SystemExit(f"ERROR: reset/set-unsafe site entered Stage 3: {site_id}")

    summary_eligible = int(
        payload["stage2_summary"]["eligible_for_activity_profile_count"]
    )
    if eligible_count != summary_eligible:
        raise SystemExit(
            "ERROR: Stage-2 eligible count mismatch: "
            f"summary={summary_eligible}, actual={eligible_count}"
        )

    digest = str(payload["static_filter_digest_sha256"])
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SystemExit("ERROR: invalid Stage-2 digest")

    return sites


def sv_module_identifier(name: str) -> str:
    if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", name):
        return name
    if name.startswith("\\") and not re.search(r"\s", name):
        return name + " "
    raise ValueError(f"unsupported module identifier: {name!r}")


def sv_scalar_expression(expression: str) -> str:
    value = expression.strip()
    if not value:
        raise ValueError("empty signal expression")

    if value.startswith("\\"):
        if not ESCAPED_SIGNAL_RE.fullmatch(value):
            raise ValueError(f"unsupported escaped signal expression: {expression!r}")
        parts = value.split(None, 1)
        escaped = parts[0]
        if len(parts) == 1:
            return escaped + " "
        suffix = parts[1].strip()
        return escaped + " " + suffix

    if not NORMAL_SIGNAL_RE.fullmatch(value):
        raise ValueError(f"unsupported signal expression: {expression!r}")
    return re.sub(r"\s+", "", value)


def eligible_sites(stage2_sites: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    result = [
        site
        for site in stage2_sites
        if site["stage2_status"] == STAGE2_ELIGIBLE
    ]
    result.sort(key=lambda site: (str(site["module"]), numeric_site_id(str(site["site_id"]))))
    return result


def build_groups(
    stage2_sites: Sequence[dict[str, Any]],
    group_width: int,
) -> tuple[list[dict[str, Any]], list[MonitorSite]]:
    if group_width <= 0 or group_width > 2048:
        raise ValueError("group width must be between 1 and 2048")

    by_module: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for site in eligible_sites(stage2_sites):
        by_module[str(site["module"])].append(site)

    groups: list[dict[str, Any]] = []
    flattened: list[MonitorSite] = []
    group_id = 1

    for module in sorted(by_module):
        module_sites = by_module[module]
        for start in range(0, len(module_sites), group_width):
            chunk = module_sites[start : start + group_width]
            group_records: list[dict[str, Any]] = []

            for local_index, site in enumerate(chunk):
                record = MonitorSite(
                    group_id=group_id,
                    local_index=local_index,
                    site_id=str(site["site_id"]),
                    site_key=str(site["site_key"]),
                    module=module,
                    source_net=str(site["source_net"]),
                    source_key=str(site["source_key"]),
                    source_kind=str(site["source_kind"]),
                    state_site=bool(site["state_site"]),
                    logic_fanout=int(site["logic_fanout"]),
                    fanout_bucket=str(site["fanout_bucket"]),
                )
                flattened.append(record)
                group_records.append(
                    {
                        "local_index": local_index,
                        "site_id": record.site_id,
                        "site_key": record.site_key,
                        "source_net": record.source_net,
                        "source_key": record.source_key,
                        "source_kind": record.source_kind,
                        "state_site": record.state_site,
                        "logic_fanout": record.logic_fanout,
                        "fanout_bucket": record.fanout_bucket,
                    }
                )

            groups.append(
                {
                    "group_id": group_id,
                    "module": module,
                    "width": len(chunk),
                    "sites": group_records,
                }
            )
            group_id += 1

    return groups, flattened


def generated_monitor_prelude() -> str:
    return r'''`timescale 1ns/1ps

// -----------------------------------------------------------------------------
// Auto-generated Fault2Assertion Stage-3 compact activity monitor.
//
// Observation only:
//   * does not drive design signals;
//   * does not call $dumpfile or $dumpvars;
//   * starts measurement after reset is released;
//   * writes one compact TSV row per monitored site and bound design instance.
// -----------------------------------------------------------------------------

package f2a_activity_pkg;
  longint unsigned cycle_count = 0;
  bit measurement_active = 0;
  integer output_fd = 0;
  string output_path = "";

  function automatic bit enabled();
    return $test$plusargs("f2a_activity");
  endfunction

  // The TSV write logic is intentionally inlined inside each final block.
  // IEEE 1800 allows final procedures to contain function-legal statements,
  // but they may not enable a user-defined task.  Keeping only shared state and
  // a zero-time function in this package avoids Xcelium BADTFB errors.
endpackage

module f2a_activity_clock_monitor (
  input wire clk_i,
  input wire rst_ni
);
  always @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      f2a_activity_pkg::cycle_count = 0;
      f2a_activity_pkg::measurement_active = 0;
    end else if (f2a_activity_pkg::enabled()) begin
      f2a_activity_pkg::cycle_count = f2a_activity_pkg::cycle_count + 1;
      if (!f2a_activity_pkg::measurement_active) begin
        f2a_activity_pkg::measurement_active = 1;
        $display(
          "[F2A_ACTIVITY] measurement started at cycle %0d time %0t",
          f2a_activity_pkg::cycle_count,
          $time
        );
      end
    end
  end
endmodule

module f2a_activity_vector_monitor #(
  parameter integer WIDTH = 1,
  parameter integer GROUP_ID = 0
) (
  input wire [WIDTH-1:0] values_i
);
  bit ready;
  bit [WIDTH-1:0] initialized;
  bit [WIDTH-1:0] seen_0;
  bit [WIDTH-1:0] seen_1;
  bit [WIDTH-1:0] unknown_seen;
  logic [WIDTH-1:0] previous_value;

  integer unsigned value_change_count [0:WIDTH-1];
  integer unsigned binary_toggle_count [0:WIDTH-1];
  longint signed first_observation_cycle [0:WIDTH-1];
  longint signed first_change_cycle [0:WIDTH-1];
  longint signed last_change_cycle [0:WIDTH-1];
  longint signed first_observation_time [0:WIDTH-1];
  longint signed first_change_time [0:WIDTH-1];
  longint signed last_change_time [0:WIDTH-1];

  string instance_path;
  string final_last_value_text;
  integer init_index;
  integer start_index;
  integer final_index;

  function automatic bit is_binary(input logic value);
    return (value === 1'b0) || (value === 1'b1);
  endfunction

  task automatic observe(input integer index);
    logic current_value;
    current_value = values_i[index];

    if (!initialized[index]) begin
      initialized[index] = 1'b1;
      first_observation_cycle[index] = f2a_activity_pkg::cycle_count;
      first_observation_time[index] = $time;
      previous_value[index] = current_value;
    end else if (current_value !== previous_value[index]) begin
      value_change_count[index] = value_change_count[index] + 1;

      if (is_binary(previous_value[index]) &&
          is_binary(current_value) &&
          (current_value !== previous_value[index])) begin
        binary_toggle_count[index] = binary_toggle_count[index] + 1;
      end

      if (first_change_cycle[index] < 0) begin
        first_change_cycle[index] = f2a_activity_pkg::cycle_count;
        first_change_time[index] = $time;
      end

      last_change_cycle[index] = f2a_activity_pkg::cycle_count;
      last_change_time[index] = $time;
      previous_value[index] = current_value;
    end

    case (current_value)
      1'b0: seen_0[index] = 1'b1;
      1'b1: seen_1[index] = 1'b1;
      default: unknown_seen[index] = 1'b1;
    endcase
  endtask

  initial begin
    ready = 1'b0;
    initialized = '0;
    seen_0 = '0;
    seen_1 = '0;
    unknown_seen = '0;
    previous_value = 'x;
    instance_path = $sformatf("%m");

    for (init_index = 0; init_index < WIDTH; init_index = init_index + 1) begin
      value_change_count[init_index] = 0;
      binary_toggle_count[init_index] = 0;
      first_observation_cycle[init_index] = -1;
      first_change_cycle[init_index] = -1;
      last_change_cycle[init_index] = -1;
      first_observation_time[init_index] = -1;
      first_change_time[init_index] = -1;
      last_change_time[init_index] = -1;
    end

    ready = 1'b1;
  end

  // Capture the value of every monitored site when post-reset measurement starts,
  // including signals that remain constant for the entire workload.
  always @(posedge f2a_activity_pkg::measurement_active) begin
    if (ready && f2a_activity_pkg::enabled()) begin
      for (start_index = 0; start_index < WIDTH; start_index = start_index + 1) begin
        observe(start_index);
      end
    end
  end

  genvar track_index;
  generate
    for (track_index = 0; track_index < WIDTH; track_index = track_index + 1) begin : g_track
      always @(values_i[track_index]) begin
        if (ready &&
            f2a_activity_pkg::enabled() &&
            f2a_activity_pkg::measurement_active) begin
          observe(track_index);
        end
      end
    end
  endgenerate

  final begin
    if (f2a_activity_pkg::enabled()) begin
      // Do not call a user-defined task from a final procedure.  Xcelium
      // enforces IEEE 1800 final-procedure restrictions and reports BADTFB.
      // Open the shared output file and emit the rows directly with system
      // file-I/O calls, which are legal zero-time final-procedure operations.
      if (f2a_activity_pkg::output_fd == 0) begin
        if (!$value$plusargs(
              "f2a_activity_output=%s",
              f2a_activity_pkg::output_path
            )) begin
          f2a_activity_pkg::output_path = "f2a_activity_raw.tsv";
        end

        f2a_activity_pkg::output_fd = $fopen(
          f2a_activity_pkg::output_path,
          "w"
        );

        if (f2a_activity_pkg::output_fd == 0) begin
          $fatal(
            2,
            "[F2A_ACTIVITY] cannot open output file: %0s",
            f2a_activity_pkg::output_path
          );
        end

        $fdisplay(
          f2a_activity_pkg::output_fd,
          "#F2A_ACTIVITY_V1"
        );
        $fdisplay(
          f2a_activity_pkg::output_fd,
          "#group_id\tlocal_index\tinstance_path\tseen_0\tseen_1\tunknown_seen\tvalue_change_count\tbinary_toggle_count\tfirst_observation_cycle\tfirst_change_cycle\tlast_change_cycle\tfirst_observation_time\tfirst_change_time\tlast_change_time\tlast_value"
        );
      end

      for (final_index = 0; final_index < WIDTH; final_index = final_index + 1) begin
        case (previous_value[final_index])
          1'b0: final_last_value_text = "0";
          1'b1: final_last_value_text = "1";
          1'bz: final_last_value_text = "z";
          default: final_last_value_text = "x";
        endcase

        $fdisplay(
          f2a_activity_pkg::output_fd,
          "%0d\t%0d\t%0s\t%0d\t%0d\t%0d\t%0d\t%0d\t%0d\t%0d\t%0d\t%0d\t%0d\t%0d\t%0s",
          GROUP_ID,
          final_index,
          instance_path,
          seen_0[final_index],
          seen_1[final_index],
          unknown_seen[final_index],
          value_change_count[final_index],
          binary_toggle_count[final_index],
          first_observation_cycle[final_index],
          first_change_cycle[final_index],
          last_change_cycle[final_index],
          first_observation_time[final_index],
          first_change_time[final_index],
          last_change_time[final_index],
          final_last_value_text
        );
      end

      $fflush(f2a_activity_pkg::output_fd);
    end
  end
endmodule

bind tb_top f2a_activity_clock_monitor f2a_activity_clock_monitor_i (
  .clk_i  (clk),
  .rst_ni (rst_n)
);

'''


def render_monitor(groups: Sequence[dict[str, Any]]) -> str:
    lines: list[str] = [generated_monitor_prelude()]

    for group in groups:
        group_id = int(group["group_id"])
        module = str(group["module"])
        width = int(group["width"])
        records = group["sites"]

        expressions = [
            sv_scalar_expression(str(record["source_net"]))
            for record in records
        ]
        # Concatenation's right-most expression maps to bit zero.
        reversed_expressions = list(reversed(expressions))

        lines.append(
            f"// Group {group_id}: module={module} sites={width}\n"
        )
        lines.append(
            f"bind {sv_module_identifier(module)} "
            f"f2a_activity_vector_monitor #(\n"
            f"  .WIDTH    ({width}),\n"
            f"  .GROUP_ID ({group_id})\n"
            f") f2a_activity_g{group_id:04d} (\n"
            f"  .values_i ({{\n"
        )

        for index, expression in enumerate(reversed_expressions):
            comma = "," if index + 1 < len(reversed_expressions) else ""
            lines.append(f"    {expression}{comma}\n")

        lines.append("  })\n);\n\n")

    return "".join(lines)


def build_monitor_manifest(
    stage2_path: Path,
    stage2_payload: dict[str, Any],
    groups: Sequence[dict[str, Any]],
    monitor_path: Path,
    monitor_text: str,
    group_width: int,
) -> dict[str, Any]:
    eligible_count = sum(int(group["width"]) for group in groups)
    modules = Counter(str(group["module"]) for group in groups)

    manifest = {
        "schema_version": MONITOR_SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "generated_at_utc": utc_now(),
        "stage": "stage_03_activity_monitor_generation",
        "design": stage2_payload["design"],
        "source_stage2": {
            "path": str(stage2_path.resolve()),
            "sha256": sha256_file(stage2_path),
            "static_filter_digest_sha256": stage2_payload[
                "static_filter_digest_sha256"
            ],
            "eligible_site_count": int(
                stage2_payload["stage2_summary"][
                    "eligible_for_activity_profile_count"
                ]
            ),
        },
        "monitor": {
            "path": str(monitor_path.resolve()),
            "sha256": hashlib.sha256(monitor_text.encode("utf-8")).hexdigest(),
            "group_width_limit": group_width,
            "group_count": len(groups),
            "module_count": len(modules),
            "site_count": eligible_count,
            "raw_format_marker": RAW_FORMAT_MARKER,
            "vcd_required": False,
            "observation_starts_after_reset": True,
        },
        "groups": list(groups),
    }
    manifest["mapping_digest_sha256"] = canonical_digest(manifest["groups"])
    return manifest


def monitor_mapping(manifest: dict[str, Any]) -> dict[tuple[int, int], MonitorSite]:
    if manifest.get("stage") != "stage_03_activity_monitor_generation":
        raise SystemExit("ERROR: invalid Stage-3 monitor manifest stage")

    groups = manifest.get("groups")
    if not isinstance(groups, list):
        raise SystemExit("ERROR: monitor manifest groups must be a list")

    expected_digest = manifest.get("mapping_digest_sha256")
    actual_digest = canonical_digest(groups)
    if actual_digest != expected_digest:
        raise SystemExit(
            "ERROR: monitor mapping digest mismatch: "
            f"expected={expected_digest}, actual={actual_digest}"
        )

    mapping: dict[tuple[int, int], MonitorSite] = {}
    seen_site_ids: set[str] = set()

    for group in groups:
        group_id = int(group["group_id"])
        module = str(group["module"])
        width = int(group["width"])
        records = group.get("sites")
        if not isinstance(records, list) or len(records) != width:
            raise SystemExit(f"ERROR: invalid manifest group width: {group_id}")

        for record in records:
            local_index = int(record["local_index"])
            key = (group_id, local_index)
            if key in mapping:
                raise SystemExit(f"ERROR: duplicate monitor key: {key}")

            site_id = str(record["site_id"])
            numeric_site_id(site_id)
            if site_id in seen_site_ids:
                raise SystemExit(f"ERROR: duplicate monitored site ID: {site_id}")
            seen_site_ids.add(site_id)

            mapping[key] = MonitorSite(
                group_id=group_id,
                local_index=local_index,
                site_id=site_id,
                site_key=str(record["site_key"]),
                module=module,
                source_net=str(record["source_net"]),
                source_key=str(record["source_key"]),
                source_kind=str(record["source_kind"]),
                state_site=bool(record["state_site"]),
                logic_fanout=int(record["logic_fanout"]),
                fanout_bucket=str(record["fanout_bucket"]),
            )

    expected_site_count = int(manifest["monitor"]["site_count"])
    if len(mapping) != expected_site_count:
        raise SystemExit(
            "ERROR: monitor site count mismatch: "
            f"manifest={expected_site_count}, mapping={len(mapping)}"
        )
    return mapping


def parse_bool_field(value: str, label: str, line_number: int) -> bool:
    if value == "0":
        return False
    if value == "1":
        return True
    raise SystemExit(
        f"ERROR: invalid {label} on raw activity line {line_number}: {value!r}"
    )


def parse_nonnegative(value: str, label: str, line_number: int) -> int:
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise SystemExit(
            f"ERROR: invalid {label} on raw activity line {line_number}: {value!r}"
        ) from exc
    if parsed < 0:
        raise SystemExit(
            f"ERROR: negative {label} on raw activity line {line_number}: {parsed}"
        )
    return parsed


def parse_signed(value: str, label: str, line_number: int) -> int:
    try:
        return int(value, 10)
    except ValueError as exc:
        raise SystemExit(
            f"ERROR: invalid {label} on raw activity line {line_number}: {value!r}"
        ) from exc


def parse_raw_activity(
    raw_path: Path,
    mapping: dict[tuple[int, int], MonitorSite],
) -> tuple[dict[str, AggregateActivity], dict[str, Any]]:
    require_nonempty_file(raw_path, "raw activity TSV")

    aggregates = {
        site.site_id: AggregateActivity()
        for site in mapping.values()
    }
    marker_seen = False
    data_rows = 0
    instance_paths: set[str] = set()

    with raw_path.open("r", encoding="utf-8", errors="strict") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.rstrip("\n")
            if not line:
                continue
            if line.startswith("#"):
                if line == RAW_FORMAT_MARKER:
                    marker_seen = True
                continue

            fields = line.split("\t")
            if len(fields) != 15:
                raise SystemExit(
                    "ERROR: expected 15 tab-separated fields on raw activity "
                    f"line {line_number}; got {len(fields)}"
                )

            try:
                group_id = int(fields[0], 10)
                local_index = int(fields[1], 10)
            except ValueError as exc:
                raise SystemExit(
                    f"ERROR: invalid group/index on raw activity line {line_number}"
                ) from exc

            key = (group_id, local_index)
            if key not in mapping:
                raise SystemExit(
                    f"ERROR: unknown monitor key on line {line_number}: {key}"
                )

            instance_path = fields[2]
            if not instance_path or "\t" in instance_path:
                raise SystemExit(
                    f"ERROR: invalid instance path on line {line_number}: {instance_path!r}"
                )
            instance_paths.add(instance_path)

            row = {
                "seen_0": parse_bool_field(fields[3], "seen_0", line_number),
                "seen_1": parse_bool_field(fields[4], "seen_1", line_number),
                "unknown_seen": parse_bool_field(
                    fields[5], "unknown_seen", line_number
                ),
                "value_change_count": parse_nonnegative(
                    fields[6], "value_change_count", line_number
                ),
                "binary_toggle_count": parse_nonnegative(
                    fields[7], "binary_toggle_count", line_number
                ),
                "first_observation_cycle": parse_signed(
                    fields[8], "first_observation_cycle", line_number
                ),
                "first_change_cycle": parse_signed(
                    fields[9], "first_change_cycle", line_number
                ),
                "last_change_cycle": parse_signed(
                    fields[10], "last_change_cycle", line_number
                ),
                "first_observation_time": parse_signed(
                    fields[11], "first_observation_time", line_number
                ),
                "first_change_time": parse_signed(
                    fields[12], "first_change_time", line_number
                ),
                "last_change_time": parse_signed(
                    fields[13], "last_change_time", line_number
                ),
                "last_value": fields[14].lower(),
            }

            if row["last_value"] not in {"0", "1", "x", "z"}:
                raise SystemExit(
                    "ERROR: invalid last_value on raw activity line "
                    f"{line_number}: {row['last_value']!r}"
                )

            site_id = mapping[key].site_id
            aggregates[site_id].add_row(row)
            data_rows += 1

    if not marker_seen:
        raise SystemExit(
            f"ERROR: raw activity marker {RAW_FORMAT_MARKER!r} was not found"
        )
    if data_rows == 0:
        raise SystemExit("ERROR: raw activity TSV contains no data rows")

    metadata = {
        "data_row_count": data_rows,
        "bound_instance_path_count": len(instance_paths),
        "site_with_at_least_one_row_count": sum(
            1 for aggregate in aggregates.values() if aggregate.instance_count > 0
        ),
    }
    return aggregates, metadata


def activity_decision(activity: AggregateActivity) -> tuple[str, list[str], str | None]:
    if activity.instance_count == 0:
        return "excluded_no_monitor_row", [], "no_monitor_row"
    if activity.seen_0 and activity.seen_1:
        return "eligible_for_fault_classification", ["SA0", "SA1"], None
    if activity.seen_0:
        return "eligible_for_fault_classification", ["SA1"], None
    if activity.seen_1:
        return "eligible_for_fault_classification", ["SA0"], None
    if activity.unknown_seen:
        return "excluded_by_activity_filter", [], "unknown_only"
    return "excluded_by_activity_filter", [], "no_binary_observation"


def build_stage3_payload(
    stage2_path: Path,
    stage2_payload: dict[str, Any],
    manifest_path: Path,
    manifest: dict[str, Any],
    raw_path: Path,
    aggregates: dict[str, AggregateActivity],
    raw_metadata: dict[str, Any],
    workload: str,
    run_directory: str | None,
) -> dict[str, Any]:
    stage2_sites = stage2_payload["sites"]
    result_sites: list[dict[str, Any]] = []

    status_counts: Counter[str] = Counter()
    exclusion_counts: Counter[str] = Counter()
    polarity_counts: Counter[str] = Counter()
    source_kind_counts: Counter[str] = Counter()
    module_counts: Counter[str] = Counter()
    unknown_eligible_count = 0

    for site in stage2_sites:
        # Preserve the complete Stage-2 structural and safety record for Stage 4.
        result = dict(site)
        result.update(
            {
                "stage3_status": "not_profiled_stage2_excluded",
                "eligible_polarities": [],
                "activity_exclusion_reason": None,
                "activity": None,
            }
        )

        if site["stage2_status"] == STAGE2_ELIGIBLE:
            aggregate = aggregates[str(site["site_id"])]
            status, polarities, reason = activity_decision(aggregate)
            result["stage3_status"] = status
            result["eligible_polarities"] = polarities
            result["activity_exclusion_reason"] = reason
            result["activity"] = aggregate.payload()

            status_counts[status] += 1
            if reason is not None:
                exclusion_counts[reason] += 1
            for polarity in polarities:
                polarity_counts[polarity] += 1

            if status == "eligible_for_fault_classification":
                source_kind_counts[str(site["source_kind"])] += 1
                module_counts[str(site["module"])] += 1
                if aggregate.unknown_seen:
                    unknown_eligible_count += 1
        else:
            status_counts["not_profiled_stage2_excluded"] += 1

        result_sites.append(result)

    profiled_count = int(
        stage2_payload["stage2_summary"]["eligible_for_activity_profile_count"]
    )
    eligible_count = status_counts["eligible_for_fault_classification"]

    summary = {
        "workload": workload,
        "raw_site_count": len(result_sites),
        "stage2_profile_requested_count": profiled_count,
        "site_with_monitor_row_count": raw_metadata[
            "site_with_at_least_one_row_count"
        ],
        "activity_eligible_site_count": eligible_count,
        "activity_excluded_site_count": (
            status_counts["excluded_by_activity_filter"]
            + status_counts["excluded_no_monitor_row"]
        ),
        "stage2_excluded_not_profiled_count": status_counts[
            "not_profiled_stage2_excluded"
        ],
        "activity_eligible_with_unknown_seen_count": unknown_eligible_count,
        "eligible_fault_instance_count": sum(polarity_counts.values()),
        "by_stage3_status": dict(sorted(status_counts.items())),
        "activity_exclusion_reason_counts": dict(sorted(exclusion_counts.items())),
        "eligible_polarity_counts": dict(sorted(polarity_counts.items())),
        "eligible_by_source_kind": dict(sorted(source_kind_counts.items())),
        "eligible_by_module": dict(sorted(module_counts.items())),
        "raw_data_row_count": raw_metadata["data_row_count"],
        "bound_instance_path_count": raw_metadata[
            "bound_instance_path_count"
        ],
    }

    digest_records = [
        {
            "site_id": site["site_id"],
            "stage3_status": site["stage3_status"],
            "eligible_polarities": site["eligible_polarities"],
            "activity_exclusion_reason": site["activity_exclusion_reason"],
            "activity": site["activity"],
        }
        for site in result_sites
    ]

    payload = {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "generated_at_utc": utc_now(),
        "stage": STAGE3_NAME,
        "design": stage2_payload["design"],
        "workload": workload,
        "source_stage2": {
            "path": str(stage2_path.resolve()),
            "sha256": sha256_file(stage2_path),
            "static_filter_digest_sha256": stage2_payload[
                "static_filter_digest_sha256"
            ],
        },
        "monitor_manifest": {
            "path": str(manifest_path.resolve()),
            "sha256": sha256_file(manifest_path),
            "mapping_digest_sha256": manifest["mapping_digest_sha256"],
            "monitor_sha256": manifest["monitor"]["sha256"],
        },
        "raw_activity": {
            "path": str(raw_path.resolve()),
            "sha256": sha256_file(raw_path),
            "format_marker": RAW_FORMAT_MARKER,
            **raw_metadata,
        },
        "simulation_run_directory": run_directory,
        "definitions": {
            "measurement_window": (
                "from the first positive clock edge after reset deassertion "
                "until normal simulation finish"
            ),
            "sa0_activation": "golden source value 1 was observed",
            "sa1_activation": "golden source value 0 was observed",
            "unknown_policy": (
                "X/Z does not invalidate a site if a required binary activation "
                "value was also observed; unknown_seen is retained as a feature"
            ),
            "vcd_generated": False,
        },
        "stage3_summary": summary,
        "activity_digest_sha256": canonical_digest(digest_records),
        "sites": result_sites,
    }
    return payload


def render_stage3_report(payload: dict[str, Any]) -> str:
    summary = payload["stage3_summary"]
    lines = [
        "Fault2Assertion Stage 3 - Golden Activity Filtering\n",
        "=" * 80 + "\n",
        f"Generated UTC         : {payload['generated_at_utc']}\n",
        f"Design                : {payload['design']}\n",
        f"Workload              : {payload['workload']}\n",
        f"Stage-2 digest        : {payload['source_stage2']['static_filter_digest_sha256']}\n",
        f"Monitor mapping digest: {payload['monitor_manifest']['mapping_digest_sha256']}\n",
        f"Raw activity SHA256   : {payload['raw_activity']['sha256']}\n",
        f"Stage-3 digest        : {payload['activity_digest_sha256']}\n",
        "\n",
        "Stage-3 summary\n",
        "-" * 80 + "\n",
        f"Raw Stage-1 sites             : {summary['raw_site_count']}\n",
        f"Stage-2 profile requests      : {summary['stage2_profile_requested_count']}\n",
        f"Sites with monitor rows       : {summary['site_with_monitor_row_count']}\n",
        f"Activity-eligible sites       : {summary['activity_eligible_site_count']}\n",
        f"Activity-excluded sites       : {summary['activity_excluded_site_count']}\n",
        f"Stage-2 excluded/not profiled : {summary['stage2_excluded_not_profiled_count']}\n",
        f"Eligible fault instances      : {summary['eligible_fault_instance_count']}\n",
        f"Eligible sites with X/Z flag  : {summary['activity_eligible_with_unknown_seen_count']}\n",
        f"Raw TSV rows                  : {summary['raw_data_row_count']}\n",
        f"Bound monitor instance paths  : {summary['bound_instance_path_count']}\n",
        "\n",
        "Eligible polarity counts\n",
        "-" * 80 + "\n",
    ]

    for key, value in summary["eligible_polarity_counts"].items():
        lines.append(f"{key:8s}: {value}\n")

    lines.extend(
        [
            "\n",
            "Activity exclusion reasons\n",
            "-" * 80 + "\n",
        ]
    )
    if summary["activity_exclusion_reason_counts"]:
        for key, value in summary["activity_exclusion_reason_counts"].items():
            lines.append(f"{key:30s}: {value}\n")
    else:
        lines.append("None\n")

    lines.extend(
        [
            "\n",
            "Eligible sites by source kind\n",
            "-" * 80 + "\n",
        ]
    )
    for key, value in summary["eligible_by_source_kind"].items():
        lines.append(f"{key:35s}: {value}\n")

    lines.extend(
        [
            "\n",
            "Stage-3 interpretation\n",
            "-" * 80 + "\n",
            "No fault type has been assigned yet.\n",
            "No fault instance or faulty netlist has been generated.\n",
            "Only sites marked eligible_for_fault_classification enter Stage 4.\n",
            "SA0 is eligible only when golden value 1 was observed.\n",
            "SA1 is eligible only when golden value 0 was observed.\n",
            "VCD was not generated; activity came from the compact bound monitor.\n",
        ]
    )
    return "".join(lines)


def command_make_monitor(args: argparse.Namespace) -> int:
    stage2_path = args.stage2_json.resolve()
    monitor_path = args.sv_output.resolve()
    manifest_path = args.manifest_output.resolve()
    require_nonempty_file(stage2_path, "Stage-2 JSON")

    stage2_payload = read_json(stage2_path)
    stage2_sites = validate_stage2_payload(stage2_payload, stage2_path)

    if args.expect_stage2_digest:
        actual = stage2_payload["static_filter_digest_sha256"]
        if actual != args.expect_stage2_digest:
            raise SystemExit(
                "ERROR: Stage-2 digest mismatch: "
                f"expected={args.expect_stage2_digest}, actual={actual}"
            )

    groups, flattened = build_groups(stage2_sites, args.group_width)
    expected_eligible = int(
        stage2_payload["stage2_summary"]["eligible_for_activity_profile_count"]
    )
    if len(flattened) != expected_eligible:
        raise SystemExit(
            "ERROR: monitor site count mismatch: "
            f"expected={expected_eligible}, actual={len(flattened)}"
        )

    monitor_text = render_monitor(groups)
    if re.search(r"\$dump(?:file|vars)\s*\(", monitor_text):
        raise SystemExit("ERROR: generated compact monitor unexpectedly contains VCD calls")
    if "f2a_activity_pkg::emit(" in monitor_text:
        raise SystemExit(
            "ERROR: generated final procedure still calls a user-defined emit task"
        )

    manifest = build_monitor_manifest(
        stage2_path,
        stage2_payload,
        groups,
        monitor_path,
        monitor_text,
        args.group_width,
    )

    write_text(monitor_path, monitor_text, force=args.force, mode=0o644)
    write_json(manifest_path, manifest, force=args.force, mode=0o644)

    print(f"Stage-2 JSON       : {stage2_path}")
    print(f"Stage-2 digest     : {stage2_payload['static_filter_digest_sha256']}")
    print(f"Monitor SV         : {monitor_path}")
    print(f"Monitor SHA256     : {manifest['monitor']['sha256']}")
    print(f"Manifest           : {manifest_path}")
    print(f"Mapping digest     : {manifest['mapping_digest_sha256']}")
    print(f"Eligible sites     : {len(flattened)}")
    print(f"Monitor groups     : {len(groups)}")
    print(f"Design modules     : {manifest['monitor']['module_count']}")
    print("VCD required       : 0")
    print("Stage-3 monitor generation: PASS")
    return 0


def command_validate_monitor(args: argparse.Namespace) -> int:
    stage2_path = args.stage2_json.resolve()
    monitor_path = args.sv.resolve()
    manifest_path = args.manifest.resolve()
    require_nonempty_file(stage2_path, "Stage-2 JSON")
    require_nonempty_file(monitor_path, "activity monitor SV")
    require_nonempty_file(manifest_path, "activity monitor manifest")

    stage2_payload = read_json(stage2_path)
    stage2_sites = validate_stage2_payload(stage2_payload, stage2_path)
    manifest = read_json(manifest_path)
    mapping = monitor_mapping(manifest)

    expected_stage2_sha = manifest["source_stage2"]["sha256"]
    actual_stage2_sha = sha256_file(stage2_path)
    if actual_stage2_sha != expected_stage2_sha:
        raise SystemExit(
            "ERROR: Stage-2 JSON SHA mismatch: "
            f"expected={expected_stage2_sha}, actual={actual_stage2_sha}"
        )

    expected_stage2_digest = manifest["source_stage2"][
        "static_filter_digest_sha256"
    ]
    if stage2_payload["static_filter_digest_sha256"] != expected_stage2_digest:
        raise SystemExit("ERROR: Stage-2 static-filter digest changed")

    expected_monitor_sha = manifest["monitor"]["sha256"]
    actual_monitor_sha = sha256_file(monitor_path)
    if actual_monitor_sha != expected_monitor_sha:
        raise SystemExit(
            "ERROR: monitor SV SHA mismatch: "
            f"expected={expected_monitor_sha}, actual={actual_monitor_sha}"
        )

    eligible_ids = {
        str(site["site_id"])
        for site in stage2_sites
        if site["stage2_status"] == STAGE2_ELIGIBLE
    }
    mapped_ids = {site.site_id for site in mapping.values()}
    if mapped_ids != eligible_ids:
        missing = sorted(eligible_ids - mapped_ids)[:20]
        extra = sorted(mapped_ids - eligible_ids)[:20]
        raise SystemExit(
            "ERROR: monitor mapping differs from Stage-2 eligible sites; "
            f"missing={missing}, extra={extra}"
        )

    text = monitor_path.read_text(encoding="utf-8")
    for required in (
        "package f2a_activity_pkg",
        "module f2a_activity_clock_monitor",
        "module f2a_activity_vector_monitor",
        "bind tb_top",
        "f2a_activity_output=%s",
    ):
        if required not in text:
            raise SystemExit(f"ERROR: generated monitor is missing: {required}")

    if re.search(r"\$dump(?:file|vars)\s*\(", text):
        raise SystemExit("ERROR: compact monitor contains VCD system calls")
    if "f2a_activity_pkg::emit(" in text:
        raise SystemExit(
            "ERROR: compact monitor calls a user-defined task from a final procedure"
        )
    bind_count = len(re.findall(r"(?m)^bind\s+", text))
    expected_bind_count = int(manifest["monitor"]["group_count"]) + 1
    if bind_count != expected_bind_count:
        raise SystemExit(
            "ERROR: bind count mismatch: "
            f"expected={expected_bind_count}, actual={bind_count}"
        )

    print(f"Stage-2 JSON       : {stage2_path}")
    print(f"Monitor SV         : {monitor_path}")
    print(f"Monitor SHA256     : {actual_monitor_sha}")
    print(f"Manifest           : {manifest_path}")
    print(f"Mapping digest     : {manifest['mapping_digest_sha256']}")
    print(f"Mapped sites       : {len(mapping)}")
    print(f"Bind statements    : {bind_count}")
    print("VCD calls          : 0")
    print("Stage-3 monitor validation: PASS")
    return 0


def command_parse_results(args: argparse.Namespace) -> int:
    stage2_path = args.stage2_json.resolve()
    manifest_path = args.manifest.resolve()
    raw_path = args.raw_activity.resolve()
    json_output = args.json_output.resolve()
    text_output = args.text_output.resolve()

    require_nonempty_file(stage2_path, "Stage-2 JSON")
    require_nonempty_file(manifest_path, "activity monitor manifest")
    require_nonempty_file(raw_path, "raw activity TSV")

    stage2_payload = read_json(stage2_path)
    validate_stage2_payload(stage2_payload, stage2_path)
    manifest = read_json(manifest_path)
    mapping = monitor_mapping(manifest)

    if sha256_file(stage2_path) != manifest["source_stage2"]["sha256"]:
        raise SystemExit("ERROR: Stage-2 JSON differs from monitor-generation input")
    if (
        stage2_payload["static_filter_digest_sha256"]
        != manifest["source_stage2"]["static_filter_digest_sha256"]
    ):
        raise SystemExit("ERROR: Stage-2 digest differs from monitor manifest")

    aggregates, raw_metadata = parse_raw_activity(raw_path, mapping)
    payload = build_stage3_payload(
        stage2_path=stage2_path,
        stage2_payload=stage2_payload,
        manifest_path=manifest_path,
        manifest=manifest,
        raw_path=raw_path,
        aggregates=aggregates,
        raw_metadata=raw_metadata,
        workload=args.workload,
        run_directory=args.run_directory,
    )

    write_json(json_output, payload, force=args.force, mode=0o644)
    write_text(
        text_output,
        render_stage3_report(payload),
        force=args.force,
        mode=0o644,
    )

    summary = payload["stage3_summary"]
    print(f"Raw activity TSV   : {raw_path}")
    print(f"Raw activity SHA   : {payload['raw_activity']['sha256']}")
    print(f"Stage-3 JSON       : {json_output}")
    print(f"Stage-3 report     : {text_output}")
    print(f"Profiled sites     : {summary['stage2_profile_requested_count']}")
    print(f"Activity eligible  : {summary['activity_eligible_site_count']}")
    print(f"Activity excluded  : {summary['activity_excluded_site_count']}")
    print(f"Fault instances    : {summary['eligible_fault_instance_count']}")
    print(f"Stage-3 digest     : {payload['activity_digest_sha256']}")
    print("Stage-3 result parsing: PASS")
    return 0


def rebuild_stage3_from_output(payload: dict[str, Any]) -> dict[str, Any]:
    stage2_path = Path(payload["source_stage2"]["path"])
    manifest_path = Path(payload["monitor_manifest"]["path"])
    raw_path = Path(payload["raw_activity"]["path"])

    require_nonempty_file(stage2_path, "Stage-2 JSON")
    require_nonempty_file(manifest_path, "activity monitor manifest")
    require_nonempty_file(raw_path, "raw activity TSV")

    if sha256_file(stage2_path) != payload["source_stage2"]["sha256"]:
        raise SystemExit("ERROR: Stage-2 JSON SHA changed after Stage 3")
    if sha256_file(manifest_path) != payload["monitor_manifest"]["sha256"]:
        raise SystemExit("ERROR: monitor manifest SHA changed after Stage 3")
    if sha256_file(raw_path) != payload["raw_activity"]["sha256"]:
        raise SystemExit("ERROR: raw activity SHA changed after Stage 3")

    stage2_payload = read_json(stage2_path)
    validate_stage2_payload(stage2_payload, stage2_path)
    manifest = read_json(manifest_path)
    mapping = monitor_mapping(manifest)
    aggregates, raw_metadata = parse_raw_activity(raw_path, mapping)

    return build_stage3_payload(
        stage2_path=stage2_path,
        stage2_payload=stage2_payload,
        manifest_path=manifest_path,
        manifest=manifest,
        raw_path=raw_path,
        aggregates=aggregates,
        raw_metadata=raw_metadata,
        workload=str(payload["workload"]),
        run_directory=payload.get("simulation_run_directory"),
    )


def command_validate_output(args: argparse.Namespace) -> int:
    output_path = args.json.resolve()
    require_nonempty_file(output_path, "Stage-3 JSON")
    payload = read_json(output_path)

    if payload.get("stage") != STAGE3_NAME:
        raise SystemExit("ERROR: invalid Stage-3 output stage")

    sites = payload.get("sites")
    if not isinstance(sites, list):
        raise SystemExit("ERROR: Stage-3 sites must be a list")

    seen_ids: set[str] = set()
    for site in sites:
        site_id = str(site["site_id"])
        numeric_site_id(site_id)
        if site_id in seen_ids:
            raise SystemExit(f"ERROR: duplicate Stage-3 site ID: {site_id}")
        seen_ids.add(site_id)

        status = site["stage3_status"]
        polarities = site["eligible_polarities"]
        if status == "eligible_for_fault_classification":
            if not polarities or any(p not in {"SA0", "SA1"} for p in polarities):
                raise SystemExit(
                    f"ERROR: invalid eligible polarities for {site_id}: {polarities}"
                )
            activity = site["activity"]
            if "SA0" in polarities and not activity["seen_1"]:
                raise SystemExit(f"ERROR: SA0 selected without golden 1: {site_id}")
            if "SA1" in polarities and not activity["seen_0"]:
                raise SystemExit(f"ERROR: SA1 selected without golden 0: {site_id}")
        elif polarities:
            raise SystemExit(
                f"ERROR: excluded Stage-3 site has polarities: {site_id}"
            )

    rebuilt = rebuild_stage3_from_output(payload)
    expected_digest = payload["activity_digest_sha256"]
    actual_digest = rebuilt["activity_digest_sha256"]
    if actual_digest != expected_digest:
        raise SystemExit(
            "ERROR: Stage-3 digest mismatch after full rebuild: "
            f"expected={expected_digest}, actual={actual_digest}"
        )

    if rebuilt["stage3_summary"] != payload["stage3_summary"]:
        raise SystemExit("ERROR: Stage-3 summary differs after full rebuild")

    print(f"Stage-3 JSON       : {output_path}")
    print(f"Sites              : {len(sites)}")
    print(
        "Activity eligible  : "
        f"{payload['stage3_summary']['activity_eligible_site_count']}"
    )
    print(
        "Fault instances    : "
        f"{payload['stage3_summary']['eligible_fault_instance_count']}"
    )
    print(f"Stage-3 digest     : {actual_digest}")
    print("Stage-3 validation: PASS")
    return 0


def synthetic_stage2_payload() -> dict[str, Any]:
    sites = [
        {
            "site_id": "RS000001",
            "site_key": "child|data_q",
            "module": "child",
            "source_net": "data_q",
            "source_key": "data_q",
            "source_kind": "sequential_output",
            "state_site": True,
            "logic_fanout": 2,
            "fanout_bucket": "2",
            "static_safety": {"clock_safe": True, "reset_set_safe": True},
            "stage2_status": STAGE2_ELIGIBLE,
            "exclusion_reasons": [],
        },
        {
            "site_id": "RS000002",
            "site_key": "child|escaped",
            "module": "child",
            "source_net": "\\escaped/name",
            "source_key": "\\escaped/name",
            "source_kind": "combinational_output",
            "state_site": False,
            "logic_fanout": 1,
            "fanout_bucket": "1",
            "static_safety": {"clock_safe": True, "reset_set_safe": True},
            "stage2_status": STAGE2_ELIGIBLE,
            "exclusion_reasons": [],
        },
        {
            "site_id": "RS000003",
            "site_key": "top|flag",
            "module": "top",
            "source_net": "flag",
            "source_key": "flag",
            "source_kind": "combinational_output",
            "state_site": False,
            "logic_fanout": 1,
            "fanout_bucket": "1",
            "static_safety": {"clock_safe": True, "reset_set_safe": True},
            "stage2_status": STAGE2_ELIGIBLE,
            "exclusion_reasons": [],
        },
        {
            "site_id": "RS000004",
            "site_key": "top|excluded",
            "module": "top",
            "source_net": "excluded",
            "source_key": "excluded",
            "source_kind": "module_input",
            "state_site": False,
            "logic_fanout": 1,
            "fanout_bucket": "1",
            "static_safety": {"clock_safe": False, "reset_set_safe": True},
            "stage2_status": "excluded_by_static_filter",
            "exclusion_reasons": ["protected_clock_signal_cone"],
        },
    ]
    return {
        "schema_version": "1.0",
        "program_version": "test",
        "generated_at_utc": utc_now(),
        "stage": STAGE2_NAME,
        "design": "synthetic",
        "source": {"path": "synthetic.v", "sha256": "0" * 64},
        "stage2_summary": {
            "raw_site_count": 4,
            "eligible_for_activity_profile_count": 3,
        },
        "static_filter_digest_sha256": "1" * 64,
        "sites": sites,
    }


def command_self_test(_: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="f2a_activity_selftest_") as temporary:
        root = Path(temporary)
        stage2_path = root / "stage2.json"
        monitor_path = root / "monitor.sv"
        manifest_path = root / "manifest.json"
        raw_path = root / "activity.tsv"

        stage2_payload = synthetic_stage2_payload()
        stage2_path.write_text(
            json.dumps(stage2_payload, indent=2) + "\n",
            encoding="utf-8",
        )
        sites = validate_stage2_payload(stage2_payload, stage2_path)
        groups, flattened = build_groups(sites, group_width=2)
        assert len(flattened) == 3
        assert len(groups) == 2

        monitor_text = render_monitor(groups)
        assert not re.search(r"\$dump(?:file|vars)\s*\(", monitor_text)
        assert "f2a_activity_pkg::emit(" not in monitor_text
        assert "task automatic emit" not in monitor_text
        assert "task automatic ensure_output_open" not in monitor_text
        assert "\\escaped/name " in monitor_text
        monitor_path.write_text(monitor_text, encoding="utf-8")

        manifest = build_monitor_manifest(
            stage2_path,
            stage2_payload,
            groups,
            monitor_path,
            monitor_text,
            2,
        )
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )
        mapping = monitor_mapping(manifest)
        assert len(mapping) == 3

        # RS000001 sees both values -> SA0 and SA1.
        # RS000002 sees only 0 -> SA1.
        # RS000003 sees only X -> excluded.
        raw_path.write_text(
            "\n".join(
                [
                    RAW_FORMAT_MARKER,
                    "1\t0\ttop.u_child.monitor\t1\t1\t0\t4\t4\t1\t2\t8\t10\t20\t80\t1",
                    "1\t1\ttop.u_child.monitor\t1\t0\t1\t0\t0\t1\t-1\t-1\t10\t-1\t-1\t0",
                    "2\t0\ttop.monitor\t0\t0\t1\t0\t0\t1\t-1\t-1\t10\t-1\t-1\tx",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        aggregates, metadata = parse_raw_activity(raw_path, mapping)
        assert metadata["data_row_count"] == 3
        assert activity_decision(aggregates["RS000001"])[1] == ["SA0", "SA1"]
        assert activity_decision(aggregates["RS000002"])[1] == ["SA1"]
        assert activity_decision(aggregates["RS000003"])[2] == "unknown_only"

    print("Synthetic sites      : 4")
    print("Monitored sites      : 3")
    print("Monitor groups       : 2")
    print("VCD calls            : 0")
    print("Final task calls     : 0")
    print("Polarity decisions   : PASS")
    print("Stage-3 self-test    : PASS")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate and analyze compact golden activity monitors."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {PROGRAM_VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    self_test = subparsers.add_parser("self-test", help="run internal Stage-3 tests")
    self_test.set_defaults(func=command_self_test)

    make_monitor = subparsers.add_parser(
        "make-monitor",
        help="generate observation-only SystemVerilog and its manifest",
    )
    make_monitor.add_argument("--stage2-json", type=Path, required=True)
    make_monitor.add_argument("--sv-output", type=Path, required=True)
    make_monitor.add_argument("--manifest-output", type=Path, required=True)
    make_monitor.add_argument(
        "--group-width",
        type=int,
        default=DEFAULT_GROUP_WIDTH,
        help=f"maximum scalar sites per bound vector monitor (default {DEFAULT_GROUP_WIDTH})",
    )
    make_monitor.add_argument("--expect-stage2-digest")
    make_monitor.add_argument("--force", action="store_true")
    make_monitor.set_defaults(func=command_make_monitor)

    validate_monitor = subparsers.add_parser(
        "validate-monitor",
        help="validate monitor SHA, mapping, and Stage-2 coverage",
    )
    validate_monitor.add_argument("--stage2-json", type=Path, required=True)
    validate_monitor.add_argument("--sv", type=Path, required=True)
    validate_monitor.add_argument("--manifest", type=Path, required=True)
    validate_monitor.set_defaults(func=command_validate_monitor)

    parse_results = subparsers.add_parser(
        "parse-results",
        help="parse compact simulator TSV and produce Stage-3 JSON/report",
    )
    parse_results.add_argument("--stage2-json", type=Path, required=True)
    parse_results.add_argument("--manifest", type=Path, required=True)
    parse_results.add_argument("--raw-activity", type=Path, required=True)
    parse_results.add_argument("--workload", default="crc32")
    parse_results.add_argument("--run-directory")
    parse_results.add_argument("--json-output", type=Path, required=True)
    parse_results.add_argument("--text-output", type=Path, required=True)
    parse_results.add_argument("--force", action="store_true")
    parse_results.set_defaults(func=command_parse_results)

    validate_output = subparsers.add_parser(
        "validate-output",
        help="fully rebuild and validate a Stage-3 output JSON",
    )
    validate_output.add_argument("--json", type=Path, required=True)
    validate_output.set_defaults(func=command_validate_output)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except (AssertionError, KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
