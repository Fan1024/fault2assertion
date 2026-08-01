#!/usr/bin/env python3
"""Generate one run-local Stage-5 diagnostic mm_ram overlay.

Native execution compiles the immutable original ``mm_ram.sv``. Diagnostic
OBSERVE and QUARANTINE executions compile a generated overlay that:

* preserves the registered out-of-bounds write detector as a procedural,
  first-event recorder;
* suppresses the procedural out-of-bounds read ``$fatal`` and records the same
  read boundary;
* keeps the data path deterministic: an illegal read returns the existing
  ``data_rdata_mux`` default value of zero;
* applies the existing write quarantine action only in QUARANTINE mode.

The external CV32E40P source is never modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Sequence

PROGRAM_VERSION = "4.1.0"
MARKER = "Fault2Assertion Stage-5 diagnostic detector adapter"
FIRST_EVENT_POLICY = "FIRST_VIOLATION_ONLY"

DECLARATION_ANCHOR = "  logic [31:0] error_addr_q;\n"

WRITE_BRANCH_OLD = """        end else begin
          // out of bounds write
        end
"""

WRITE_BRANCH_NEW = """        end else begin
          // out of bounds write
          // OBSERVE preserves the existing no-grant behavior. QUARANTINE
          // acknowledges and drops only this unsafe write transaction.
          if (f2a_assert_mode_q == F2A_ASSERT_DIAGNOSTIC_QUARANTINE) begin
            perip_gnt   = 1'b1;
            transaction = T_PER;
          end
        end
"""

ASSERTION_OLD = """`ifndef VERILATOR
  // signal out of bound writes
  out_of_bounds_write :
  assert property
    (@(posedge clk_i) disable iff (~rst_ni)
     (data_req_i && data_we_i |-> data_addr_i < 2 ** RAM_ADDR_WIDTH
      || data_addr_i == 32'h1000_0000
      || data_addr_i == 32'h1500_0000
      || data_addr_i == 32'h1500_0004
      || data_addr_i == 32'h2000_0000
      || data_addr_i == 32'h2000_0004
      || data_addr_i == 32'h2000_0008
      || data_addr_i == 32'h2000_000c
      || data_addr_i == 32'h2000_0010
      || data_addr_i[31:16] == 16'h1600))
  else $fatal("out of bounds write to %08x with %08x", data_addr_i, data_wdata_i);
`endif
"""

READ_FATAL_OLD = """    end else if (transaction_q == T_ERR) begin
      $display("out of bounds read from %08x", data_addr_i);
      $fatal(2);
    end
"""

READ_FATAL_NEW = """    end else if (transaction_q == T_ERR) begin
      // The original read_mux $fatal is intentionally suppressed only in the
      // diagnostic overlay. data_rdata_mux retains its default value of zero.
    end
"""

DECLARATIONS = r'''

  // Fault2Assertion Stage-5 diagnostic detector adapter.
  localparam int unsigned F2A_ASSERT_NATIVE = 0;
  localparam int unsigned F2A_ASSERT_OBSERVE = 1;
  localparam int unsigned F2A_ASSERT_DIAGNOSTIC_QUARANTINE = 2;

  // Configuration owner: f2a_assertion_mode_init.
  int unsigned f2a_assert_mode_q;
  string f2a_assert_mode_name;
  string f2a_assert_event_file;
  integer f2a_assert_event_fd;

  // Sequential-state owner: f2a_assertion_state.
  longint unsigned f2a_cycle_q;
  longint unsigned f2a_assert_event_count_q;
  logic f2a_detector_seen_q;

  // Combinational-predicate owner: f2a_assertion_predicates.
  logic f2a_write_address_allowed;
  logic f2a_oob_write_violation;
  logic f2a_oob_write_first_event;
  logic f2a_oob_read_violation;
  logic f2a_oob_read_first_event;

  always_comb begin : f2a_assertion_predicates
    // Same address allow-list used by the removed out_of_bounds_write SVA.
    f2a_write_address_allowed =
         !$isunknown(data_addr_i)
      && (data_addr_i < 2 ** RAM_ADDR_WIDTH
          || data_addr_i == 32'h1000_0000
          || data_addr_i == 32'h1500_0000
          || data_addr_i == 32'h1500_0004
          || data_addr_i == 32'h2000_0000
          || data_addr_i == 32'h2000_0004
          || data_addr_i == 32'h2000_0008
          || data_addr_i == 32'h2000_000c
          || data_addr_i == 32'h2000_0010
          || data_addr_i[31:16] == 16'h1600);

    f2a_oob_write_violation =
      data_req_i && data_we_i && !f2a_write_address_allowed;

    // transaction == T_ERR is the decode boundary that will be registered
    // into transaction_q and enter read_mux's original fatal branch one cycle
    // later. Sampling here preserves the offending request address.
    f2a_oob_read_violation =
      data_req_i && !data_we_i && (transaction == T_ERR);

    // One global first-event policy. Write receives priority only if both
    // predicates are simultaneously true.
    f2a_oob_write_first_event =
      f2a_oob_write_violation && !f2a_detector_seen_q;
    f2a_oob_read_first_event =
      f2a_oob_read_violation
      && !f2a_oob_write_violation
      && !f2a_detector_seen_q;
  end

  initial begin : f2a_assertion_mode_init
    string requested_mode;

    f2a_assert_mode_q = F2A_ASSERT_NATIVE;
    f2a_assert_mode_name = "invalid";
    f2a_assert_event_file = "";
    f2a_assert_event_fd = 0;
    requested_mode = "";

    if (!$value$plusargs("f2a_assert_mode=%s", requested_mode)) begin
      $fatal(2, "missing +f2a_assert_mode=<observe|diagnostic_quarantine>");
    end

    case (requested_mode)
      "observe": begin
        f2a_assert_mode_q = F2A_ASSERT_OBSERVE;
        f2a_assert_mode_name = "observe";
      end
      "diagnostic_quarantine": begin
        f2a_assert_mode_q = F2A_ASSERT_DIAGNOSTIC_QUARANTINE;
        f2a_assert_mode_name = "diagnostic_quarantine";
      end
      "native": begin
        $fatal(
          2,
          "native runtime must compile original mm_ram.sv, not the diagnostic overlay"
        );
      end
      default: begin
        $fatal(2, "unsupported f2a_assert_mode=%s", requested_mode);
      end
    endcase

    if (!$value$plusargs("f2a_assert_event_file=%s", f2a_assert_event_file)) begin
      $fatal(2, "missing +f2a_assert_event_file=<absolute path>");
    end

    f2a_assert_event_fd = $fopen(f2a_assert_event_file, "w");
    if (f2a_assert_event_fd == 0) begin
      $fatal(
        2,
        "cannot open Stage-5 assertion event file %s",
        f2a_assert_event_file
      );
    end

    $fdisplay(
      f2a_assert_event_fd,
      "H\tF2A_ASSERT_EVENTS\t1\t%s",
      f2a_assert_mode_name
    );
    $fflush(f2a_assert_event_fd);
    $display("F2A_ASSERT_MODE\t%s", f2a_assert_mode_name);
    $display("F2A_DETECTOR_IMPLEMENTATION\tPROCEDURAL_FIRST_EVENT");
  end

  always_ff @(posedge clk_i or negedge rst_ni) begin : f2a_assertion_state
    if (!rst_ni) begin
      f2a_cycle_q <= 0;
      f2a_assert_event_count_q <= 0;
      f2a_detector_seen_q <= 1'b0;
    end else begin
      f2a_cycle_q <= f2a_cycle_q + 1;

      if (f2a_oob_write_first_event || f2a_oob_read_first_event) begin
        f2a_assert_event_count_q <= f2a_assert_event_count_q + 1;
        f2a_detector_seen_q <= 1'b1;
      end
    end
  end

  task automatic f2a_emit_out_of_bounds_write_event(
    input string action_name
  );
    // Keep the final field separator in a separate write. Some Xcelium
    // versions concatenate the string argument directly after %x when the
    // final tab and %s share the same formatted output call.
    $fwrite(
      f2a_assert_event_fd,
      "A\t%0d\t%0d\t%0t\tPREEXISTING_TB_ASSERTION\tout_of_bounds_write\tILLEGAL_MEMORY_WRITE\t%08x\t%08x\t%01x",
      f2a_assert_event_count_q,
      f2a_cycle_q,
      $time,
      data_addr_i,
      data_wdata_i,
      data_be_i
    );
    $fdisplay(f2a_assert_event_fd, "\t%s", action_name);
    $fflush(f2a_assert_event_fd);

    $display(
      "F2A_ASSERT_EVENT\t%s\tout_of_bounds_write\tcycle=%0d\ttime=%0t\taddr=%08x\twdata=%08x\tbe=%01x",
      action_name,
      f2a_cycle_q,
      $time,
      data_addr_i,
      data_wdata_i,
      data_be_i
    );
  endtask

  task automatic f2a_emit_out_of_bounds_read_event(
    input string action_name
  );
    // Write the action field separately so the TSV delimiter is explicit
    // and cannot be absorbed into the preceding hexadecimal conversion.
    $fwrite(
      f2a_assert_event_fd,
      "A\t%0d\t%0d\t%0t\tPREEXISTING_TB_FATAL\tout_of_bounds_read\tILLEGAL_MEMORY_READ\t%08x\t%08x\t%01x",
      f2a_assert_event_count_q,
      f2a_cycle_q,
      $time,
      data_addr_i,
      data_wdata_i,
      data_be_i
    );
    $fdisplay(f2a_assert_event_fd, "\t%s", action_name);
    $fflush(f2a_assert_event_fd);

    $display(
      "F2A_ASSERT_EVENT\t%s\tout_of_bounds_read\tcycle=%0d\ttime=%0t\taddr=%08x",
      action_name,
      f2a_cycle_q,
      $time,
      data_addr_i
    );
  endtask
'''

DIAGNOSTIC_DETECTOR_BLOCK = r'''`ifndef VERILATOR
  // Native runs retain the original out_of_bounds_write SVA and read_mux
  // fatal. The diagnostic overlay records the first registered detector event
  // and suppresses only its terminating action.
  always @(posedge clk_i) begin : f2a_diagnostic_out_of_bounds_write
    if (rst_ni && f2a_oob_write_first_event) begin
      if (f2a_assert_mode_q == F2A_ASSERT_OBSERVE) begin
        f2a_emit_out_of_bounds_write_event("RECORD_ONLY");
      end else if (
        f2a_assert_mode_q == F2A_ASSERT_DIAGNOSTIC_QUARANTINE
      ) begin
        f2a_emit_out_of_bounds_write_event("RECORD_AND_QUARANTINE");
      end else begin
        $fatal(2, "invalid diagnostic assertion mode %0d", f2a_assert_mode_q);
      end
    end
  end

  always @(posedge clk_i) begin : f2a_diagnostic_out_of_bounds_read
    if (rst_ni && f2a_oob_read_first_event) begin
      if (f2a_assert_mode_q == F2A_ASSERT_OBSERVE) begin
        f2a_emit_out_of_bounds_read_event("RECORD_ONLY");
      end else if (
        f2a_assert_mode_q == F2A_ASSERT_DIAGNOSTIC_QUARANTINE
      ) begin
        f2a_emit_out_of_bounds_read_event("RECORD_AND_RETURN_ZERO");
      end else begin
        $fatal(2, "invalid diagnostic assertion mode %0d", f2a_assert_mode_q);
      end
    end
  end
`endif
'''


class PreparationError(RuntimeError):
    """Controlled diagnostic-overlay generation failure."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def replace_exact(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise PreparationError(f"expected exactly one {label}; found {count}")
    return text.replace(old, new, 1)


def validate_policy(policy: dict[str, Any]) -> None:
    if policy.get("schema_version") != "1.0":
        raise PreparationError("unsupported assertion policy schema")

    expected_modes = ["native", "observe", "diagnostic_quarantine"]
    if policy.get("supported_modes") != expected_modes:
        raise PreparationError(
            f"assertion policy modes must be exactly {expected_modes}"
        )

    mode_contracts = policy.get("mode_contracts")
    if not isinstance(mode_contracts, dict) or set(mode_contracts) != set(
        expected_modes
    ):
        raise PreparationError("assertion policy mode contracts are incomplete")

    registry = policy.get("detectors")
    if not isinstance(registry, list):
        raise PreparationError("assertion policy detectors must be an array")

    by_id = {
        item.get("detector_id"): item
        for item in registry
        if isinstance(item, dict)
    }
    expected = {
        "cv32e40p.mm_ram.out_of_bounds_write": (
            "ILLEGAL_MEMORY_WRITE",
            "ACKNOWLEDGE_AND_DROP_WRITE",
        ),
        "cv32e40p.mm_ram.out_of_bounds_read": (
            "ILLEGAL_MEMORY_READ",
            "RETURN_ZERO_AND_CONTINUE",
        ),
    }
    if set(by_id) != set(expected):
        raise PreparationError(
            "Phase-2 policy must register exactly the mm_ram read and write detectors"
        )

    for detector_id, (effect, action) in expected.items():
        detector = by_id[detector_id]
        if detector.get("effect_hint") != effect:
            raise PreparationError(f"{detector_id} effect hint mismatch")
        if detector.get("quarantine_action") != action:
            raise PreparationError(f"{detector_id} quarantine action mismatch")
        if detector.get("diagnostic_adapter") != "MM_RAM_STAGE5_OVERLAY_V2":
            raise PreparationError(f"{detector_id} diagnostic adapter mismatch")


def validate_output(text: str) -> dict[str, Any]:
    required = {
        "adapter marker": MARKER,
        "observe mode": "F2A_ASSERT_OBSERVE",
        "quarantine mode": "F2A_ASSERT_DIAGNOSTIC_QUARANTINE",
        "mode owner": "begin : f2a_assertion_mode_init",
        "state owner": "begin : f2a_assertion_state",
        "predicate owner": "begin : f2a_assertion_predicates",
        "write detector": "begin : f2a_diagnostic_out_of_bounds_write",
        "read detector": "begin : f2a_diagnostic_out_of_bounds_read",
        "write first-event predicate": "f2a_oob_write_first_event",
        "read first-event predicate": "f2a_oob_read_first_event",
        "first-event state": "f2a_detector_seen_q",
        "structured header": "H\\tF2A_ASSERT_EVENTS",
        "explicit TSV action separator":
            '$fdisplay(f2a_assert_event_fd, "\\t%s", action_name);',
        "write effect": "ILLEGAL_MEMORY_WRITE",
        "read effect": "ILLEGAL_MEMORY_READ",
        "write quarantine grant": "perip_gnt   = 1'b1;",
        "write quarantine transaction": "transaction = T_PER;",
        "read quarantine action": "RECORD_AND_RETURN_ZERO",
        "surrogate marker": "F2A_DETECTOR_IMPLEMENTATION\\tPROCEDURAL_FIRST_EVENT",
    }
    missing = [label for label, token in required.items() if token not in text]
    if missing:
        raise PreparationError("prepared source missing: " + ", ".join(missing))

    if ASSERTION_OLD in text or "out_of_bounds_write :" in text:
        raise PreparationError("original out_of_bounds_write assertion remains")
    if READ_FATAL_OLD in text:
        raise PreparationError("original out-of-bounds read fatal remains")
    if text.count(MARKER) != 1:
        raise PreparationError("prepared source must contain one adapter marker")
    if text.count('"f2a_assert_mode=%s"') != 1:
        raise PreparationError("prepared source must contain one mode reader")
    if text.count("begin : f2a_assertion_state") != 1:
        raise PreparationError("prepared source must contain one state owner")
    if text.count("begin : f2a_diagnostic_out_of_bounds_write") != 1:
        raise PreparationError("prepared source must contain one write detector")
    if text.count("begin : f2a_diagnostic_out_of_bounds_read") != 1:
        raise PreparationError("prepared source must contain one read detector")
    if text.count("task automatic f2a_emit_out_of_bounds_write_event") != 1:
        raise PreparationError("prepared source must contain one write event task")
    if text.count("task automatic f2a_emit_out_of_bounds_read_event") != 1:
        raise PreparationError("prepared source must contain one read event task")

    return {
        "adapter_marker_count": 1,
        "supported_modes": ["native", "observe", "diagnostic_quarantine"],
        "detectors": ["out_of_bounds_write", "out_of_bounds_read"],
        "original_write_assertion_removed": True,
        "original_read_fatal_removed": True,
        "diagnostic_detector_implementation": "PROCEDURAL_FIRST_EVENT",
        "first_event_policy": FIRST_EVENT_POLICY,
        "mode_reader_count": 1,
        "state_owner_count": 1,
        "procedural_detector_count": 2,
        "event_task_count": 2,
        "write_quarantine_action": "ACKNOWLEDGE_AND_DROP_WRITE",
        "read_quarantine_action": "RETURN_ZERO_AND_CONTINUE",
        "source_profile": "diagnostic",
    }


def build_overlay(source: Path, policy_path: Path) -> tuple[str, dict[str, Any]]:
    if not source.is_file() or source.stat().st_size == 0:
        raise PreparationError(f"source not found or empty: {source}")
    if not policy_path.is_file() or policy_path.stat().st_size == 0:
        raise PreparationError(f"assertion policy not found or empty: {policy_path}")

    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PreparationError(f"invalid assertion policy: {exc}") from exc
    if not isinstance(policy, dict):
        raise PreparationError("assertion policy must contain one JSON object")
    validate_policy(policy)

    text = source.read_text(encoding="utf-8", errors="strict")
    if MARKER in text:
        raise PreparationError("source already contains the diagnostic adapter")

    text = replace_exact(
        text,
        DECLARATION_ANCHOR,
        DECLARATION_ANCHOR + DECLARATIONS,
        "declaration anchor",
    )
    text = replace_exact(text, WRITE_BRANCH_OLD, WRITE_BRANCH_NEW, "write branch")
    text = replace_exact(
        text,
        ASSERTION_OLD,
        DIAGNOSTIC_DETECTOR_BLOCK,
        "original out_of_bounds_write assertion block",
    )
    text = replace_exact(
        text,
        READ_FATAL_OLD,
        READ_FATAL_NEW,
        "out-of-bounds read fatal branch",
    )

    return text, validate_output(text)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {PROGRAM_VERSION}"
    )
    args = parser.parse_args(argv)

    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    report = args.report.expanduser().resolve()
    policy_path = args.policy.expanduser().resolve()

    if output == source:
        raise PreparationError("run-local output must not overwrite source")
    if output.exists():
        raise PreparationError(f"refusing to overwrite existing output: {output}")
    if report.exists():
        raise PreparationError(f"refusing to overwrite existing report: {report}")

    text, validation = build_overlay(source, policy_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")

    payload = {
        "schema_version": "1.0",
        "program_version": PROGRAM_VERSION,
        "kind": "stage5_diagnostic_mm_ram_preparation",
        "source_profile": "diagnostic",
        "source": str(source),
        "source_sha256": sha256_file(source),
        "assertion_policy": str(policy_path),
        "assertion_policy_sha256": sha256_file(policy_path),
        "output": str(output),
        "output_sha256": sha256_file(output),
        "transformation_count": 1,
        "transformations": [
            "insert_mode_state_and_event_writers",
            "insert_quarantine_write_branch",
            "remove_original_out_of_bounds_write_assertion",
            "remove_original_out_of_bounds_read_fatal",
            "insert_write_and_read_first_event_detectors",
        ],
        "original_write_assertion_removed": True,
        "original_read_fatal_removed": True,
        "diagnostic_detector_implementation": "PROCEDURAL_FIRST_EVENT",
        "first_event_policy": FIRST_EVENT_POLICY,
        "validation": validation,
        "external_source_modified": False,
    }

    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print(f"Source mm_ram       : {source}")
    print(f"Diagnostic overlay : {output}")
    print(f"Overlay SHA-256     : {payload['output_sha256']}")
    print("Write assertion     : REMOVED FROM DIAGNOSTIC OVERLAY")
    print("Read fatal          : REMOVED FROM DIAGNOSTIC OVERLAY")
    print("Diagnostic detectors: WRITE + READ PROCEDURAL_FIRST_EVENT")
    print("Preparation result  : PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PreparationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
