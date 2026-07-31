#!/usr/bin/env python3
"""Shared fail-closed helpers for Stage-5 Gate 2/3/4 validators."""

from __future__ import annotations

import gzip
import hashlib
import json
import tarfile
from pathlib import Path
from typing import Any


class GateValidationError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GateValidationError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise GateValidationError(f"invalid {label} JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise GateValidationError(f"{label} must contain one JSON object: {path}")
    return payload


def require_file(path: Path, label: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise GateValidationError(f"{label} missing or empty: {path}")


def require_absent(path: Path, label: str) -> None:
    if path.exists():
        raise GateValidationError(f"{label} must not exist: {path}")


def read_result(run_dir: Path) -> dict[str, Any]:
    result = load_json(run_dir / "result.json", "runner result")
    text_path = run_dir / "result.txt"
    require_file(text_path, "result.txt")
    text_value = text_path.read_text(encoding="utf-8").strip()
    if text_value != result.get("status"):
        raise GateValidationError(
            f"result.txt/result.json mismatch: {text_value!r} vs {result.get('status')!r}"
        )
    return result


def read_retention(run_dir: Path) -> dict[str, Any]:
    return load_json(run_dir / "retention.json", "retention record")


def validate_bundle(run_dir: Path, expected_status: str) -> dict[str, Any]:
    bundle = run_dir / "reproduction_bundle.tar.gz"
    manifest_path = run_dir / "reproduction_bundle_manifest.json"
    require_file(bundle, "reproduction bundle")
    manifest = load_json(manifest_path, "reproduction bundle manifest")
    if manifest.get("kind") != "stage5_reproduction_bundle":
        raise GateValidationError("invalid reproduction bundle kind")
    if manifest.get("status") != expected_status:
        raise GateValidationError(
            f"bundle status mismatch: expected={expected_status}, actual={manifest.get('status')}"
        )
    with tarfile.open(bundle, "r:gz") as archive:
        names = archive.getnames()
    required = {
        "README_REPRODUCE.txt",
        "reproduction_bundle_manifest.json",
        "run/xrun.log",
        "run/result.json",
        "run/command.txt",
        "run/stage5_monitor.sv",
    }
    missing = sorted(required - set(names))
    if missing:
        raise GateValidationError(f"bundle missing required entries: {missing}")
    forbidden_suffixes = (
        "fault_netlist.v",
        "cv32e40p.mapped.sim.v",
        "riscy_tb.vcd",
    )
    forbidden = [name for name in names if name.endswith(forbidden_suffixes)]
    if forbidden:
        raise GateValidationError(f"bundle contains forbidden artifacts: {forbidden}")
    return manifest


def validate_no_vcd(root: Path) -> None:
    vcds = list(root.rglob("*.vcd"))
    if vcds:
        raise GateValidationError(f"unexpected VCD files: {vcds[:5]}")


def iter_trace_rows(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as stream:
        for line_number, raw in enumerate(stream, start=1):
            stripped = raw.rstrip("\n")
            if stripped:
                yield line_number, stripped.split("\t")

def parse_sha256sum_file(path: Path, label: str) -> list[dict[str, str]]:
    require_file(path, label)
    records: list[dict[str, str]] = []
    for line_number, raw in enumerate(
        path.read_text(encoding="utf-8", errors="strict").splitlines(), start=1
    ):
        if not raw:
            continue
        digest = raw[:64]
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise GateValidationError(
                f"malformed SHA-256 digest at {path}:{line_number}: {raw!r}"
            )
        rest = raw[64:].lstrip()
        if rest.startswith("*"):
            rest = rest[1:]
        if not rest:
            raise GateValidationError(
                f"missing SHA-256 filename at {path}:{line_number}"
            )
        records.append({"sha256": digest, "path": rest})
    if not records:
        raise GateValidationError(f"SHA-256 file contains no records: {path}")
    return records


def collect_run_input_hashes(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    firmware_records = parse_sha256sum_file(
        run_dir / "firmware.sha256", "firmware SHA file"
    )
    firmware = {Path(item["path"]).name: item["sha256"] for item in firmware_records}
    if set(firmware) != {"crc32.hex", "crc32.elf"}:
        raise GateValidationError(
            f"unexpected firmware SHA entries at {run_dir}: {sorted(firmware)}"
        )

    netlist_records = parse_sha256sum_file(
        run_dir / "netlist_sources.sha256", "netlist source SHA file"
    )
    if len(netlist_records) != 2:
        raise GateValidationError(
            f"netlist source SHA file must contain raw netlist and cell model: {run_dir}"
        )
    prepared = parse_sha256sum_file(
        run_dir / "simulation_netlist.sha256", "prepared netlist SHA file"
    )
    monitor = parse_sha256sum_file(
        run_dir / "stage5_monitor.sha256", "monitor SHA file"
    )
    adapter = parse_sha256sum_file(
        run_dir / "stage5_assertion_adapter.sha256",
        "Stage-5 assertion adapter SHA file",
    )
    if len(prepared) != 1 or len(monitor) != 1 or len(adapter) != 4:
        raise GateValidationError(
            f"unexpected prepared/monitor/adapter SHA count: {run_dir}"
        )
    adapter_by_name = {
        Path(item["path"]).name: item["sha256"] for item in adapter
    }
    required_adapter_names = {
        "mm_ram.sv",
        "mm_ram.stage5.sv",
        "stage5_assertion_policy_v1.json",
        "prepare_stage5_mm_ram.py",
    }
    if set(adapter_by_name) != required_adapter_names:
        raise GateValidationError(
            "unexpected assertion-adapter SHA entries: "
            f"{sorted(adapter_by_name)}"
        )
    return {
        "firmware": firmware,
        "raw_netlist_sha256": netlist_records[0]["sha256"],
        "cell_model_sha256": netlist_records[1]["sha256"],
        "prepared_netlist_sha256": prepared[0]["sha256"],
        "monitor_sha256": monitor[0]["sha256"],
        "assertion_adapter": adapter_by_name,
    }

