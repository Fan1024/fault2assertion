#!/usr/bin/env python3
"""Independent synthetic tests for Stage-5 layered native-execution results."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Sequence


class SelfTestError(RuntimeError):
    pass


def load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path.resolve())
    if spec is None or spec.loader is None:
        raise SelfTestError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def expect(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise SelfTestError(f"{label}: expected={expected!r}, actual={actual!r}")
    print(f"{label:58s}: PASS")


def assertion_log() -> str:
    return "\n".join(
        [
            "[TB] Netlist fetch enabled",
            "xmsim: *F,ASRTST: (/tmp/verification/shared/tb/mm_ram.sv,367): "
            "(time 780 NS) Assertion "
            "tb_top.wrapper_i.ram_i.out_of_bounds_write has failed",
            "         X         0",
            "Simulation terminated via $fatal(1) at time 780 NS + 11",
            "/tmp/verification/shared/tb/mm_ram.sv:367   "
            "else $fatal(\"out of bounds write to %08x with %08x\", "
            "data_addr_i, data_wdata_i);",
        ]
    ) + "\n"


def verdict_tests(verdict_module: Any) -> None:
    exact = (
        "CRC32 PASS: vector=cbf43926 signature=2d6352b3 last=5650ac83\n"
        "EXIT SUCCESS\n"
    )
    cases = [
        ("compile", "golden", 0, "compile clean\n", "COMPILE_PASS"),
        ("compile", "golden", 1, "xmvlog: *E,BADTFB broken\n", "COMPILE_ERROR"),
        ("run", "golden", 0, exact, "PASS"),
        ("run", "fault", 0, exact, "OUTPUT_MATCH"),
        ("run", "golden", 0, "EXIT SUCCESS\n", "UNKNOWN"),
        (
            "run",
            "fault",
            0,
            "CRC32 PASS: vector=cbf43926 signature=00000000 last=5650ac83\n"
            "EXIT SUCCESS\n",
            "OUTPUT_MISMATCH",
        ),
        ("run", "fault", 0, "CRC32 FAIL\nEXIT FAILURE\n", "OUTPUT_MISMATCH"),
        (
            "run",
            "fault",
            1,
            "Simulation aborted due to maximum cycle limit\n",
            "TIMEOUT",
        ),
        ("run", "fault", 1, "xmelab: *E,CUVMUR unresolved\n", "ERROR"),
        ("run", "fault", 0, "no terminal marker\n", "UNKNOWN"),
        ("run", "fault", 2, assertion_log(), "EXISTING_ASSERTION_DETECTED"),
        ("run", "golden", 2, assertion_log(), "GOLDEN_INVALID"),
    ]
    for index, (phase, kind, xstatus, text, expected) in enumerate(cases, start=1):
        verdict = verdict_module.compute_verdict(
            phase=phase,
            run_kind=kind,
            xrun_exit_status=xstatus,
            log_text=text,
        )
        expect(verdict.status, expected, f"Verdict scenario {index:02d} ({expected})")

    fault_assertion = verdict_module.compute_verdict(
        phase="run",
        run_kind="fault",
        xrun_exit_status=2,
        log_text=assertion_log(),
    )
    raw = fault_assertion.raw_facts
    expect(raw["tool"]["status"], "OK", "ASRTST is not infrastructure error")
    expect(
        raw["tool"]["infrastructure_error_count"],
        0,
        "ASRTST infrastructure count",
    )
    expect(
        raw["execution"]["completion"],
        "TERMINATED_BY_EXISTING_ASSERTION",
        "ASRTST execution completion",
    )
    expect(raw["workload"]["outcome"], "NOT_REACHED", "ASRTST workload outcome")
    expect(
        raw["workload"]["architectural_outcome"],
        "CENSORED",
        "ASRTST architectural outcome",
    )
    detector = raw["existing_detector_baseline"]
    expect(detector["event_count"], 1, "ASRTST detector count")
    event = detector["events"][0]
    expect(
        event["detector_origin"],
        "PREEXISTING_TB_ASSERTION",
        "ASRTST detector origin",
    )
    expect(
        event["assertion_leaf_name"],
        "out_of_bounds_write",
        "ASRTST assertion name",
    )
    expect(event["action"], "FATAL_TERMINATION", "ASRTST action")
    expect(event["source_line"], 367, "ASRTST source line")
    expect(event["simulation_time"], {"value": "780", "unit": "NS"}, "ASRTST time")


def bundle_test(bundle_script: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="f2a_runner_selftest_") as temporary:
        root = Path(temporary)
        run_dir = root / "run"
        work_dir = run_dir / "work"
        run_dir.mkdir()
        work_dir.mkdir()
        (run_dir / "xrun.log").write_text(assertion_log(), encoding="utf-8")
        (run_dir / "result.txt").write_text(
            "EXISTING_ASSERTION_DETECTED\n", encoding="utf-8"
        )
        (run_dir / "command.txt").write_text("xrun\n", encoding="utf-8")
        (run_dir / "stage5_monitor.sv").write_text(
            "module m; endmodule\n", encoding="utf-8"
        )
        (run_dir / "fault.json").write_text(
            '{"fault_id":"TF000001_SA0"}\n', encoding="utf-8"
        )
        (work_dir / "fault_netlist.v").write_text("forbidden\n", encoding="utf-8")
        (work_dir / "cv32e40p.mapped.sim.v").write_text(
            "forbidden\n", encoding="utf-8"
        )
        trace = root / "fault.trace.tsv"
        trace.write_text(
            "H\tFAULT\tTF000001_SA0\n"
            "F\tTF000001_SA0\t1\t10\ttop.u\t0\t0\t0\n",
            encoding="utf-8",
        )
        output = run_dir / "reproduction_bundle.tar.gz"
        manifest = run_dir / "reproduction_bundle_manifest.json"
        completed = subprocess.run(
            [
                sys.executable,
                str(bundle_script),
                "--run-dir",
                str(run_dir),
                "--status",
                "EXISTING_ASSERTION_DETECTED",
                "--trace",
                str(trace),
                "--output",
                str(output),
                "--manifest",
                str(manifest),
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if completed.returncode != 0:
            raise SelfTestError(
                f"bundle helper failed:\n{completed.stdout}\n{completed.stderr}"
            )
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        expect(
            payload["status"],
            "EXISTING_ASSERTION_DETECTED",
            "Bundle manifest status",
        )
        expect(output.is_file(), True, "Bundle archive created")
        with tarfile.open(output, "r:gz") as archive:
            names = archive.getnames()
        forbidden = [
            name
            for name in names
            if name.endswith("fault_netlist.v")
            or name.endswith("cv32e40p.mapped.sim.v")
            or name.endswith("riscy_tb.vcd")
        ]
        expect(forbidden, [], "Bundle excludes generated netlists/VCD")
        expect("run/xrun.log" in names, True, "Bundle includes xrun.log")
        expect(
            "reproduction_bundle_manifest.json" in names,
            True,
            "Bundle includes manifest",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verdict", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        verdict_module = load_module(args.verdict, "f2a_stage5_verdict_selftest")
        verdict_tests(verdict_module)
        bundle_test(args.bundle.resolve())
    except SelfTestError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("Stage-5 runner self-test                             : PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
