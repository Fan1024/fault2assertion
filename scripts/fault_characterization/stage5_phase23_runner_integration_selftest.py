#!/usr/bin/env python3
"""Fake-Xcelium integration test for the Phase-2 three-mode shell runner."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence


class TestError(RuntimeError):
    pass


def write(path: Path, text: str, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(0o755)


def import_module(path: Path):
    spec = importlib.util.spec_from_file_location("f2a_phase23_prep_for_runner", path)
    if spec is None or spec.loader is None:
        raise TestError(path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_tree(root: Path, args: argparse.Namespace) -> dict[str, Path]:
    f2a, cv, bin_dir = root / "f2a", root / "cv", root / "bin"
    for path in (
        f2a / "scripts/lib",
        f2a / "scripts/fault_characterization",
        f2a / "platform/cv32e40p/tb",
        f2a / "platform/cv32e40p",
        bin_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)
    copies = {
        args.common: f2a / "scripts/lib/xrun_stage5_common.sh",
        args.verdict: f2a / "scripts/fault_characterization/stage5_verdict.py",
        args.bundle: f2a / "scripts/fault_characterization/stage5_reproduction_bundle.py",
        args.prepare_mm_ram: f2a / "platform/cv32e40p/prepare_stage5_mm_ram.py",
        args.policy: f2a / "platform/cv32e40p/stage5_assertion_policy_v1.json",
    }
    for source, target in copies.items():
        shutil.copy2(source, target)
        target.chmod(0o755 if target.suffix in {".py", ".sh"} else 0o644)
    prep = import_module(args.prepare_mm_ram)
    synthetic_mm_ram = (
        "module mm_ram;\n"
        + prep.DECLARATION_ANCHOR
        + "  always_comb begin\n"
        + prep.WRITE_BRANCH_OLD
        + "  end\n"
        + prep.ASSERTION_OLD
        + "endmodule\n"
    )
    write(cv / "verification/shared/tb/mm_ram.sv", synthetic_mm_ram)
    for name in (
        "include/perturbation_pkg.sv",
        "amo_shim.sv",
        "cv32e40p_random_interrupt_generator.sv",
        "dp_ram.sv",
        "riscv_gnt_stall.sv",
        "riscv_rvalid_stall.sv",
        "tb_top.sv",
    ):
        write(cv / "verification/shared/tb" / name, "module dummy; endmodule\n")
    for name in (
        "cv32e40p_apu_core_pkg.sv",
        "cv32e40p_pkg.sv",
        "cv32e40p_fpu_pkg.sv",
    ):
        write(cv / "rtl/include" / name, "package p; endpackage\n")
    write(f2a / "platform/cv32e40p/tb/cv32e40p_tb_subsystem.sv", "module s; endmodule\n")
    write(
        f2a / "platform/cv32e40p/prepare_netlist.py",
        "#!/usr/bin/env python3\nimport shutil,sys\nshutil.copyfile(sys.argv[1],sys.argv[2])\n",
        True,
    )
    write(
        f2a / "scripts/fault_characterization/fake_apply.py",
        "#!/usr/bin/env python3\nimport argparse,os,shutil\n"
        "p=argparse.ArgumentParser(); p.add_argument('command'); p.add_argument('--fault-json'); p.add_argument('--output-netlist'); a=p.parse_args(); "
        "shutil.copyfile(os.environ['F2A_FAKE_GOLDEN'],a.output_netlist)\n",
        True,
    )
    write(
        f2a / "scripts/setup_env.sh",
        "#!/usr/bin/env bash\nexport PATH=\"${F2A_FAKE_BIN}:$PATH\"\nexport CV32E40P_CELL_MODEL=\"${F2A_FAKE_CELL}\"\n",
        True,
    )
    write(f2a / "build/cv32e40p/crc32/crc32.hex", "00\n")
    write(f2a / "build/cv32e40p/crc32/crc32.elf", "ELF\n")
    write(root / "cell.v", "module C; endmodule\n")
    write(root / "golden.v", "module cv32e40p_top; endmodule\n")
    write(root / "fault.json", '{"stage":"stage_05_fault_materialization","fault_id":"TF000002_SA0"}\n')
    write(
        bin_dir / "xrun",
        r'''#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-version" ]]; then echo 'fake xrun 4.0'; exit 0; fi
log=''; elaborate=0; mode='native'; event=''; prev=''
for arg in "$@"; do
  [[ "$prev" == '-l' ]] && log="$arg"
  [[ "$arg" == '-elaborate' ]] && elaborate=1
  case "$arg" in
    +f2a_assert_mode=*) mode="${arg#*=}" ;;
    +f2a_assert_event_file=*) event="${arg#*=}" ;;
  esac
  prev="$arg"
done
[[ -n "$log" ]] || exit 9
mkdir -p "$(dirname "$log")"
if [[ "$elaborate" == 1 ]]; then echo 'compile clean' > "$log"; exit 0; fi
mkdir -p "$(dirname "$STAGE5_TRACE_OUTPUT")" "$(dirname "$event")"
if [[ "${RUN_KIND}" == golden ]]; then
  printf 'H\tGOLDEN\nG\tTS000002\t1\t10\ttop.u.mon\t1\t0\n' > "$STAGE5_TRACE_OUTPUT"
  printf 'H\tF2A_ASSERT_EVENTS\t1\tnative\n' > "$event"
  printf '%s\n' 'CRC32 PASS: vector=cbf43926 signature=2d6352b3 last=5650ac83' 'EXIT SUCCESS' > "$log"
  exit 0
fi
printf 'H\tFAULT\tTF000002_SA0\nF\tTF000002_SA0\t1\t10\ttop.u.mon\t1\t0\t0\n' > "$STAGE5_TRACE_OUTPUT"
case "$mode" in
 native)
  printf 'H\tF2A_ASSERT_EVENTS\t1\tnative\nA\t0\t73\t780 NS\tPREEXISTING_TB_ASSERTION\tout_of_bounds_write\tILLEGAL_MEMORY_WRITE\txxxxxxxx\t00000000\tf\tFATAL_TERMINATION\n' > "$event"
  cat > "$log" <<'EOF'
xmsim: *F,ASRTST: (/tmp/verification/shared/tb/mm_ram.sv,367): (time 780 NS) Assertion tb_top.wrapper_i.ram_i.out_of_bounds_write has failed
Simulation terminated via $fatal(1) at time 780 NS + 0
EOF
  exit 2 ;;
 observe)
  printf 'H\tF2A_ASSERT_EVENTS\t1\tobserve\nA\t0\t73\t780 NS\tPREEXISTING_TB_ASSERTION\tout_of_bounds_write\tILLEGAL_MEMORY_WRITE\txxxxxxxx\t00000000\tf\tRECORD_ONLY\n' > "$event"
  echo 'Simulation aborted due to maximum cycle limit' > "$log"
  exit 2 ;;
 diagnostic_quarantine)
  printf 'H\tF2A_ASSERT_EVENTS\t1\tdiagnostic_quarantine\nA\t0\t73\t780 NS\tPREEXISTING_TB_ASSERTION\tout_of_bounds_write\tILLEGAL_MEMORY_WRITE\txxxxxxxx\t00000000\tf\tRECORD_AND_QUARANTINE\n' > "$event"
  printf '%s\n' 'CRC32 PASS: vector=cbf43926 signature=2d6352b3 last=5650ac83' 'EXIT SUCCESS' > "$log"
  exit 0 ;;
 *) exit 8 ;;
esac
''',
        True,
    )
    return {
        "f2a": f2a,
        "cv": cv,
        "bin": bin_dir,
        "cell": root / "cell.v",
        "golden": root / "golden.v",
        "fault": root / "fault.json",
        "apply": f2a / "scripts/fault_characterization/fake_apply.py",
        "common": f2a / "scripts/lib/xrun_stage5_common.sh",
    }


def run(tree: dict[str, Path], root: Path, name: str, kind: str, phase: str, purpose: str, expected: str, rc: int) -> None:
    run_dir, trace = root / f"run_{name}", root / f"{name}.trace.tsv"
    monitor = root / f"{name}.sv"
    write(monitor, f'module monitor; initial $display("{trace.resolve()}"); endmodule\n')
    harness = root / f"{name}.sh"
    write(
        harness,
        f'''#!/usr/bin/env bash
set -euo pipefail
export F2A_ROOT={tree['f2a']}
export F2A_HOME={tree['f2a']}
export CV32E40P_HOME={tree['cv']}
export F2A_FAKE_BIN={tree['bin']}
export F2A_FAKE_CELL={tree['cell']}
export F2A_FAKE_GOLDEN={tree['golden']}
export STAGE5_TRACE_OUTPUT={trace.resolve()}
RUN_KIND={kind}; export RUN_KIND
DESIGN=cv32e40p; WORKLOAD=crc32; SIM_LEVEL=netlist
RUN_NAME={name}; RUN_DIR={run_dir.resolve()}; EXTRA_SV_SOURCE={monitor.resolve()}
GOLDEN_NETLIST={tree['golden']}; FAULT_JSON={tree['fault']}; FAULT_ID=TF000002_SA0
STAGE5_FAULT_APPLIER={tree['apply']}; STAGE5_PHASE={phase}; STAGE5_RUN_PURPOSE={purpose}
MAXCYCLES=100; VCD=0; VERBOSE=0; KEEP_WORK=0; WRAPPER_COMMAND=synthetic
source {tree['common']}
set +e; f2a_stage5_run_xrun; value=$?; set -e; exit "$value"
''',
        True,
    )
    completed = subprocess.run([str(harness)], text=True, capture_output=True, env={**os.environ})
    if completed.returncode != rc:
        raise TestError(f"{name}: rc {completed.returncode} != {rc}\n{completed.stdout}\n{completed.stderr}")
    result = json.loads((run_dir / "result.json").read_text())
    if result["status"] != expected or result["verdict_engine_version"] != "4.0.0":
        raise TestError(f"{name}: {result['status']} / {result['verdict_engine_version']}")
    if not (run_dir / "stage5_assertion_adapter.sha256").is_file():
        raise TestError(f"{name}: adapter SHA record missing")
    if phase == "run" and not (run_dir / "assertion_events.tsv").is_file():
        raise TestError(f"{name}: event file missing")
    print(f"Phase-2 runner integration {name:22s}: PASS ({expected})")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--common", type=Path, required=True)
    parser.add_argument("--verdict", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--prepare-mm-ram", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        with tempfile.TemporaryDirectory(prefix="f2a_phase23_runner_") as value:
            root = Path(value)
            tree = make_tree(root, args)
            run(tree, root, "golden_compile", "golden", "compile", "COMPILE_CHECK", "COMPILE_PASS", 0)
            run(tree, root, "fault_compile", "fault", "compile", "COMPILE_CHECK", "COMPILE_PASS", 0)
            run(tree, root, "golden_native", "golden", "run", "NATIVE_CHARACTERIZATION", "PASS", 0)
            run(tree, root, "fault_native", "fault", "run", "NATIVE_CHARACTERIZATION", "EXISTING_ASSERTION_DETECTED", 2)
            run(tree, root, "fault_observe", "fault", "run", "DIAGNOSTIC_OBSERVE", "DIAGNOSTIC_TIMEOUT", 2)
            run(tree, root, "fault_quarantine", "fault", "run", "DIAGNOSTIC_QUARANTINE", "DIAGNOSTIC_OUTPUT_MATCH", 0)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("Stage-5 Phase-2 runner integration self-test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
