#!/usr/bin/env python3
"""Synthetic create/verify/mutation test for Stage-5 execution-input locks."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence


def write(path: Path, text: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run(command: list[str], expected: int, label: str) -> None:
    completed = subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != expected:
        raise RuntimeError(
            f"{label}: expected rc={expected}, actual={completed.returncode}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    print(f"Execution lock {label:24s}: PASS")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        with tempfile.TemporaryDirectory(prefix="f2a_execution_lock_") as temporary:
            root = Path(temporary)
            repo = root / "f2a"
            cv = root / "cv"
            tb = cv / "verification/shared/tb"
            for path in (
                repo / "build/cv32e40p/crc32/crc32.hex",
                repo / "build/cv32e40p/crc32/crc32.elf",
                repo / "scripts/setup_env.sh",
                repo / "scripts/lib/xrun_stage5_common.sh",
                repo / "scripts/run_xrun_stage5_golden.sh",
                repo / "scripts/run_xrun_stage5_fault.sh",
                repo / "scripts/fault_characterization/stage5_verdict.py",
                repo / "scripts/fault_characterization/stage5_reproduction_bundle.py",
                repo / "scripts/fault_characterization/stage5_faults.py",
                repo / "platform/cv32e40p/prepare_netlist.py",
                repo / "platform/cv32e40p/tb/cv32e40p_tb_subsystem.sv",
                cv / "rtl/include/cv32e40p_apu_core_pkg.sv",
                cv / "rtl/include/cv32e40p_pkg.sv",
                cv / "rtl/include/cv32e40p_fpu_pkg.sv",
                tb / "include/perturbation_pkg.sv",
                tb / "amo_shim.sv",
                tb / "cv32e40p_random_interrupt_generator.sv",
                tb / "dp_ram.sv",
                tb / "riscv_gnt_stall.sv",
                tb / "riscv_rvalid_stall.sv",
                tb / "mm_ram.sv",
                tb / "tb_top.sv",
            ):
                write(path)
            write(cv / "bhv/include/a.svh")
            write(cv / "sva/a.sv")
            cell = root / "cell.v"
            mapped = root / "mapped.v"
            monitor1 = root / "golden_monitor.sv"
            monitor2 = root / "fault_monitor.sv"
            for path in (cell, mapped, monitor1, monitor2):
                write(path)
            lock = root / "lock.json"
            create = [
                sys.executable,
                str(args.tool.resolve()),
                "create",
                "--repo-root", str(repo),
                "--cv32e40p-home", str(cv),
                "--cell-model", str(cell),
                "--mapped-netlist", str(mapped),
                "--monitor", str(monitor1),
                "--monitor", str(monitor2),
                "--output", str(lock),
            ]
            verify = [
                sys.executable,
                str(args.tool.resolve()),
                "verify",
                "--lock", str(lock),
            ]
            run(create, 0, "create")
            run(verify, 0, "verify unchanged")
            write(repo / "build/cv32e40p/crc32/crc32.hex", "changed\n")
            run(verify, 1, "detect mutation")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("Stage-5 execution-input lock self-test : PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
