#!/usr/bin/env bash
set -euo pipefail
F2A_ROOT="${F2A_ROOT:-/raid/spring2026/fwu44/research/fault2assertion}"
FC="$F2A_ROOT/scripts/fault_characterization"
PHASE_ROOT="$F2A_ROOT/runs/stage5_dev/phase23_smoke_v1"
LOCK="$F2A_ROOT/runs/stage5_locks/phase23_smoke_execution_inputs.json"
META="$PHASE_ROOT/provenance/smoke_metadata.json"
STATIC_REPORT="$PHASE_ROOT/reports/phase23_static_validation.json"
COMPILE_ROOT="$PHASE_ROOT/compile"
REPORT="$PHASE_ROOT/reports/phase23_compile_validation.json"
COMMON="$FC/stage5_gate_validation_common.py"
VALIDATOR="$FC/stage5_phase23_compile_validate.py"
GOLDEN_WRAPPER="$F2A_ROOT/scripts/run_xrun_stage5_golden.sh"
FAULT_WRAPPER="$F2A_ROOT/scripts/run_xrun_stage5_fault.sh"
LOCK_TOOL="$FC/stage5_execution_input_lock.py"
log(){ printf '\n[%s] %s\n' "$(date '+%F %T')" "$*"; }
fail(){ echo "ERROR: $*" >&2; exit 1; }
cd "$F2A_ROOT"
[[ ! -e "$COMPILE_ROOT" ]] || fail "compile workspace exists: $COMPILE_ROOT"
[[ ! -e "$REPORT" ]] || fail "compile report exists: $REPORT"
python3 - "$STATIC_REPORT" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]); v=json.loads(p.read_text())
assert v.get("status")=="PASS", "static report not PASS"
PY
python3 "$LOCK_TOOL" verify --lock "$LOCK"
readarray -t M < <(python3 - "$META" <<'PY'
import json,sys
from pathlib import Path
m=json.loads(Path(sys.argv[1]).read_text()); print(m["fault_id"]); print(m["fault_spec"]); print(m["mapped_netlist"])
PY
)
FAULT_ID="${M[0]}"; FAULT_JSON="${M[1]}"; GOLDEN_NETLIST="${M[2]}"
mkdir -p "$COMPILE_ROOT"
GOLDEN_TRACE="$PHASE_ROOT/traces/golden_all.trace.tsv"
GOLDEN_MONITOR="$PHASE_ROOT/monitors/stage5_golden_monitor.sv"
GOLDEN_RUN="$COMPILE_ROOT/golden"
log "Compile/elaborate golden plus mode-aware testbench"
STAGE5_PHASE=compile STAGE5_RUN_PURPOSE=COMPILE_CHECK STAGE5_TRACE_OUTPUT="$GOLDEN_TRACE" \
GOLDEN_NETLIST="$GOLDEN_NETLIST" MAXCYCLES=2000000 VCD=0 KEEP_WORK=0 \
"$GOLDEN_WRAPPER" "$GOLDEN_MONITOR" "$GOLDEN_RUN"
for mode in native observe diagnostic_quarantine; do
  monitor="$PHASE_ROOT/monitors/faults/${FAULT_ID}.${mode}.sv"
  trace="$PHASE_ROOT/traces/${FAULT_ID}.${mode}.trace.tsv"
  run="$COMPILE_ROOT/${mode}"
  log "Compile/elaborate exact ${mode} fault monitor"
  STAGE5_PHASE=compile STAGE5_RUN_PURPOSE=COMPILE_CHECK STAGE5_TRACE_OUTPUT="$trace" \
  MAXCYCLES=2000000 VCD=0 KEEP_WORK=0 \
  "$FAULT_WRAPPER" "$FAULT_JSON" "$monitor" "$run"
done
log "Validate all four compile/elaboration runs"
python3 "$VALIDATOR" \
  --common "$COMMON" \
  --golden-run "$GOLDEN_RUN" \
  --native-run "$COMPILE_ROOT/native" \
  --observe-run "$COMPILE_ROOT/observe" \
  --quarantine-run "$COMPILE_ROOT/diagnostic_quarantine" \
  --golden-trace "$GOLDEN_TRACE" \
  --native-trace "$PHASE_ROOT/traces/${FAULT_ID}.native.trace.tsv" \
  --observe-trace "$PHASE_ROOT/traces/${FAULT_ID}.observe.trace.tsv" \
  --quarantine-trace "$PHASE_ROOT/traces/${FAULT_ID}.diagnostic_quarantine.trace.tsv" \
  --report "$REPORT"
printf '\nPhase-2 compile/elaboration: PASS\nReport: %s\n' "$REPORT"
