#!/usr/bin/env bash
set -euo pipefail

F2A_ROOT="${F2A_ROOT:-/raid/spring2026/fwu44/research/fault2assertion}"

FC="$F2A_ROOT/scripts/fault_characterization"
MINI_ROOT="$F2A_ROOT/runs/stage5_dev/mini_smoke_v1"
PHASE2_ROOT="$F2A_ROOT/runs/stage5_dev/phase2_v1"
G3_ROOT="$PHASE2_ROOT/g3_observe"
G4_ROOT="$PHASE2_ROOT/g4_diagnostic_quarantine"

POLICY="$F2A_ROOT/platform/cv32e40p/stage5_phase2_execution_policy_v1.json"
MODE_TOOL="$FC/stage5_phase2_modes.py"
G4_VALIDATOR="$FC/stage5_phase2_g4_validate.py"

SMOKE_SELECTION="$MINI_ROOT/provenance/smoke_fault_selection.json"
FAULT_MONITOR_ROOT="$MINI_ROOT/monitors/faults"
FAULT_MANIFEST_ROOT="$MINI_ROOT/manifests/faults"

G3_REPORT="$G3_ROOT/phase2_g3_validation.json"
FAULT_WRAPPER="$F2A_ROOT/scripts/run_xrun_stage5_fault.sh"

QUARANTINE_MONITOR="$G4_ROOT/generated_monitors/fault_diagnostic_quarantine_runtime.sv"
QUARANTINE_METADATA="$G4_ROOT/mode_metadata/fault_diagnostic_quarantine_runtime.json"
QUARANTINE_TRACE="$G4_ROOT/traces/fault_diagnostic_quarantine_runtime.trace.tsv"
QUARANTINE_RUN="$G4_ROOT/runs/fault_diagnostic_quarantine_runtime"
REPORT="$G4_ROOT/phase2_g4_validation.json"


log() {
    printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}


fail() {
    echo "ERROR: $*" >&2
    return 1
}


require_file() {
    local path="$1"
    local label="$2"
    [[ -s "$path" ]] || fail "$label not found or empty: $path"
}


create_fresh_root() {
    case "$G4_ROOT" in
        "$F2A_ROOT"/runs/stage5_dev/phase2_v1/g4_diagnostic_quarantine)
            ;;
        *)
            fail "unsafe Phase2-G4 root: $G4_ROOT"
            return 1
            ;;
    esac

    if [[ -e "$G4_ROOT" ]]; then
        fail "Phase2-G4 workspace already exists: $G4_ROOT"
        return 1
    fi

    mkdir -p \
        "$G4_ROOT/generated_monitors" \
        "$G4_ROOT/mode_metadata" \
        "$G4_ROOT/runs" \
        "$G4_ROOT/traces"
}


cd "$F2A_ROOT"

log "Validate the frozen G3 OBSERVE checkpoint"

require_file "$G3_REPORT" "Phase2-G3 report"

python3 - "$G3_REPORT" <<'PY'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if report.get("status") != "PASS":
    raise SystemExit("ERROR: Phase2-G3 report is not PASS")

claims = report.get("gate_claims", {})
required_true = (
    "g2_native_sanity_passed",
    "diagnostic_observe_executed",
    "diagnostic_profile_used",
    "original_target_assertion_removed",
    "procedural_first_event_detector_recorded_once",
    "existing_detector_is_out_of_bounds_write",
    "detector_action_is_record_only",
    "termination_suppressed",
    "observe_result_is_valid_scientific_outcome",
)
for key in required_true:
    if claims.get(key) is not True:
        raise SystemExit(f"ERROR: Phase2-G3 claim is not true: {key}")

if claims.get("transaction_quarantine_enabled") is not False:
    raise SystemExit("ERROR: Phase2-G3 OBSERVE unexpectedly enabled quarantine")
if claims.get("diagnostic_quarantine_runtime_executed") is not False:
    raise SystemExit("ERROR: Phase2-G3 unexpectedly executed DIAGNOSTIC_QUARANTINE")
if claims.get("final_diagnostic_oracle_assigned") is not False:
    raise SystemExit("ERROR: Phase2-G3 unexpectedly assigned a final oracle")

print("Phase2-G3 prerequisite: PASS")
PY

log "Validate the minimal G4 inputs"

for file in \
    "$POLICY" \
    "$MODE_TOOL" \
    "$G4_VALIDATOR" \
    "$SMOKE_SELECTION" \
    "$G3_REPORT" \
    "$FAULT_WRAPPER"
do
    require_file "$file" "Phase2-G4 input"
done

python3 -m json.tool "$POLICY" >/dev/null
python3 -m py_compile "$MODE_TOOL" "$G4_VALIDATOR"
bash -n "$FAULT_WRAPPER"

readarray -t META < <(
python3 - \
    "$G3_REPORT" \
    "$SMOKE_SELECTION" \
    "$FAULT_MONITOR_ROOT" \
    "$FAULT_MANIFEST_ROOT" <<'PY'
import json
import sys
from pathlib import Path

g3 = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
selection = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))

fault_id = str(g3["fault_id"])
if str(selection["fault_id"]) != fault_id:
    raise SystemExit("ERROR: G3 and smoke selection fault IDs differ")

fault_json = Path(str(selection["fault_spec"])).resolve()
fault_monitor = Path(sys.argv[3]).resolve() / f"{fault_id}.sv"
fault_manifest = Path(sys.argv[4]).resolve() / f"{fault_id}.json"

print(fault_id)
print(fault_json)
print(fault_monitor)
print(fault_manifest)
PY
)

[[ "${#META[@]}" -eq 4 ]] || fail "failed to resolve Phase2-G4 metadata"

FAULT_ID="${META[0]}"
FAULT_JSON="${META[1]}"
FAULT_BASE_MONITOR="${META[2]}"
FAULT_BASE_MANIFEST="${META[3]}"

for file in \
    "$FAULT_JSON" \
    "$FAULT_BASE_MONITOR" \
    "$FAULT_BASE_MANIFEST"
do
    require_file "$file" "resolved Phase2-G4 input"
done

log "Create a fresh Phase2-G4 workspace"
create_fresh_root

log "Generate the QUARANTINE fault monitor"

python3 "$MODE_TOOL" compose \
    --policy "$POLICY" \
    --base-monitor "$FAULT_BASE_MONITOR" \
    --base-manifest "$FAULT_BASE_MANIFEST" \
    --mode QUARANTINE \
    --trace-output "$QUARANTINE_TRACE" \
    --output-monitor "$QUARANTINE_MONITOR" \
    --output-metadata "$QUARANTINE_METADATA"

log "Run the fault in DIAGNOSTIC_QUARANTINE"

set +e
STAGE5_PHASE=run \
STAGE5_RUN_PURPOSE=DIAGNOSTIC_QUARANTINE \
STAGE5_MM_RAM_PROFILE=diagnostic \
STAGE5_TRACE_OUTPUT="$QUARANTINE_TRACE" \
MAXCYCLES=2000000 \
VCD=0 \
KEEP_WORK=0 \
"$FAULT_WRAPPER" "$FAULT_JSON" "$QUARANTINE_MONITOR" "$QUARANTINE_RUN"
QUARANTINE_WRAPPER_STATUS=$?
set -e

if [[ "$QUARANTINE_WRAPPER_STATUS" -ne 0 \
      && "$QUARANTINE_WRAPPER_STATUS" -ne 2 ]]; then
    fail \
        "DIAGNOSTIC_QUARANTINE wrapper returned infrastructure/unknown status ${QUARANTINE_WRAPPER_STATUS}"
fi

require_file "$QUARANTINE_RUN/result.txt" "DIAGNOSTIC_QUARANTINE result.txt"
QUARANTINE_RESULT="$(cat "$QUARANTINE_RUN/result.txt")"

case "$QUARANTINE_RESULT" in
    DIAGNOSTIC_OUTPUT_MATCH|DIAGNOSTIC_OUTPUT_MISMATCH|DIAGNOSTIC_TIMEOUT)
        ;;
    *)
        fail "invalid DIAGNOSTIC_QUARANTINE result: $QUARANTINE_RESULT"
        ;;
esac

require_file "$QUARANTINE_RUN/result.json" "DIAGNOSTIC_QUARANTINE result.json"
require_file "$QUARANTINE_TRACE" "DIAGNOSTIC_QUARANTINE compact trace"
require_file "$QUARANTINE_RUN/assertion_events.tsv" "DIAGNOSTIC_QUARANTINE assertion events"
require_file "$QUARANTINE_RUN/mm_ram_preparation.json" "DIAGNOSTIC_QUARANTINE preparation report"
require_file "$QUARANTINE_RUN/mm_ram_ownership.json" "DIAGNOSTIC_QUARANTINE ownership report"

log "Validate the minimal G4 DIAGNOSTIC_QUARANTINE contract"

python3 "$G4_VALIDATOR" \
    --g3-report "$G3_REPORT" \
    --run-dir "$QUARANTINE_RUN" \
    --trace "$QUARANTINE_TRACE" \
    --metadata "$QUARANTINE_METADATA" \
    --fault-id "$FAULT_ID" \
    --report "$REPORT"

readarray -t SUMMARY < <(
python3 - "$REPORT" <<'PY'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
quarantine = report["diagnostic_quarantine"]
print(quarantine["runner_status"])
print(quarantine["quarantine_outcome"])
print(quarantine["execution_completion"])
print(quarantine["architectural_outcome"])
PY
)

QUARANTINE_RESULT="${SUMMARY[0]}"
QUARANTINE_OUTCOME="${SUMMARY[1]}"
QUARANTINE_COMPLETION="${SUMMARY[2]}"
QUARANTINE_ARCHITECTURAL_OUTCOME="${SUMMARY[3]}"

log "Phase2-G4 completed successfully"

echo
echo "======================================================================"
echo "Phase2-G4 PASS: minimal DIAGNOSTIC_QUARANTINE experiment"
echo "======================================================================"
echo "Fault ID                         : $FAULT_ID"
echo "Execution mode                   : QUARANTINE"
echo "Execution purpose                : DIAGNOSTIC_QUARANTINE"
echo "Assertion mode                   : diagnostic_quarantine"
echo "mm_ram profile                   : diagnostic"
echo "Existing detector                : out_of_bounds_write"
echo "Detector action                  : RECORD_AND_QUARANTINE"
echo "Termination suppressed           : YES"
echo "Transaction quarantine           : YES"
echo "DIAGNOSTIC_QUARANTINE result     : $QUARANTINE_RESULT"
echo "Quarantine outcome               : $QUARANTINE_OUTCOME"
echo "Execution completion             : $QUARANTINE_COMPLETION"
echo "Architectural outcome            : $QUARANTINE_ARCHITECTURAL_OUTCOME"
echo "Trace                            : $QUARANTINE_TRACE"
echo "Assertion events                 : $QUARANTINE_RUN/assertion_events.tsv"
echo "Validation report                : $REPORT"
echo "Final diagnostic oracle          : NOT ASSIGNED"
echo "VCD generated                    : NO"
