#!/usr/bin/env python3
"""Independent in-memory tests for Stage-5 digest validation logic."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import sys
from pathlib import Path
from typing import Any, Sequence


def import_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path.resolve())
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage5-tool", type=Path, required=True)
    parser.add_argument("--version-guard", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    stage5 = import_module(args.stage5_tool, "f2a_stage5_selftest_target")
    guard = import_module(args.version_guard, "f2a_stage5_guard_selftest_target")

    base_spec = {
        "schema_version": stage5.SCHEMA_VERSION,
        "program_version": stage5.PROGRAM_VERSION,
        "generated_at_utc": "2026-01-01T00:00:00+00:00",
        "stage": stage5.STAGE5_FAULT_MARKER,
        "fault_id": "TF000001_SA0",
        "selection_id": "TS000001",
        "stuck_at": 0,
        "site": {"module": "m", "source_net": "n"},
        "receiver_signals": [{"expression": "q"}],
    }

    # Mirror the generator: the stored digest field is not present yet.
    generated = copy.deepcopy(base_spec)
    generated["fault_spec_digest_sha256"] = stage5.canonical_json_digest(
        {
            key: value
            for key, value in generated.items()
            if key != "generated_at_utc"
        }
    )

    if guard.compute_fault_spec_digest(generated) != generated[
        "fault_spec_digest_sha256"
    ]:
        raise RuntimeError("valid generated fault spec was rejected")

    timestamp_only = copy.deepcopy(generated)
    timestamp_only["generated_at_utc"] = "2099-12-31T23:59:59+00:00"
    if guard.compute_fault_spec_digest(timestamp_only) != generated[
        "fault_spec_digest_sha256"
    ]:
        raise RuntimeError("generated_at_utc incorrectly affects fault digest")

    payload_mutation = copy.deepcopy(generated)
    payload_mutation["stuck_at"] = 1
    if guard.compute_fault_spec_digest(payload_mutation) == generated[
        "fault_spec_digest_sha256"
    ]:
        raise RuntimeError("payload mutation was not detected")

    digest_mutation = copy.deepcopy(generated)
    digest_mutation["fault_spec_digest_sha256"] = "0" * 64
    recomputed = guard.compute_fault_spec_digest(digest_mutation)
    if recomputed != generated["fault_spec_digest_sha256"]:
        raise RuntimeError("stored digest field incorrectly affects recomputation")
    if digest_mutation["fault_spec_digest_sha256"] == recomputed:
        raise RuntimeError("stored digest corruption was not detectable")

    campaign = {
        "source_stage4": {"selection_sha256": "a" * 64},
        "mapped_netlist": {"sha256": "b" * 64},
        "selected_sites": [{"selection_id": "TS000001"}],
        "faults": [{"fault_id": "TF000001_SA0"}],
    }
    campaign["campaign_digest_sha256"] = stage5.canonical_json_digest(
        {
            "source_stage4": campaign["source_stage4"],
            "mapped_netlist": campaign["mapped_netlist"],
            "selected_sites": campaign["selected_sites"],
            "faults": campaign["faults"],
        }
    )
    if guard.compute_campaign_digest(campaign) != campaign[
        "campaign_digest_sha256"
    ]:
        raise RuntimeError("valid campaign digest was rejected")

    changed_campaign = copy.deepcopy(campaign)
    changed_campaign["faults"][0]["fault_id"] = "TF000001_SA1"
    if guard.compute_campaign_digest(changed_campaign) == campaign[
        "campaign_digest_sha256"
    ]:
        raise RuntimeError("campaign mutation was not detected")

    print("Fault-spec valid digest          : PASS")
    print("Timestamp exclusion              : PASS")
    print("Payload mutation detection       : PASS")
    print("Stored-digest exclusion/check    : PASS")
    print("Campaign valid digest            : PASS")
    print("Campaign mutation detection      : PASS")
    print("Stage-5 guard self-test          : PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
