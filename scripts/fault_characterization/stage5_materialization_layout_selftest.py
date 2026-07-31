#!/usr/bin/env python3
"""Synthetic regression tests for the Stage-5 v1.0.8 layout correction."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any, Sequence


class SelfTestError(RuntimeError):
    pass


def import_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SelfTestError(f"cannot import module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage5-tool", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tool_path = args.stage5_tool.resolve()
    tool = import_module(tool_path, "f2a_stage5_v108_layout_selftest_target")

    if str(tool.PROGRAM_VERSION) != "1.0.8":
        raise SelfTestError(
            f"expected Stage-5 1.0.8, got {tool.PROGRAM_VERSION!r}"
        )

    source = """module demo(input wire a, output wire y);
  wire n;
  BUF_X1 u_driver (.A(a), .Z(n));
  assign y = n;
endmodule
"""
    connection_start = source.index("n));")
    connection_end = connection_start + 1
    temporary = "f2a_pre_tf000001_sa0"

    modified, facts = tool.build_fault_netlist_text(
        text=source,
        module_name="demo",
        source_net="n",
        stuck_at=0,
        temporary_net=temporary,
        fault_id="TF000001_SA0",
        connection_start=connection_start,
        connection_end=connection_end,
        replacement_connection=temporary,
        expected_original_connection="n",
    )

    declaration = f"wire {temporary};"
    use = f".Z({temporary})"
    assignment = "assign n = 1'b0;"
    if not (modified.index(declaration) < modified.index(use) < modified.index(assignment)):
        raise SelfTestError("corrected declaration/use/assignment order is wrong")
    if facts["temporary_net_occurrence_count"] != 2:
        raise SelfTestError("temporary-net occurrence count is not exactly two")

    # Reproduce the old invalid ordering and require the validator to reject it.
    invalid = """module demo(input wire a, output wire y);
  wire n;
  BUF_X1 u_driver (.A(a), .Z(f2a_pre_tf000001_sa0));
  assign y = n;
  wire f2a_pre_tf000001_sa0;
  assign n = 1'b0;
endmodule
"""
    try:
        tool.validate_materialized_netlist_text(
            invalid,
            module_name="demo",
            source_net="n",
            stuck_at=0,
            temporary_net=temporary,
            fault_id="TF000001_SA0",
        )
    except tool.Stage5Error:
        invalid_rejected = True
    else:
        invalid_rejected = False
    if not invalid_rejected:
        raise SelfTestError("old use-before-declaration layout was accepted")

    # A second explicit declaration must also be rejected.
    duplicate = modified.replace(declaration, declaration + "\n  " + declaration)
    try:
        tool.validate_materialized_netlist_text(
            duplicate,
            module_name="demo",
            source_net="n",
            stuck_at=0,
            temporary_net=temporary,
            fault_id="TF000001_SA0",
        )
    except tool.Stage5Error:
        duplicate_rejected = True
    else:
        duplicate_rejected = False
    if not duplicate_rejected:
        raise SelfTestError("duplicate temporary-net declaration was accepted")

    print("Stage-5 tool version             : 1.0.8")
    print("Declaration precedes first use   : PASS")
    print("Assignment follows driver use    : PASS")
    print("Old invalid layout rejected      : PASS")
    print("Duplicate declaration rejected   : PASS")
    print("Materialization layout self-test : PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
