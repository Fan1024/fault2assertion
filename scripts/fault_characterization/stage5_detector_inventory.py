#!/usr/bin/env python3
"""Inventory pre-existing assertion and terminal points used by Stage-5.

The inventory does not change source code.  It records named concurrent
assertions and procedural terminal calls, maps registered detectors from the
Stage-5 assertion policy, and explicitly states whether the current detector
registry is ready for a one-fault smoke or for an unrestricted full campaign.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

PROGRAM_VERSION = "1.0.0"
ASSERTION_RE = re.compile(
    r"(?m)^\s*(?P<label>[A-Za-z_][A-Za-z0-9_$]*)\s*:\s*\n\s*assert\s+property\b"
)
TERMINAL_RE = re.compile(r"\$(?P<kind>fatal|error|finish)\s*\(", re.IGNORECASE)


class InventoryError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def classify_terminal(path: Path, line: str, associated_assertion: str | None) -> tuple[str, bool]:
    normalized = str(path).replace("\\", "/").lower()
    lowered = line.lower()
    if associated_assertion == "out_of_bounds_write" and normalized.endswith("/verification/shared/tb/mm_ram.sv"):
        return "REGISTERED_MODE_AWARE_DETECTOR_ACTION", False
    if "simulation aborted due to maximum cycle limit" in lowered:
        return "WATCHDOG_TERMINAL", False
    if "invalid instr_rdata_width" in lowered:
        return "CONFIGURATION_INVARIANT", False
    if "out of bounds read" in lowered or "out of bounds write" in lowered:
        return "UNREGISTERED_FAULT_RELEVANT_TERMINAL", True
    if "$finish" in lowered:
        return "WORKLOAD_OR_TESTBENCH_COMPLETION", False
    return "UNCLASSIFIED_TERMINAL", True


def scan_source(path: Path, registered_names: set[str]) -> dict[str, Any]:
    path = path.resolve()
    if not path.is_file() or path.stat().st_size == 0:
        raise InventoryError(f"source missing or empty: {path}")
    text = path.read_text(encoding="utf-8", errors="strict")
    lines = text.splitlines()
    assertions: list[dict[str, Any]] = []
    assertion_offsets: list[tuple[int, str]] = []
    for match in ASSERTION_RE.finditer(text):
        label = match.group("label")
        offset = match.start()
        assertion_offsets.append((offset, label))
        assertions.append(
            {
                "label": label,
                "line": line_number(text, offset),
                "registered_mode_aware": label in registered_names,
            }
        )

    terminals: list[dict[str, Any]] = []
    for match in TERMINAL_RE.finditer(text):
        offset = match.start()
        line_no = line_number(text, offset)
        associated: str | None = None
        preceding = [(position, label) for position, label in assertion_offsets if position < offset]
        if preceding:
            position, label = max(preceding)
            if line_no - line_number(text, position) <= 40:
                associated = label
        source_line = lines[line_no - 1].strip() if line_no <= len(lines) else ""
        category, blocks_full = classify_terminal(path, source_line, associated)
        terminals.append(
            {
                "kind": match.group("kind").upper(),
                "line": line_no,
                "source_text": source_line,
                "associated_assertion": associated,
                "category": category,
                "blocks_unrestricted_full_campaign": blocks_full,
            }
        )
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "named_assertions": assertions,
        "terminal_calls": terminals,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--source", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        policy_path = args.policy.resolve()
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        detectors = policy.get("detectors")
        if not isinstance(detectors, list) or not detectors:
            raise InventoryError("assertion policy contains no detectors")
        registered_names = {str(item.get("assertion_leaf_name")) for item in detectors}
        sources = [scan_source(path, registered_names) for path in args.source]
        found_registered = {
            assertion["label"]
            for source in sources
            for assertion in source["named_assertions"]
            if assertion["registered_mode_aware"]
        }
        missing_registered = sorted(registered_names - found_registered)
        blockers = [
            {"path": source["path"], **terminal}
            for source in sources
            for terminal in source["terminal_calls"]
            if terminal["blocks_unrestricted_full_campaign"]
        ]
        smoke_ready = not missing_registered
        payload = {
            "schema_version": "1.0",
            "program_version": PROGRAM_VERSION,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "kind": "stage5_preexisting_detector_inventory",
            "status": "PASS" if smoke_ready else "FAIL",
            "assertion_policy": str(policy_path),
            "assertion_policy_sha256": sha256_file(policy_path),
            "registered_detector_names": sorted(registered_names),
            "found_registered_detector_names": sorted(found_registered),
            "missing_registered_detector_names": missing_registered,
            "sources": sources,
            "unregistered_or_unclassified_terminal_points": blockers,
            "readiness": {
                "known_out_of_bounds_write_smoke": smoke_ready,
                "unrestricted_full_campaign": smoke_ready and not blockers,
            },
            "guardrails": {
                "inventory_does_not_modify_sources": True,
                "unregistered_terminal_points_fail_closed_during_campaign_expansion": True,
                "watchdog_and_configuration_invariants_are_not_fault_effect_detectors": True,
            },
        }
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError, InventoryError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"Registered detectors found : {len(found_registered)}")
    print(f"Expansion blockers         : {len(blockers)}")
    print(f"Smoke ready                : {payload['readiness']['known_out_of_bounds_write_smoke']}")
    print(f"Full campaign ready        : {payload['readiness']['unrestricted_full_campaign']}")
    print(f"Inventory                  : {output}")
    return 0 if smoke_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
