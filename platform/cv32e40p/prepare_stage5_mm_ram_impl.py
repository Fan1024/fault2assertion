#!/usr/bin/env python3
"""Generate one run-local Stage-5 diagnostic mm_ram overlay.

The immutable CV32E40P ``mm_ram.sv`` source is never modified.

Native execution does not use this file.  It compiles the original
``mm_ram.sv`` and therefore preserves the original ``out_of_bounds_write``
concurrent assertion and its ``$fatal`` action exactly.

For diagnostic OBSERVE and QUARANTINE execution, this generator creates one
run-local overlay by performing three exact, fail-closed substitutions:

1. insert one mode/configuration subsystem, one first-event state owner, and
   one structured event writer;
2. add quarantine behavior only to the existing out-of-bounds write branch;
3. remove the original ``out_of_bounds_write`` assertion block and replace it
   with a same-clock procedural first-event detector using the same address
   legality boundary.

The diagnostic overlay contains no replacement SVA for this detector.  It is
not an AI-generated assertion and is never used as the native baseline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Sequence

PROGRAM_VERSION = "3.0.0"
MARKER = "Fault2Assertion Stage-5 diagnostic detector adapter"
FIRST_EVENT_POLICY = "FIRST_VIOLATION_ONLY"

DECLARATION_ANCHOR = "  logic [31:0] error_addr_q;\n"

WRITE_BRANCH_OLD = """        end else begin
          // out of bounds write
        end
"""

WRITE_BRANCH_NEW = """        end else begin
          // out of bounds write
          // Diagnostic quarantine acknowledges and drops only this unsafe
          // transaction.  Observe preserves the original no-grant behavior.
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

  always_comb begin : f2a_assertion_predicates
    // This is the same address-allow list used by the removed pre-existing
    // out_of_bounds_write assertion.  Unknown addresses are treated as not
    // demonstrably allowed and therefore remain fail-closed for diagnostics.
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

    f2a_oob_write_first_event =
      f2a_oob_write_violation && !f2a_detector_seen_q;
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

      if (f2a_oob_write_first_event) begin
        f2a_assert_event_count_q <= f2a_assert_event_count_q + 1;
        f2a_detector_seen_q <= 1'b1;
      end
    end
  end

  task automatic f2a_emit_out_of_bounds_write_event(
    input string action_name
  );
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

DIAGNOSTIC_DETECTOR_BLOCK = r'''`ifndef VERILATOR
  // The original out_of_bounds_write concurrent assertion is intentionally
  // absent from this diagnostic overlay.  Native runs compile the immutable
  // original mm_ram.sv and preserve that assertion and its $fatal action.
  //
  // This procedural surrogate samples the same request/address boundary at
  // posedge clk_i, honors reset, and emits only the first violation event.
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

    expected_modes = [
        "native",
        "observe",
        "diagnostic_quarantine",
    ]
    if policy.get("supported_modes") != expected_modes:
        raise PreparationError(
            f"assertion policy modes must be exactly {expected_modes}"
        )

    mode_contracts = policy.get("mode_contracts")
    if (
        not isinstance(mode_contracts, dict)
        or set(mode_contracts) != set(expected_modes)
    ):
        raise PreparationError("assertion policy mode contracts are incomplete")

    registry = policy.get("detectors")
    if not isinstance(registry, list) or len(registry) != 1:
        raise PreparationError(
            "Phase-2 policy must register exactly one detector"
        )

    detector = registry[0]
    if (
        detector.get("detector_id")
        != "cv32e40p.mm_ram.out_of_bounds_write"
    ):
        raise PreparationError("unexpected detector registry entry")
    if detector.get("effect_hint") != "ILLEGAL_MEMORY_WRITE":
        raise PreparationError("detector effect hint mismatch")
    if (
        detector.get("quarantine_action")
        != "ACKNOWLEDGE_AND_DROP_WRITE"
    ):
        raise PreparationError("detector quarantine action mismatch")


def validate_output(text: str) -> dict[str, Any]:
    required = {
        "adapter marker": MARKER,
        "observe mode": "F2A_ASSERT_OBSERVE",
        "quarantine mode": "F2A_ASSERT_DIAGNOSTIC_QUARANTINE",
        "mode owner": "begin : f2a_assertion_mode_init",
        "state owner": "begin : f2a_assertion_state",
        "predicate owner": "begin : f2a_assertion_predicates",
        "procedural detector":
            "begin : f2a_diagnostic_out_of_bounds_write",
        "first-event predicate": "f2a_oob_write_first_event",
        "first-event state": "f2a_detector_seen_q",
        "structured header": "H\\tF2A_ASSERT_EVENTS",
        "structured event": "A\\t%0d\\t%0d",
        "effect hint": "ILLEGAL_MEMORY_WRITE",
        "quarantine grant": "perip_gnt   = 1'b1;",
        "quarantine transaction": "transaction = T_PER;",
        "observe action": "RECORD_ONLY",
        "quarantine action": "RECORD_AND_QUARANTINE",
        "surrogate marker":
            "F2A_DETECTOR_IMPLEMENTATION\\tPROCEDURAL_FIRST_EVENT",
    }

    missing = [
        label
        for label, token in required.items()
        if token not in text
    ]
    if missing:
        raise PreparationError(
            "prepared source missing: " + ", ".join(missing)
        )

    if ASSERTION_OLD in text:
        raise PreparationError("original assertion block remains verbatim")
    if "out_of_bounds_write :" in text:
        raise PreparationError(
            "diagnostic overlay must not contain the original named assertion"
        )
    if "assert property" in DIAGNOSTIC_DETECTOR_BLOCK:
        raise PreparationError(
            "diagnostic detector block unexpectedly contains SVA"
        )
    if text.count(MARKER) != 1:
        raise PreparationError(
            "prepared source must contain one diagnostic adapter marker"
        )
    if text.count('"f2a_assert_mode=%s"') != 1:
        raise PreparationError(
            "prepared source must contain one mode reader"
        )
    if text.count("begin : f2a_assertion_state") != 1:
        raise PreparationError(
            "prepared source must contain one state owner"
        )
    if text.count("begin : f2a_diagnostic_out_of_bounds_write") != 1:
        raise PreparationError(
            "prepared source must contain one procedural detector"
        )
    if (
        text.count(
            "task automatic f2a_emit_out_of_bounds_write_event"
        )
        != 1
    ):
        raise PreparationError(
            "prepared source must contain one event task"
        )

    return {
        "adapter_marker_count": 1,
        "supported_modes": [
            "native",
            "observe",
            "diagnostic_quarantine",
        ],
        "detector": "out_of_bounds_write",
        "original_assertion_block_removed": True,
        "diagnostic_detector_implementation":
            "PROCEDURAL_FIRST_EVENT",
        "first_event_policy": FIRST_EVENT_POLICY,
        "mode_reader_count": 1,
        "state_owner_count": 1,
        "procedural_detector_count": 1,
        "event_task_count": 1,
        "quarantine_action": "ACKNOWLEDGE_AND_DROP_WRITE",
        "source_profile": "diagnostic",
    }


def build_overlay(
    source: Path,
    policy_path: Path,
) -> tuple[str, dict[str, Any]]:
    if not source.is_file() or source.stat().st_size == 0:
        raise PreparationError(f"source not found or empty: {source}")
    if (
        not policy_path.is_file()
        or policy_path.stat().st_size == 0
    ):
        raise PreparationError(
            f"assertion policy not found or empty: {policy_path}"
        )

    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PreparationError(
            f"invalid assertion policy: {exc}"
        ) from exc
    if not isinstance(policy, dict):
        raise PreparationError(
            "assertion policy must contain one JSON object"
        )
    validate_policy(policy)

    text = source.read_text(encoding="utf-8", errors="strict")
    if MARKER in text:
        raise PreparationError(
            "source already contains the Stage-5 diagnostic adapter"
        )

    text = replace_exact(
        text,
        DECLARATION_ANCHOR,
        DECLARATION_ANCHOR + DECLARATIONS,
        "declaration anchor",
    )
    text = replace_exact(
        text,
        WRITE_BRANCH_OLD,
        WRITE_BRANCH_NEW,
        "write branch",
    )
    text = replace_exact(
        text,
        ASSERTION_OLD,
        DIAGNOSTIC_DETECTOR_BLOCK,
        "original out_of_bounds_write assertion block",
    )

    validation = validate_output(text)
    return text, validation


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {PROGRAM_VERSION}",
    )
    args = parser.parse_args(argv)

    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    report = args.report.expanduser().resolve()
    policy_path = args.policy.expanduser().resolve()

    if output == source:
        raise PreparationError(
            "run-local output must not overwrite source"
        )
    if output.exists():
        raise PreparationError(
            f"refusing to overwrite existing output: {output}"
        )
    if report.exists():
        raise PreparationError(
            f"refusing to overwrite existing report: {report}"
        )

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
            "insert_mode_state_and_event_writer",
            "insert_quarantine_write_branch",
            "remove_original_out_of_bounds_assertion_block",
            "insert_procedural_first_event_detector",
        ],
        "original_assertion_block_removed": True,
        "diagnostic_detector_implementation":
            "PROCEDURAL_FIRST_EVENT",
        "first_event_policy": FIRST_EVENT_POLICY,
        "validation": validation,
        "external_source_modified": False,
    }

    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Source mm_ram       : {source}")
    print(f"Diagnostic overlay : {output}")
    print(f"Overlay SHA-256     : {payload['output_sha256']}")
    print("Original assertion  : REMOVED FROM DIAGNOSTIC OVERLAY")
    print("Diagnostic detector : PROCEDURAL_FIRST_EVENT")
    print("Transformations     : 1 generation pass")
    print("Preparation result  : PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PreparationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
