#!/usr/bin/env bash
set -euo pipefail
F2A_ROOT="${F2A_ROOT:-/raid/spring2026/fwu44/research/fault2assertion}"
FC="$F2A_ROOT/scripts/fault_characterization"
PHASE_ROOT="$F2A_ROOT/runs/stage5_dev/phase23_smoke_v1"
LOCK="$F2A_ROOT/runs/stage5_locks/phase23_smoke_execution_inputs.json"
META="$PHASE_ROOT/provenance/smoke_metadata.json"
COMPILE_REPORT="$PHASE_ROOT/reports/phase23_compile_validation.json"
GOLDEN_REPORT="$PHASE_ROOT/reports/phase23_golden_validation.json"
RUN_ROOT="$PHASE_ROOT/fault_runs"
REPORT_ROOT="$PHASE_ROOT/reports/modes"
SUMMARY="$PHASE_ROOT/reports/phase2_three_mode_smoke.json"
WRAPPER="$F2A_ROOT/scripts/run_xrun_stage5_fault.sh"
VALIDATOR="$FC/stage5_diagnostic_run_validate.py"
COMMON="$FC/stage5_gate_validation_common.py"
LOCK_TOOL="$FC/stage5_execution_input_lock.py"
fail(){ echo "ERROR: $*" >&2; exit 1; }
log(){ printf '\n[%s] %s\n' "$(date '+%F %T')" "$*"; }
cd "$F2A_ROOT"
[[ ! -e "$RUN_ROOT" ]] || fail "three-mode run root exists: $RUN_ROOT"
[[ ! -e "$SUMMARY" ]] || fail "three-mode summary exists: $SUMMARY"
python3 - "$COMPILE_REPORT" "$GOLDEN_REPORT" <<'PY'
import json,sys
from pathlib import Path
for raw in sys.argv[1:]:
 p=Path(raw); assert json.loads(p.read_text()).get("status")=="PASS", p
PY
python3 "$LOCK_TOOL" verify --lock "$LOCK"
readarray -t M < <(python3 - "$META" <<'PY'
import json,sys
from pathlib import Path
m=json.loads(Path(sys.argv[1]).read_text()); print(m["fault_id"]); print(m["fault_spec"])
PY
)
FAULT_ID="${M[0]}"; FAULT_JSON="${M[1]}"
mkdir -p "$RUN_ROOT" "$REPORT_ROOT"
run_mode(){
  local mode="$1" purpose="$2"
  local monitor="$PHASE_ROOT/monitors/faults/${FAULT_ID}.${mode}.sv"
  local trace="$PHASE_ROOT/traces/${FAULT_ID}.${mode}.trace.tsv"
  local run="$RUN_ROOT/${mode}"
  local report="$REPORT_ROOT/${mode}.json"
  log "Run ${FAULT_ID} in ${mode} mode (${purpose})"
  set +e
  STAGE5_PHASE=run STAGE5_RUN_PURPOSE="$purpose" STAGE5_TRACE_OUTPUT="$trace" \
  MAXCYCLES=2000000 VCD=0 KEEP_WORK=0 \
  "$WRAPPER" "$FAULT_JSON" "$monitor" "$run"
  local rc=$?
  set -e
  if [[ "$rc" -ne 0 && "$rc" -ne 2 ]]; then
    fail "${mode} runner returned infrastructure/unknown status ${rc}; inspect ${run}"
  fi
  python3 "$VALIDATOR" \
    --common "$COMMON" \
    --compile-report "$COMPILE_REPORT" \
    --run-dir "$run" \
    --trace "$trace" \
    --fault-id "$FAULT_ID" \
    --purpose "$purpose" \
    --require-detector-event \
    --report "$report"
}
run_mode native NATIVE_CHARACTERIZATION
run_mode observe DIAGNOSTIC_OBSERVE
run_mode diagnostic_quarantine DIAGNOSTIC_QUARANTINE
log "Check the smoke-specific three-mode completion contract"
python3 - "$REPORT_ROOT" "$SUMMARY" <<'PY'
import json,sys
from datetime import datetime,timezone
from pathlib import Path
root,out=map(Path,sys.argv[1:])
r={name:json.loads((root/f"{name}.json").read_text()) for name in ("native","observe","diagnostic_quarantine")}
if r["native"]["runner_status"] != "EXISTING_ASSERTION_DETECTED":
 raise SystemExit("ERROR: smoke native run did not reproduce the existing assertion")
if r["native"]["execution_completion"] != "TERMINATED_BY_EXISTING_ASSERTION":
 raise SystemExit("ERROR: native completion boundary mismatch")
if r["observe"]["intervention"]["transaction_quarantine"] is not False:
 raise SystemExit("ERROR: observe mode incorrectly quarantined transaction")
if r["diagnostic_quarantine"]["intervention"]["transaction_quarantine"] is not True:
 raise SystemExit("ERROR: quarantine mode did not quarantine transaction")
payload={
 "schema_version":"1.0","generated_at_utc":datetime.now(timezone.utc).isoformat(),
 "kind":"stage5_phase2_three_mode_smoke","status":"PASS","modes":r,
 "contracts":{
  "native_defines_natural_completion_boundary":True,
  "observe_suppresses_fatal_only":True,
  "quarantine_acknowledges_and_drops_unsafe_write":True,
  "diagnostic_outcomes_are_counterfactual_after_first_event":True,
 }
}
out.write_text(json.dumps(payload,indent=2)+"\n")
print("Three-mode smoke contract: PASS")
PY
printf '\nPhase-2 three-mode smoke: PASS\nSummary: %s\n' "$SUMMARY"
