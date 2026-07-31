#!/usr/bin/env bash
set -euo pipefail

F2A_ROOT="${F2A_ROOT:-/raid/spring2026/fwu44/research/fault2assertion}"
FC="$F2A_ROOT/scripts/fault_characterization"
PHASE_ROOT="$F2A_ROOT/runs/stage5_dev/phase23_smoke_v1"
META="$PHASE_ROOT/provenance/smoke_metadata.json"
POLICY="$F2A_ROOT/platform/cv32e40p/stage5_assertion_policy_v1.json"
LOCK="$F2A_ROOT/runs/stage5_locks/phase23_smoke_execution_inputs.json"
STATIC_REPORT="$PHASE_ROOT/reports/phase23_static_validation.json"
INVENTORY_REPORT="$PHASE_ROOT/static/preexisting_detector_inventory.json"
COMPILE_REPORT="$PHASE_ROOT/reports/phase23_compile_validation.json"
GOLDEN_REPORT="$PHASE_ROOT/reports/phase23_golden_validation.json"
MODE_SUMMARY="$PHASE_ROOT/reports/phase2_three_mode_smoke.json"
ORACLE_ROOT="$PHASE_ROOT/oracle"
ORACLE_JSON="$ORACLE_ROOT/oracle.json"
ORACLE_TXT="$ORACLE_ROOT/oracle.txt"
VALIDATION_LOG="$ORACLE_ROOT/oracle_validation.log"
CLEANUP_REPORT="$ORACLE_ROOT/post_oracle_cleanup.json"
ORACLE_LOCK="$F2A_ROOT/runs/stage5_locks/phase23_smoke_multidim_oracle.json"
ANALYZER="$FC/stage5_multidim_oracle.py"
VALIDATOR="$FC/stage5_multidim_oracle_validate.py"
LOCK_TOOL="$FC/stage5_execution_input_lock.py"
ARTIFACT_LOCK="$FC/stage5_artifact_lock.py"

fail(){ echo "ERROR: $*" >&2; exit 1; }
log(){ printf '\n[%s] %s\n' "$(date '+%F %T')" "$*"; }
require_file(){ [[ -s "$1" ]] || fail "missing or empty file: $1"; }

cd "$F2A_ROOT"
[[ ! -e "$ORACLE_ROOT" ]] || fail "oracle root exists: $ORACLE_ROOT"
[[ ! -e "$ORACLE_LOCK" ]] || fail "oracle lock exists: $ORACLE_LOCK"
for file in "$META" "$POLICY" "$STATIC_REPORT" "$INVENTORY_REPORT" "$COMPILE_REPORT" "$GOLDEN_REPORT" "$MODE_SUMMARY" "$ANALYZER" "$VALIDATOR"; do
  require_file "$file"
done
python3 - "$COMPILE_REPORT" "$GOLDEN_REPORT" "$MODE_SUMMARY" <<'PY'
import json,sys
from pathlib import Path
for raw in sys.argv[1:]:
 p=Path(raw); value=json.loads(p.read_text())
 if value.get("status") != "PASS": raise SystemExit(f"ERROR: prerequisite not PASS: {p}")
PY
python3 "$LOCK_TOOL" verify --lock "$LOCK"

readarray -t M < <(python3 - "$META" <<'PY'
import json,sys
from pathlib import Path
m=json.loads(Path(sys.argv[1]).read_text())
print(m["fault_id"]); print(m["selection_id"]); print(m["fault_spec"])
PY
)
FAULT_ID="${M[0]}"; SELECTION_ID="${M[1]}"; FAULT_JSON="${M[2]}"
GOLDEN_TRACE="$PHASE_ROOT/golden/site_traces/${SELECTION_ID}.trace.tsv.gz"
NATIVE_RUN="$PHASE_ROOT/fault_runs/native"
OBSERVE_RUN="$PHASE_ROOT/fault_runs/observe"
QUARANTINE_RUN="$PHASE_ROOT/fault_runs/diagnostic_quarantine"
NATIVE_TRACE="$PHASE_ROOT/traces/${FAULT_ID}.native.trace.tsv"
OBSERVE_TRACE="$PHASE_ROOT/traces/${FAULT_ID}.observe.trace.tsv"
QUARANTINE_TRACE="$PHASE_ROOT/traces/${FAULT_ID}.diagnostic_quarantine.trace.tsv"
for file in "$FAULT_JSON" "$GOLDEN_TRACE" "$NATIVE_TRACE" "$OBSERVE_TRACE" "$QUARANTINE_TRACE"; do require_file "$file"; done
mkdir -p "$ORACLE_ROOT"

log "Build the Phase-3 multidimensional oracle"
python3 "$ANALYZER" \
  --fault-json "$FAULT_JSON" \
  --assertion-policy "$POLICY" \
  --golden-trace "$GOLDEN_TRACE" \
  --native-run "$NATIVE_RUN" \
  --native-trace "$NATIVE_TRACE" \
  --observe-run "$OBSERVE_RUN" \
  --observe-trace "$OBSERVE_TRACE" \
  --quarantine-run "$QUARANTINE_RUN" \
  --quarantine-trace "$QUARANTINE_TRACE" \
  --oracle-output "$ORACLE_JSON" \
  --report-output "$ORACLE_TXT"

log "Independently replay and validate the oracle from original evidence"
python3 "$VALIDATOR" \
  --oracle "$ORACLE_JSON" \
  --analyzer "$ANALYZER" \
  2>&1 | tee "$VALIDATION_LOG"

grep -q 'Multidimensional oracle validation: PASS' "$VALIDATION_LOG" \
  || fail "oracle replay validation did not report PASS"

log "Remove only run-local work/netlists after oracle replay validation"
python3 - "$CLEANUP_REPORT" "$NATIVE_RUN" "$OBSERVE_RUN" "$QUARANTINE_RUN" <<'PY'
import json, shutil, sys
from datetime import datetime, timezone
from pathlib import Path
out=Path(sys.argv[1]); runs=[Path(x).resolve() for x in sys.argv[2:]]
removed=[]
for run in runs:
    work=run/"work"
    if work.exists():
        shutil.rmtree(work)
        removed.append(str(work))
    if work.exists(): raise SystemExit(f"ERROR: failed to remove {work}")
payload={
 "schema_version":"1.0","generated_at_utc":datetime.now(timezone.utc).isoformat(),
 "kind":"stage5_phase3_post_oracle_cleanup","status":"PASS",
 "removed_work_directories":removed,
 "retained_compact_evidence":[
   "result.json","xrun.log","assertion_events.tsv","compact fault traces",
   "golden split trace","multidimensional oracle"
 ],
 "vcd_retained":False,"fault_netlist_retained":False,
}
out.write_text(json.dumps(payload,indent=2)+"\n",encoding="utf-8")
print("Post-oracle run-local cleanup: PASS")
PY

log "Freeze durable smoke oracle and all replay evidence"
ARGS=(
  --kind stage5_phase23_smoke_multidimensional_oracle
  --file "oracle_json=$ORACLE_JSON"
  --file "oracle_report=$ORACLE_TXT"
  --file "oracle_validation=$VALIDATION_LOG"
  --file "cleanup_report=$CLEANUP_REPORT"
  --file "fault_spec=$FAULT_JSON"
  --file "assertion_policy=$POLICY"
  --file "golden_trace=$GOLDEN_TRACE"
  --file "native_trace=$NATIVE_TRACE"
  --file "observe_trace=$OBSERVE_TRACE"
  --file "quarantine_trace=$QUARANTINE_TRACE"
  --file "native_result=$NATIVE_RUN/result.json"
  --file "observe_result=$OBSERVE_RUN/result.json"
  --file "quarantine_result=$QUARANTINE_RUN/result.json"
  --file "native_events=$NATIVE_RUN/assertion_events.tsv"
  --file "observe_events=$OBSERVE_RUN/assertion_events.tsv"
  --file "quarantine_events=$QUARANTINE_RUN/assertion_events.tsv"
  --file "native_log=$NATIVE_RUN/xrun.log"
  --file "observe_log=$OBSERVE_RUN/xrun.log"
  --file "quarantine_log=$QUARANTINE_RUN/xrun.log"
  --file "static_report=$STATIC_REPORT"
  --file "detector_inventory=$INVENTORY_REPORT"
  --file "compile_report=$COMPILE_REPORT"
  --file "golden_report=$GOLDEN_REPORT"
  --file "mode_summary=$MODE_SUMMARY"
  --file "execution_input_lock=$LOCK"
  --file "oracle_analyzer=$ANALYZER"
  --file "oracle_validator=$VALIDATOR"
  --output "$ORACLE_LOCK"
  --force
)
python3 "$ARTIFACT_LOCK" create "${ARGS[@]}"
python3 "$ARTIFACT_LOCK" verify --lock "$ORACLE_LOCK"

log "Final Phase-3 smoke assertions"
python3 - "$ORACLE_JSON" <<'PY'
import json,sys
from pathlib import Path
o=json.loads(Path(sys.argv[1]).read_text())
d=o["dimensions"]; g=o["guardrails"]
assert d["execution_validity"]=="VALID"
assert d["activation_class"]=="ACTIVATED"
assert d["injection_class"]=="EFFECTIVE"
assert d["propagation_class"]=="ARCHITECTURAL_INTERFACE_REACHED"
assert "ILLEGAL_MEMORY_WRITE" in d["effect_classes"]
assert d["native_architectural_outcome"]=="CENSORED"
assert d["diagnostic_continuation_available"] is True
assert g["native_run_defines_natural_completion_boundary"] is True
assert g["quarantine_outcome_is_not_natural_architectural_outcome"] is True
assert g["sva_seed_generated"] is False
print("Phase-3 multidimensional smoke oracle: PASS")
PY

printf '\nPhase-3 multidimensional smoke oracle: PASS\n'
printf 'Oracle JSON : %s\n' "$ORACLE_JSON"
printf 'Oracle lock : %s\n' "$ORACLE_LOCK"
printf 'Compact traces are retained for replay; VCD and run-local netlists are not retained.\n'
