#!/usr/bin/env bash
set -euo pipefail

F2A_ROOT="${F2A_ROOT:-/raid/spring2026/fwu44/research/fault2assertion}"
FC="$F2A_ROOT/scripts/fault_characterization"
MINI_ROOT="$F2A_ROOT/runs/stage5_dev/mini_smoke_v1"
SMOKE_SELECTION="$MINI_ROOT/provenance/smoke_fault_selection.json"
FAULT_MONITOR_ROOT="$MINI_ROOT/monitors/faults"
FAULT_MANIFEST_ROOT="$MINI_ROOT/manifests/faults"
LOCK="$F2A_ROOT/runs/stage5_locks/mini_gate2_precompile_lock.json"
EXECUTION_INPUT_LOCK="$F2A_ROOT/runs/stage5_locks/mini_gate2_execution_inputs.json"
GATE2_REPORT="$MINI_ROOT/reports/gate2_compile_validation.json"
GATE3_REPORT="$MINI_ROOT/reports/gate3_golden_validation.json"
GATE4_ROOT="$MINI_ROOT/gate4_fault"
REPORT="$MINI_ROOT/reports/gate4_single_fault_validation.json"

LOCK_VERIFY="$FC/stage5_lock_verify.py"
EXECUTION_INPUT_TOOL="$FC/stage5_execution_input_lock.py"
COMMON_VALIDATE="$FC/stage5_gate_validation_common.py"
GATE4_VALIDATE="$FC/stage5_gate4_validate.py"
FAULT_WRAPPER="$F2A_ROOT/scripts/run_xrun_stage5_fault.sh"

log() {
    printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

create_fresh_gate_root() {
    case "$GATE4_ROOT" in
        "$F2A_ROOT"/runs/stage5_dev/mini_smoke_v1/gate4_fault) ;;
        *) fail "unsafe Gate-4 root: $GATE4_ROOT" ;;
    esac
    if [[ -e "$GATE4_ROOT" ]]; then
        fail "Gate-4 workspace already exists. Preserve or archive it before an intentional rerun: $GATE4_ROOT"
    fi
    if [[ -e "$REPORT" ]]; then
        fail "Gate-4 report already exists. Preserve or archive it before an intentional rerun: $REPORT"
    fi
    mkdir -p "$GATE4_ROOT"
}

cd "$F2A_ROOT"

log "Verify Gate 3 passed and the frozen local code state still matches"
python3 - "$GATE3_REPORT" <<'PY'
import json
import sys
from pathlib import Path
path = Path(sys.argv[1])
if not path.is_file():
    raise SystemExit(f"ERROR: Gate-3 report not found: {path}")
payload = json.loads(path.read_text(encoding="utf-8"))
if payload.get("status") != "PASS":
    raise SystemExit("ERROR: Gate-3 report is not PASS")
PY
python3 "$LOCK_VERIFY" --repo-root "$F2A_ROOT" --lock "$LOCK"
python3 "$EXECUTION_INPUT_TOOL" verify --lock "$EXECUTION_INPUT_LOCK"

readarray -t META < <(
python3 - "$SMOKE_SELECTION" "$FAULT_MANIFEST_ROOT" <<'PY'
import json
import sys
from pathlib import Path
selection = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
fault_id = selection["fault_id"]
manifest_path = Path(sys.argv[2]) / f"{fault_id}.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
print(fault_id)
print(Path(selection["fault_spec"]).resolve())
print(Path(manifest["trace_output"]).resolve())
PY
)
[[ "${#META[@]}" -eq 3 ]] || fail "failed to resolve Gate-4 metadata"
FAULT_ID="${META[0]}"
FAULT_JSON="${META[1]}"
FAULT_TRACE="${META[2]}"
FAULT_MONITOR="$FAULT_MONITOR_ROOT/${FAULT_ID}.sv"
FAULT_RUN="$GATE4_ROOT/$FAULT_ID"

for file in "$SMOKE_SELECTION" "$FAULT_JSON" "$FAULT_MONITOR"; do
    [[ -s "$file" ]] || fail "missing Gate-4 input: $file"
done

log "Create a fresh Gate-4 workspace without deleting prior failure evidence"
create_fresh_gate_root
case "$FAULT_TRACE" in
    "$MINI_ROOT"/traces/TF??????_SA?.trace.tsv) ;;
    *) fail "unexpected fault trace path: $FAULT_TRACE" ;;
esac
[[ ! -e "$FAULT_TRACE" ]] \
    || fail "fault trace already exists; preserve or remove it intentionally before rerun: $FAULT_TRACE"

log "Gate 4: execute one deterministic run-local stuck-at fault"
set +e
STAGE5_PHASE=run \
STAGE5_RUN_PURPOSE=NATIVE_CHARACTERIZATION \
STAGE5_TRACE_OUTPUT="$FAULT_TRACE" \
MAXCYCLES=2000000 \
VCD=0 \
KEEP_WORK=0 \
"$FAULT_WRAPPER" "$FAULT_JSON" "$FAULT_MONITOR" "$FAULT_RUN"
RUNNER_STATUS=$?
set -e

# 0 = OUTPUT_MATCH.  2 = a valid non-matching/censored fault observation:
# OUTPUT_MISMATCH, TIMEOUT, or EXISTING_ASSERTION_DETECTED.  Other return
# values are infrastructure/unknown failures and are rejected below.
if [[ "$RUNNER_STATUS" -ne 0 && "$RUNNER_STATUS" -ne 2 ]]; then
    echo "ERROR: fault runner returned invalid or infrastructure status $RUNNER_STATUS" >&2
    echo "Inspect: $FAULT_RUN" >&2
fi

log "Fail-closed Gate-4 native-execution, raw-fact, trace, retention, and bundle validation"
python3 "$GATE4_VALIDATE" \
    --common "$COMMON_VALIDATE" \
    --selection-record "$SMOKE_SELECTION" \
    --gate2-report "$GATE2_REPORT" \
    --run-dir "$FAULT_RUN" \
    --trace "$FAULT_TRACE" \
    --report "$REPORT"

log "Gate 4 completed successfully"
echo "Fault ID           : $FAULT_ID"
echo "Fault run          : $FAULT_RUN"
echo "Fault trace        : $FAULT_TRACE"
echo "Gate-4 report      : $REPORT"
echo "Native observation : $(cat "$FAULT_RUN/result.txt")"
echo "Do not delete the fault trace/work until Phase 2 diagnostic-oracle design is validated."
