#!/usr/bin/env python3
"""Generate a run-local Stage-5 mode-aware copy of CV32E40P mm_ram.sv.

The immutable CV32E40P source is never modified.  This tool performs three
strictly validated structural substitutions:

* adds the Stage-5 assertion-mode state and structured event writer;
* adds quarantine behavior only to the existing out-of-bounds write branch;
* replaces the original fatal-only assertion with a three-mode adapter.

The adapter preserves the native assertion semantics.  Observe mode suppresses
only the fatal action.  Diagnostic-quarantine mode additionally acknowledges
and drops the unsafe write so trace collection can continue.  Both diagnostic
modes are explicitly counterfactual after the first detector event.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

PROGRAM_VERSION = "1.0.0"
MARKER = "Fault2Assertion Stage-5 assertion-mode adapter"

DECLARATION_ANCHOR = "  logic [31:0] error_addr_q;\n"
WRITE_BRANCH_OLD = """        end else begin
          // out of bounds write
        end
"""
WRITE_BRANCH_NEW = """        end else begin
          // out of bounds write
          // In diagnostic_quarantine mode, acknowledge and drop the unsafe
          // transaction.  Native and observe retain the original no-grant
          // behavior of this memory model.
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

DECLARATIONS = r'''

  // Fault2Assertion Stage-5 assertion-mode adapter.
  localparam int unsigned F2A_ASSERT_NATIVE = 0;
  localparam int unsigned F2A_ASSERT_OBSERVE = 1;
  localparam int unsigned F2A_ASSERT_DIAGNOSTIC_QUARANTINE = 2;

  int unsigned     f2a_assert_mode_q = F2A_ASSERT_NATIVE;
  string           f2a_assert_mode_name = "native";
  string           f2a_assert_event_file;
  integer          f2a_assert_event_fd = 0;
  longint unsigned f2a_cycle_q = 0;
  longint unsigned f2a_assert_event_count_q = 0;
  logic            f2a_oob_write_violation_q;

  logic f2a_write_address_allowed;
  logic f2a_oob_write_violation;
  logic f2a_oob_write_violation_start;

  always_comb begin
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
    f2a_oob_write_violation_start =
      f2a_oob_write_violation && !f2a_oob_write_violation_q;
  end

  initial begin : f2a_assertion_mode_init
    string requested_mode;
    requested_mode = "native";
    void'($value$plusargs("f2a_assert_mode=%s", requested_mode));
    case (requested_mode)
      "native": begin
        f2a_assert_mode_q = F2A_ASSERT_NATIVE;
        f2a_assert_mode_name = "native";
      end
      "observe": begin
        f2a_assert_mode_q = F2A_ASSERT_OBSERVE;
        f2a_assert_mode_name = "observe";
      end
      "diagnostic_quarantine": begin
        f2a_assert_mode_q = F2A_ASSERT_DIAGNOSTIC_QUARANTINE;
        f2a_assert_mode_name = "diagnostic_quarantine";
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
      $fatal(2, "cannot open Stage-5 assertion event file %s", f2a_assert_event_file);
    end
    $fdisplay(
      f2a_assert_event_fd,
      "H\tF2A_ASSERT_EVENTS\t1\t%s",
      f2a_assert_mode_name
    );
    $fflush(f2a_assert_event_fd);
    $display("F2A_ASSERT_MODE\t%s", f2a_assert_mode_name);
  end

  always_ff @(posedge clk_i or negedge rst_ni) begin : f2a_assertion_state
    if (!rst_ni) begin
      f2a_cycle_q <= 0;
      f2a_oob_write_violation_q <= 1'b0;
    end else begin
      f2a_cycle_q <= f2a_cycle_q + 1;
      f2a_oob_write_violation_q <= f2a_oob_write_violation;
    end
  end

  task automatic f2a_emit_out_of_bounds_write_event(input string action_name);
    $fdisplay(
      f2a_assert_event_fd,
      "A\t%0d\t%0d\t%0t\tPREEXISTING_TB_ASSERTION\tout_of_bounds_write\tILLEGAL_MEMORY_WRITE\t%08x\t%08x\t%01x\t%s",
      f2a_assert_event_count_q,
      f2a_cycle_q,
      $time,
      data_addr_i,
      data_wdata_i,
      data_be_i,
      action_name
    );
    $fflush(f2a_assert_event_fd);
    f2a_assert_event_count_q = f2a_assert_event_count_q + 1;
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
'''

ASSERTION_NEW = r'''`ifndef VERILATOR
  // The original assertion remains active in native mode.  The two diagnostic
  // modes use the identical violation predicate but suppress fatal termination.
  out_of_bounds_write :
  assert property
    (@(posedge clk_i) disable iff (~rst_ni)
     (f2a_assert_mode_q != F2A_ASSERT_NATIVE)
     || !f2a_oob_write_violation_start)
  else begin
    f2a_emit_out_of_bounds_write_event("FATAL_TERMINATION");
    $fatal("out of bounds write to %08x with %08x", data_addr_i, data_wdata_i);
  end

  always @(posedge clk_i) begin : f2a_diagnostic_out_of_bounds_write
    if (rst_ni && f2a_oob_write_violation_start
        && f2a_assert_mode_q != F2A_ASSERT_NATIVE) begin
      if (f2a_assert_mode_q == F2A_ASSERT_OBSERVE) begin
        f2a_emit_out_of_bounds_write_event("RECORD_ONLY");
      end else begin
        f2a_emit_out_of_bounds_write_event("RECORD_AND_QUARANTINE");
      end
    end
  end
`endif
'''


class PreparationError(RuntimeError):
    pass


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


def validate_output(text: str) -> dict[str, Any]:
    required = {
        "adapter marker": MARKER,
        "native mode": "F2A_ASSERT_NATIVE",
        "observe mode": "F2A_ASSERT_OBSERVE",
        "quarantine mode": "F2A_ASSERT_DIAGNOSTIC_QUARANTINE",
        "structured header": "H\\tF2A_ASSERT_EVENTS",
        "structured event": "A\\t%0d\\t%0d",
        "effect hint": "ILLEGAL_MEMORY_WRITE",
        "quarantine grant": "perip_gnt   = 1'b1;",
        "native assertion": "out_of_bounds_write :",
        "fatal action": "FATAL_TERMINATION",
        "observe action": "RECORD_ONLY",
        "quarantine action": "RECORD_AND_QUARANTINE",
    }
    missing = [label for label, token in required.items() if token not in text]
    if missing:
        raise PreparationError("prepared source missing: " + ", ".join(missing))
    if ASSERTION_OLD in text:
        raise PreparationError("original fatal-only assertion block remains")
    if text.count("out_of_bounds_write :") != 1:
        raise PreparationError("prepared source must contain one named assertion")
    return {
        "adapter_marker_present": True,
        "supported_modes": ["native", "observe", "diagnostic_quarantine"],
        "detector": "out_of_bounds_write",
        "quarantine_action": "ACKNOWLEDGE_AND_DROP_WRITE",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    source = args.source.resolve()
    output = args.output.resolve()
    report = args.report.resolve()
    policy_path = args.policy.resolve()

    if not policy_path.is_file():
        raise PreparationError(f"assertion policy not found: {policy_path}")
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PreparationError(f"invalid assertion policy: {exc}") from exc
    if policy.get("schema_version") != "1.0":
        raise PreparationError("unsupported assertion policy schema")
    supported_modes = policy.get("supported_modes")
    if supported_modes != ["native", "observe", "diagnostic_quarantine"]:
        raise PreparationError("assertion policy must define the ordered three supported modes")
    mode_contracts = policy.get("mode_contracts")
    if not isinstance(mode_contracts, dict) or set(mode_contracts) != set(supported_modes):
        raise PreparationError("assertion policy mode contracts are incomplete")
    registry = policy.get("detectors")
    if not isinstance(registry, list) or len(registry) != 1:
        raise PreparationError("Phase-2 smoke policy must register exactly one detector")
    detector = registry[0]
    if detector.get("detector_id") != "cv32e40p.mm_ram.out_of_bounds_write":
        raise PreparationError("unexpected detector registry entry")
    if detector.get("effect_hint") != "ILLEGAL_MEMORY_WRITE":
        raise PreparationError("detector effect-class mismatch")
    if detector.get("quarantine_action") != "ACKNOWLEDGE_AND_DROP_WRITE":
        raise PreparationError("detector quarantine-action mismatch")

    if not source.is_file():
        raise PreparationError(f"source not found: {source}")
    if output == source:
        raise PreparationError("run-local output must not overwrite source")
    text = source.read_text(encoding="utf-8", errors="strict")
    if MARKER in text:
        raise PreparationError("source already contains Stage-5 adapter")

    text = replace_exact(
        text,
        DECLARATION_ANCHOR,
        DECLARATION_ANCHOR + DECLARATIONS,
        "declaration anchor",
    )
    text = replace_exact(text, WRITE_BRANCH_OLD, WRITE_BRANCH_NEW, "write branch")
    text = replace_exact(text, ASSERTION_OLD, ASSERTION_NEW, "assertion block")
    validation = validate_output(text)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    payload = {
        "schema_version": "1.0",
        "program_version": PROGRAM_VERSION,
        "kind": "stage5_mode_aware_mm_ram_preparation",
        "source": str(source),
        "source_sha256": sha256_file(source),
        "assertion_policy": str(policy_path),
        "assertion_policy_sha256": sha256_file(policy_path),
        "output": str(output),
        "output_sha256": sha256_file(output),
        "validation": validation,
        "source_modified": False,
    }
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Source mm_ram      : {source}")
    print(f"Prepared mm_ram    : {output}")
    print(f"Prepared SHA-256   : {payload['output_sha256']}")
    print("Assertion modes    : native, observe, diagnostic_quarantine")
    print("Preparation result : PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PreparationError as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)
