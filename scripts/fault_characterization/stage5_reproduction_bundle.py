#!/usr/bin/env python3
"""Create a compact, reconstructible Stage-5 incident bundle.

The bundle deliberately excludes Xcelium work libraries, VCDs, and generated
faulty netlists.  A fault can be reconstructed from fault.json plus the frozen
source hashes.  Compact traces are included only below a configurable size cap;
otherwise their path, size, and SHA-256 are recorded.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

PROGRAM_VERSION = "1.0.0"
DEFAULT_TRACE_LIMIT = 64 * 1024 * 1024

ROOT_FILE_ALLOWLIST = {
    "cell_model_source.txt",
    "command.txt",
    "cv32e40p_commit.txt",
    "environment.txt",
    "fault.json",
    "fault2assertion_commit.txt",
    "firmware.sha256",
    "firmware_source.txt",
    "manifest.json",
    "manifest.txt",
    "materialize.log",
    "prepare_netlist.log",
    "preflight_failure.txt",
    "mapped_netlist_source.txt",
    "netlist_sources.sha256",
    "result.env",
    "result.json",
    "result.txt",
    "signature.txt",
    "simulation_netlist.sha256",
    "stage5_monitor.sha256",
    "stage5_monitor.sv",
    "wrapper_command.txt",
    "xrun.log",
    "xrun_version.txt",
}
FORBIDDEN_COMPONENTS = {
    "fault_netlist.v",
    "cv32e40p.mapped.sim.v",
    "riscy_tb.vcd",
    "xcelium.d",
    "INCA_libs",
}


class BundleError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def deterministic_tar_add_bytes(
    archive: tarfile.TarFile,
    arcname: str,
    data: bytes,
) -> None:
    info = tarfile.TarInfo(name=arcname)
    info.size = len(data)
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mode = 0o644
    archive.addfile(info, io.BytesIO(data))


def deterministic_tar_add_file(
    archive: tarfile.TarFile,
    arcname: str,
    path: Path,
) -> None:
    deterministic_tar_add_bytes(archive, arcname, path.read_bytes())


def safe_tail(path: Path, max_lines: int = 200) -> str:
    if not path.is_file():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:]) + ("\n" if lines else "")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--trace", type=Path)
    parser.add_argument("--trace-size-limit", type=int, default=DEFAULT_TRACE_LIMIT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_dir = args.run_dir.resolve()
    output = args.output.resolve()
    manifest_path = args.manifest.resolve()
    if not run_dir.is_dir():
        raise SystemExit(f"ERROR: run directory not found: {run_dir}")
    if args.trace_size_limit < 0:
        raise SystemExit("ERROR: trace-size-limit must be non-negative")
    if output.parent != run_dir:
        raise SystemExit("ERROR: bundle output must be directly inside run directory")
    if manifest_path.parent != run_dir:
        raise SystemExit("ERROR: bundle manifest must be directly inside run directory")

    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    files_to_add: list[tuple[str, Path]] = []

    for name in sorted(ROOT_FILE_ALLOWLIST):
        path = run_dir / name
        if path.is_file():
            files_to_add.append((f"run/{name}", path))
            included.append(
                {
                    "archive_path": f"run/{name}",
                    "source_path": str(path),
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )

    trace_record: dict[str, Any] | None = None
    trace_payload: bytes | None = None
    trace_archive_path: str | None = None
    if args.trace is not None:
        trace_path = args.trace.resolve()
        trace_record = {
            "path": str(trace_path),
            "exists": trace_path.is_file(),
        }
        if trace_path.is_file():
            trace_record.update(
                {
                    "size_bytes": trace_path.stat().st_size,
                    "sha256": sha256_file(trace_path),
                }
            )
            if trace_path.stat().st_size <= args.trace_size_limit:
                raw = trace_path.read_bytes()
                trace_payload = gzip.compress(raw, compresslevel=9, mtime=0)
                trace_archive_path = f"trace/{trace_path.name}.gz"
                trace_record["included"] = True
                trace_record["archive_path"] = trace_archive_path
            else:
                trace_record["included"] = False
                trace_record["reason"] = "trace_exceeds_size_limit"
                tail = safe_tail(trace_path)
                if tail:
                    trace_payload = gzip.compress(
                        tail.encode("utf-8"), compresslevel=9, mtime=0
                    )
                    trace_archive_path = f"trace/{trace_path.name}.tail.txt.gz"
                    trace_record["tail_archive_path"] = trace_archive_path

    work_dir = run_dir / "work"
    if work_dir.exists():
        excluded.append(
            {
                "path": str(work_dir),
                "reason": "Xcelium work libraries and generated netlists are intentionally excluded",
            }
        )

    for forbidden in sorted(FORBIDDEN_COMPONENTS):
        for path in run_dir.rglob(forbidden):
            excluded.append(
                {
                    "path": str(path),
                    "reason": "forbidden large or generated artifact",
                }
            )

    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "bundle_tool_version": PROGRAM_VERSION,
        "generated_at_utc": utc_now(),
        "kind": "stage5_reproduction_bundle",
        "status": args.status,
        "run_directory": str(run_dir),
        "bundle": str(output),
        "included_files": included,
        "trace": trace_record,
        "excluded_artifacts": excluded,
        "reconstruction_policy": {
            "faulty_netlist_included": False,
            "xcelium_work_library_included": False,
            "vcd_included": False,
            "reconstruct_fault_from": [
                "run/fault.json when present",
                "run/netlist_sources.sha256",
                "run/simulation_netlist.sha256",
                "run/stage5_monitor.sv",
                "run/command.txt",
            ],
        },
    }
    manifest_text = json.dumps(manifest, indent=2, sort_keys=False) + "\n"
    manifest_path.write_text(manifest_text, encoding="utf-8")

    readme = (
        "Fault2Assertion Stage-5 reproduction bundle\n"
        "=============================================\n\n"
        f"Recorded status: {args.status}\n\n"
        "This archive intentionally excludes generated faulty netlists, VCDs, "
        "and Xcelium work libraries. Recreate the run through the recorded "
        "Stage-5 wrapper after verifying every SHA-256 file in run/.\n"
    ).encode("utf-8")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    with tarfile.open(temporary, mode="w:gz", compresslevel=9) as archive:
        deterministic_tar_add_bytes(archive, "README_REPRODUCE.txt", readme)
        deterministic_tar_add_bytes(
            archive,
            "reproduction_bundle_manifest.json",
            manifest_text.encode("utf-8"),
        )
        for arcname, path in files_to_add:
            deterministic_tar_add_file(archive, arcname, path)
        if trace_payload is not None and trace_archive_path is not None:
            deterministic_tar_add_bytes(
                archive,
                trace_archive_path,
                trace_payload,
            )
    temporary.replace(output)

    # Verify the generated archive before returning success.
    with tarfile.open(output, mode="r:gz") as archive:
        names = archive.getnames()
        if "reproduction_bundle_manifest.json" not in names:
            raise BundleError("generated archive is missing its manifest")
        forbidden_names = [
            name
            for name in names
            if any(component in FORBIDDEN_COMPONENTS for component in Path(name).parts)
        ]
        if forbidden_names:
            raise BundleError(
                f"generated archive contains forbidden artifacts: {forbidden_names}"
            )

    print(f"Reproduction bundle : {output}")
    print(f"Bundle manifest     : {manifest_path}")
    print(f"Included files      : {len(included)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
