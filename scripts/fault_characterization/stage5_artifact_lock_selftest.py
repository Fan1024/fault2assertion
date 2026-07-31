#!/usr/bin/env python3
"""Self-test stage5_artifact_lock.py create/verify and mutation detection."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence


def run(command: list[str], expected: int) -> None:
    completed = subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if completed.returncode != expected:
        raise RuntimeError(
            f"unexpected return code {completed.returncode}, expected {expected}\n"
            + completed.stdout
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool", type=Path, required=True)
    args = parser.parse_args(argv)
    tool = args.tool.resolve()
    with tempfile.TemporaryDirectory(prefix="f2a_artifact_lock_") as temp:
        root = Path(temp)
        first = root / "first.txt"
        second = root / "second.txt"
        lock = root / "lock.json"
        first.write_text("alpha\n", encoding="utf-8")
        second.write_text("beta\n", encoding="utf-8")
        run(
            [
                sys.executable,
                str(tool),
                "create",
                "--kind",
                "selftest",
                "--file",
                f"first={first}",
                "--file",
                f"second={second}",
                "--output",
                str(lock),
            ],
            0,
        )
        run([sys.executable, str(tool), "verify", "--lock", str(lock)], 0)
        second.write_text("mutated\n", encoding="utf-8")
        run([sys.executable, str(tool), "verify", "--lock", str(lock)], 1)
    print("Artifact-lock create/verify          : PASS")
    print("Artifact-lock mutation detection     : PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
