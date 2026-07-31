#!/usr/bin/env python3
"""Integration-test the hardened Stage-5 shell runner with a fake Xcelium."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence


class IntegrationError(RuntimeError):
    pass


def write(path: Path, text: str, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(0o755)


def make_tree(root: Path, common: Path, verdict: Path, bundle: Path) -> dict[str, Path]:
    f2a = root / "f2a"
    cv = root / "cv32e40p"
    bin_dir = root / "bin"
    (f2a / "scripts/lib").mkdir(parents=True, exist_ok=True)
    (f2a / "scripts/fault_characterization").mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(common, f2a / "scripts/lib/xrun_stage5_common.sh")
    shutil.copy2(verdict, f2a / "scripts/fault_characterization/stage5_verdict.py")
    shutil.copy2(bundle, f2a / "scripts/fault_characterization/stage5_reproduction_bundle.py")
    (f2a / "scripts/lib/xrun_stage5_common.sh").chmod(0o755)
    (f2a / "scripts/fault_characterization/stage5_verdict.py").chmod(0o755)
    (f2a / "scripts/fault_characterization/stage5_reproduction_bundle.py").chmod(0o755)
    write(
        f2a / "scripts/fault_characterization/fake_fault_applier.py",
        "#!/usr/bin/env python3\n"
        "import argparse, os, shutil, sys\n"
        "p=argparse.ArgumentParser()\n"
        "p.add_argument('command')\n"
        "p.add_argument('--fault-json')\n"
        "p.add_argument('--output-netlist')\n"
        "a=p.parse_args()\n"
        "if a.command != 'apply': raise SystemExit(8)\n"
        "if os.environ.get('F2A_FAKE_APPLY_FAIL') == '1':\n"
        "    print('synthetic fault materialization failure', file=sys.stderr)\n"
        "    raise SystemExit(6)\n"
        "shutil.copyfile(os.environ['F2A_FAKE_GOLDEN'], a.output_netlist)\n",
        executable=True,
    )

    write(
        f2a / "scripts/setup_env.sh",
        "#!/usr/bin/env bash\n"
        "export PATH=\"${F2A_FAKE_BIN}:$PATH\"\n"
        "export CV32E40P_CELL_MODEL=\"${F2A_FAKE_CELL_MODEL}\"\n",
        executable=True,
    )
    write(f2a / "build/cv32e40p/crc32/crc32.hex", "00\n")
    write(f2a / "build/cv32e40p/crc32/crc32.elf", "ELF\n")
    write(f2a / "platform/cv32e40p/tb/cv32e40p_tb_subsystem.sv", "module s; endmodule\n")
    write(
        f2a / "platform/cv32e40p/prepare_netlist.py",
        "#!/usr/bin/env python3\n"
        "import os, shutil, sys\n"
        "if os.environ.get('F2A_FAKE_PREP_FAIL') == '1':\n"
        "    print('synthetic prepare_netlist failure', file=sys.stderr)\n"
        "    raise SystemExit(7)\n"
        "shutil.copyfile(sys.argv[1], sys.argv[2])\n",
        executable=True,
    )
    write(root / "cell_model.v", "module CELL; endmodule\n")
    write(root / "golden.v", "module cv32e40p_top; endmodule\n")
    write(
        root / "fault.json",
        '{"stage":"stage_05_fault_materialization","fault_id":"TF000001_SA0"}\n',
    )

    for name in (
        "cv32e40p_apu_core_pkg.sv",
        "cv32e40p_pkg.sv",
        "cv32e40p_fpu_pkg.sv",
    ):
        write(cv / "rtl/include" / name, f"package {name.replace('.', '_')}; endpackage\n")
    for name in (
        "include/perturbation_pkg.sv",
        "amo_shim.sv",
        "cv32e40p_random_interrupt_generator.sv",
        "dp_ram.sv",
        "riscv_gnt_stall.sv",
        "riscv_rvalid_stall.sv",
        "mm_ram.sv",
        "tb_top.sv",
    ):
        write(cv / "verification/shared/tb" / name, "module dummy; endmodule\n")

    write(
        bin_dir / "xrun",
        r'''#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-version" ]]; then
  echo "fake xrun 1.0"
  exit 0
fi
log=""
elaborate=0
prev=""
for arg in "$@"; do
  if [[ "$prev" == "-l" ]]; then log="$arg"; fi
  [[ "$arg" == "-elaborate" ]] && elaborate=1
  prev="$arg"
done
[[ -n "$log" ]] || { echo "missing -l" >&2; exit 9; }
mkdir -p "$(dirname "$log")"
if [[ "$elaborate" == "1" ]]; then
  echo "compile and elaboration clean" > "$log"
  exit 0
fi
mkdir -p "$(dirname "$STAGE5_TRACE_OUTPUT")"
printf 'H\tGOLDEN\nG\tTS000001\t1\t10\ttop.u.mon\t0\t0\n' > "$STAGE5_TRACE_OUTPUT"
case "${F2A_FAKE_MODE:?}" in
  exact)
    printf '%s\n' \
      'CRC32 PASS: vector=cbf43926 signature=2d6352b3 last=5650ac83' \
      'EXIT SUCCESS' > "$log"
    exit 0
    ;;
  exit_only)
    echo 'EXIT SUCCESS' > "$log"
    exit 0
    ;;
  timeout)
    echo 'Simulation aborted due to maximum cycle limit' > "$log"
    exit 1
    ;;
  error)
    echo 'xmelab: *E,FAKE synthetic error' > "$log"
    exit 1
    ;;
  assertion_abort)
    cat > "$log" <<'EOF_ASSERT'
xmsim: *F,ASRTST: (/tmp/verification/shared/tb/mm_ram.sv,367): (time 780 NS) Assertion tb_top.wrapper_i.ram_i.out_of_bounds_write has failed
         X         0
Simulation terminated via $fatal(1) at time 780 NS + 11
/tmp/verification/shared/tb/mm_ram.sv:367   else $fatal("out of bounds write to %08x with %08x", data_addr_i, data_wdata_i);
EOF_ASSERT
    exit 2
    ;;
  *) exit 8 ;;
esac
''',
        executable=True,
    )
    return {
        "f2a": f2a,
        "cv": cv,
        "bin": bin_dir,
        "cell": root / "cell_model.v",
        "golden": root / "golden.v",
        "fault": root / "fault.json",
        "applier": f2a / "scripts/fault_characterization/fake_fault_applier.py",
        "common": f2a / "scripts/lib/xrun_stage5_common.sh",
    }


def run_case(
    tree: dict[str, Path],
    root: Path,
    name: str,
    phase: str,
    mode: str,
    expected_status: str,
    expected_rc: int,
    expect_work: bool,
    expect_bundle: bool,
    expect_trace: bool,
    *,
    run_kind: str = "golden",
    prepare_fail: bool = False,
    apply_fail: bool = False,
) -> None:
    run_dir = root / f"run_{name}"
    trace = root / f"trace_{name}.tsv"
    monitor = root / f"monitor_{name}.sv"
    write(monitor, f'module monitor; initial $display("{trace.resolve()}"); endmodule\n')
    harness = root / f"harness_{name}.sh"
    write(
        harness,
        f'''#!/usr/bin/env bash
set -euo pipefail
export F2A_ROOT={tree['f2a']}
export F2A_HOME={tree['f2a']}
export CV32E40P_HOME={tree['cv']}
export F2A_FAKE_BIN={tree['bin']}
export F2A_FAKE_CELL_MODEL={tree['cell']}
export F2A_FAKE_MODE={mode}
export F2A_FAKE_PREP_FAIL={'1' if prepare_fail else '0'}
export F2A_FAKE_APPLY_FAIL={'1' if apply_fail else '0'}
export F2A_FAKE_GOLDEN={tree['golden']}
export STAGE5_TRACE_OUTPUT={trace.resolve()}
RUN_KIND={run_kind}
DESIGN=cv32e40p
WORKLOAD=crc32
SIM_LEVEL=netlist
RUN_NAME={run_dir.name}
RUN_DIR={run_dir.resolve()}
EXTRA_SV_SOURCE={monitor.resolve()}
GOLDEN_NETLIST={tree['golden']}
FAULT_JSON={tree['fault']}
FAULT_ID=TF000001_SA0
STAGE5_FAULT_APPLIER={tree['applier']}
STAGE5_PHASE={phase}
MAXCYCLES=100
VCD=0
VERBOSE=0
KEEP_WORK=0
WRAPPER_COMMAND=synthetic_{name}
source {tree['common']}
set +e
f2a_stage5_run_xrun
rc=$?
set -e
exit "$rc"
''',
        executable=True,
    )
    completed = subprocess.run(
        [str(harness)],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ},
    )
    if completed.returncode != expected_rc:
        raise IntegrationError(
            f"{name}: return code expected={expected_rc}, actual={completed.returncode}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    if result["status"] != expected_status:
        raise IntegrationError(
            f"{name}: status expected={expected_status}, actual={result['status']}"
        )
    if result.get("schema_version") != "2.0":
        raise IntegrationError(f"{name}: result schema is not 2.0")
    if result.get("verdict_engine_version") != "3.0.0":
        raise IntegrationError(f"{name}: verdict engine is not 3.0.0")
    if expected_status == "EXISTING_ASSERTION_DETECTED":
        raw = result["raw_facts"]
        if raw["tool"]["status"] != "OK":
            raise IntegrationError(f"{name}: assertion abort was treated as tool error")
        if raw["execution"]["completion"] != "TERMINATED_BY_EXISTING_ASSERTION":
            raise IntegrationError(f"{name}: wrong assertion completion")
        if raw["workload"]["outcome"] != "NOT_REACHED":
            raise IntegrationError(f"{name}: assertion workload outcome not censored")
        if raw["workload"]["architectural_outcome"] != "CENSORED":
            raise IntegrationError(f"{name}: assertion architecture outcome not censored")
        if raw["existing_detector_baseline"]["event_count"] != 1:
            raise IntegrationError(f"{name}: assertion detector event missing")
    if (run_dir / "work").exists() != expect_work:
        raise IntegrationError(f"{name}: unexpected work retention")
    bundle_path = run_dir / "reproduction_bundle.tar.gz"
    if bundle_path.is_file() != expect_bundle:
        raise IntegrationError(f"{name}: unexpected bundle presence")
    if expect_bundle:
        import tarfile

        with tarfile.open(bundle_path, "r:gz") as archive:
            names = set(archive.getnames())
        required = {
            "README_REPRODUCE.txt",
            "reproduction_bundle_manifest.json",
            "run/result.json",
            "run/xrun.log",
            "run/command.txt",
            "run/stage5_monitor.sv",
            "run/wrapper_command.txt",
        }
        missing = sorted(required - names)
        if missing:
            raise IntegrationError(f"{name}: bundle missing entries: {missing}")
        forbidden = [
            item
            for item in names
            if item.endswith(("fault_netlist.v", "cv32e40p.mapped.sim.v", "riscy_tb.vcd"))
            or "/xcelium.d/" in item
        ]
        if forbidden:
            raise IntegrationError(f"{name}: bundle contains forbidden entries: {forbidden}")
    if trace.is_file() != expect_trace:
        raise IntegrationError(f"{name}: unexpected trace presence")
    print(f"Runner integration {name:18s}: PASS ({expected_status})")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--common", type=Path, required=True)
    parser.add_argument("--verdict", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        with tempfile.TemporaryDirectory(prefix="f2a_runner_integration_") as temporary:
            root = Path(temporary)
            tree = make_tree(root, args.common.resolve(), args.verdict.resolve(), args.bundle.resolve())
            run_case(tree, root, "compile", "compile", "exact", "COMPILE_PASS", 0, False, False, False)
            run_case(
                tree, root, "fault_compile", "compile", "exact",
                "COMPILE_PASS", 0, False, False, False, run_kind="fault"
            )
            run_case(tree, root, "exact_pass", "run", "exact", "PASS", 0, False, False, True)
            run_case(
                tree, root, "fault_exact", "run", "exact",
                "OUTPUT_MATCH", 0, False, False, True, run_kind="fault"
            )
            run_case(tree, root, "exit_only", "run", "exit_only", "UNKNOWN", 3, True, True, True)
            run_case(tree, root, "timeout", "run", "timeout", "TIMEOUT", 2, True, True, True, run_kind="fault")
            run_case(
                tree,
                root,
                "assertion_abort",
                "run",
                "assertion_abort",
                "EXISTING_ASSERTION_DETECTED",
                2,
                True,
                True,
                True,
                run_kind="fault",
            )
            run_case(tree, root, "infra_error", "run", "error", "ERROR", 4, True, True, True)
            run_case(
                tree,
                root,
                "preflight_prepare",
                "compile",
                "exact",
                "COMPILE_ERROR",
                4,
                True,
                True,
                False,
                prepare_fail=True,
            )
            run_case(
                tree,
                root,
                "preflight_materialize",
                "compile",
                "exact",
                "COMPILE_ERROR",
                4,
                True,
                True,
                False,
                run_kind="fault",
                apply_fail=True,
            )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("Stage-5 runner integration self-test     : PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
