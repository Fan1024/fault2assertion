#!/usr/bin/env python3
"""Create a Phase2-G1 monitor by changing only the compact-trace path."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


VERSION = "1.0.0"


class PrepareError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require_file(path: Path, label: str) -> Path:
    path = path.expanduser().resolve()
    if not path.is_file() or path.stat().st_size == 0:
        raise PrepareError(f"{label} not found or empty: {path}")
    return path


def load_json(path: Path, label: str) -> dict[str, Any]:
    path = require_file(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PrepareError(f"invalid {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PrepareError(f"{label} must be a JSON object: {path}")
    return value


def atomic_write(path: Path, text: str) -> None:
    path = path.expanduser().resolve()
    if path.exists():
        raise PrepareError(f"refusing to overwrite existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def prepare(args: argparse.Namespace) -> int:
    base_monitor = require_file(args.base_monitor, "base monitor")
    base_manifest = require_file(args.base_manifest, "base manifest")
    manifest = load_json(base_manifest, "base manifest")

    old_value = manifest.get("trace_output")
    if not isinstance(old_value, str) or not old_value:
        raise PrepareError("base manifest has no non-empty trace_output")

    old_trace = str(Path(old_value).expanduser().resolve())
    new_trace = str(args.trace_output.expanduser().resolve())

    source = base_monitor.read_text(encoding="utf-8", errors="strict")

    if source.count(old_trace) != 1:
        raise PrepareError(
            "base monitor must contain the manifest trace path exactly once: "
            f"count={source.count(old_trace)} path={old_trace}"
        )

    forbidden_before = (
        "f2a_phase2_g1_mode_adapter",
        "F2A_PHASE2_G1_ADAPTER_BEGIN",
        "f2a_assert_mode=%s",
        "mm_ram.stage5.sv",
    )
    for marker in forbidden_before:
        if marker in source:
            raise PrepareError(
                f"base monitor already contains Phase-2 infrastructure: {marker}"
            )

    generated = source.replace(old_trace, new_trace, 1)

    if old_trace != new_trace and old_trace in generated:
        raise PrepareError("generated monitor retained the old trace path")
    if generated.count(new_trace) != 1:
        raise PrepareError("generated monitor must contain the new trace path once")

    # The only allowed source change is the exact trace-path substitution.
    round_trip = generated.replace(new_trace, old_trace, 1)
    if round_trip != source:
        raise PrepareError("monitor changed beyond the trace-path substitution")

    for marker in forbidden_before:
        if marker in generated:
            raise PrepareError(f"generated monitor contains forbidden marker: {marker}")

    output = args.output.expanduser().resolve()
    metadata = args.metadata.expanduser().resolve()

    atomic_write(output, generated)

    payload = {
        "schema_version": "1.0",
        "program_version": VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "role": args.role,
        "base_monitor": str(base_monitor),
        "base_monitor_sha256": sha256_file(base_monitor),
        "base_manifest": str(base_manifest),
        "base_manifest_sha256": sha256_file(base_manifest),
        "old_trace_output": old_trace,
        "trace_output": new_trace,
        "generated_monitor": str(output),
        "generated_monitor_sha256": sha256_file(output),
        "change_kind": "TRACE_PATH_ONLY",
        "mode_adapter_appended": False,
    }
    atomic_write(metadata, json.dumps(payload, indent=2) + "\n")

    print(f"Prepared {args.role} monitor: {output}")
    return 0


def selftest(_: argparse.Namespace) -> int:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="f2a_g1_monitor_") as directory:
        root = Path(directory)
        old_trace = root / "old.trace.tsv"
        new_trace = root / "new.trace.tsv"
        monitor = root / "base.sv"
        manifest = root / "manifest.json"
        output = root / "generated.sv"
        metadata = root / "generated.json"

        monitor.write_text(
            f'module m; string p = "{old_trace}"; endmodule\n',
            encoding="utf-8",
        )
        manifest.write_text(
            json.dumps({"trace_output": str(old_trace)}) + "\n",
            encoding="utf-8",
        )
        prepare(
            argparse.Namespace(
                base_monitor=monitor,
                base_manifest=manifest,
                trace_output=new_trace,
                output=output,
                metadata=metadata,
                role="golden",
            )
        )
        text = output.read_text(encoding="utf-8")
        if str(new_trace) not in text or str(old_trace) in text:
            raise PrepareError("selftest trace replacement failed")

    print("Phase2-G1 monitor preparation selftest: PASS")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--base-monitor", type=Path, required=True)
    prepare_parser.add_argument("--base-manifest", type=Path, required=True)
    prepare_parser.add_argument("--trace-output", type=Path, required=True)
    prepare_parser.add_argument("--output", type=Path, required=True)
    prepare_parser.add_argument("--metadata", type=Path, required=True)
    prepare_parser.add_argument("--role", choices=("golden", "fault"), required=True)
    prepare_parser.set_defaults(func=prepare)

    selftest_parser = subparsers.add_parser("selftest")
    selftest_parser.set_defaults(func=selftest)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except PrepareError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
