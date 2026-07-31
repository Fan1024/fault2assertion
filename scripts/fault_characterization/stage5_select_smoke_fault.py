#!/usr/bin/env python3
"""Select one deterministic mini-campaign fault for Gate 2/4."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

PREFERRED = (
    ("control_path", "SA0"),
    ("architectural_data", "SA0"),
    ("sequential_state", "SA0"),
    ("generic_observable", "SA0"),
    ("control_path", "SA1"),
    ("architectural_data", "SA1"),
    ("sequential_state", "SA1"),
    ("generic_observable", "SA1"),
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    campaign_path = args.campaign.resolve()
    output = args.output.resolve()
    if output.exists() and not args.force:
        raise SystemExit(f"ERROR: refusing to overwrite without --force: {output}")
    campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
    if campaign.get("stage") != "stage_05_fault_characterization_campaign":
        raise SystemExit("ERROR: invalid Stage-5 campaign marker")
    faults = campaign.get("faults")
    if not isinstance(faults, list) or not faults:
        raise SystemExit("ERROR: campaign contains no faults")

    chosen: dict[str, Any] | None = None
    for fault_class, polarity in PREFERRED:
        candidates = sorted(
            (
                item
                for item in faults
                if item.get("fault_class") == fault_class
                and item.get("polarity") == polarity
            ),
            key=lambda item: str(item.get("fault_id", "")),
        )
        if candidates:
            chosen = dict(candidates[0])
            break
    if chosen is None:
        raise SystemExit("ERROR: no supported smoke fault found")

    fault_spec = Path(str(chosen["fault_spec"])).resolve()
    if not fault_spec.is_file():
        raise SystemExit(f"ERROR: selected fault spec not found: {fault_spec}")
    spec = json.loads(fault_spec.read_text(encoding="utf-8"))
    if spec.get("fault_id") != chosen.get("fault_id"):
        raise SystemExit("ERROR: campaign/spec fault ID mismatch")
    if spec.get("fault_class") != chosen.get("fault_class"):
        raise SystemExit("ERROR: campaign/spec fault-class mismatch")
    if spec.get("polarity") != chosen.get("polarity"):
        raise SystemExit("ERROR: campaign/spec polarity mismatch")

    payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "kind": "stage5_mini_smoke_fault_selection",
        "campaign": str(campaign_path),
        "campaign_sha256": sha256_file(campaign_path),
        "fault_id": spec["fault_id"],
        "selection_id": spec["selection_id"],
        "site_id": spec["site_id"],
        "fault_class": spec["fault_class"],
        "polarity": spec["polarity"],
        "stuck_at": spec["stuck_at"],
        "fault_spec": str(fault_spec),
        "fault_spec_sha256": sha256_file(fault_spec),
        "fault_spec_digest_sha256": spec["fault_spec_digest_sha256"],
        "selection_policy": "first available by frozen class/polarity preference",
        "preference_order": [
            {"fault_class": fault_class, "polarity": polarity}
            for fault_class, polarity in PREFERRED
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Smoke fault          : {payload['fault_id']}")
    print(f"Fault class          : {payload['fault_class']}")
    print(f"Polarity             : {payload['polarity']}")
    print(f"Selection record     : {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
