#!/usr/bin/env bash
set -euo pipefail

F2A_ROOT="${F2A_ROOT:-/raid/spring2026/fwu44/research/fault2assertion}"

FC="$F2A_ROOT/scripts/fault_characterization"
MINI_ROOT="$F2A_ROOT/runs/stage5_dev/mini_smoke_v1"
PHASE2_ROOT="$F2A_ROOT/runs/stage5_dev/phase2_v1"
G2_ROOT="$PHASE2_ROOT/g2_native_equivalence"
G3_ROOT="$PHASE2_ROOT/g3_observe"

POLICY="$F2A_ROOT/platform/cv32e40p/stage5_phase2_execution_policy_v1.json"
MODE_TOOL="$FC/stage5_phase2_modes.py"
G3_VALIDATOR="$FC/stage5_phase2_g3_validate.py"

SMOKE_SELECTION="$MINI_ROOT/provenance/smoke_fault_selection.json"
FAULT_MONITOR_ROOT="$MINI_ROOT/monitors/faults"
FAULT_MANIFEST_ROOT="$MINI_ROOT/manifests/faults"

G2_REPORT="$G2_ROOT/phase2_g2_validation.json"
FAULT_WRAPPER="$F2A_ROOT/scripts/run_xrun_stage5_fault.sh"

OBSERVE_MONITOR="$G3_ROOT/generated_monitors/fault_observe_runtime.sv"
OBSERVE_METADATA="$G3_ROOT/mode_metadata/fault_observe_runtime.json"
OBSERVE_TRACE="$G3_ROOT/traces/fault_observe_runtime.trace.tsv"
OBSERVE_RUN="$G3_ROOT/runs/fault_observe_runtime"
REPORT="$G3_ROOT/phase2_g3_validation.json"


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
    case "$G3_ROOT" in
        "$F2A_ROOT"/runs/stage5_dev/phase2_v1/g3_observe)
            ;;
        *)
            fail "unsafe Phase2-G3 root: $G3_ROOT"
            return 1
            ;;
    esac

    if [[ -e "$G3_ROOT" ]]; then
        fail "Phase2-G3 workspace already exists: $G3_ROOT"
        return 1
    fi

    mkdir -p \
        "$G3_ROOT/generated_monitors" \
        "$G3_ROOT/mode_metadata" \
        "$G3_ROOT/runs" \
        "$G3_ROOT/traces"
}


cd "$F2A_ROOT"

log "Validate the frozen G2 Native sanity checkpoint"

require_file "$G2_REPORT" "Phase2-G2 report"

python3 - "$G2_REPORT" <<'PY'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if report.get("status") != "PASS":
    raise SystemExit("ERROR: Phase2-G2 report is not PASS")

claims = report.get("gate_claims", {})
required_true = (
    "golden_native_passed",
    "golden_expected_crc_signature_present",
    "fault_native_reproduced_phase1_key_facts",
    "fault_existing_detector_is_out_of_bounds_write",
    "native_runs_used_original_mm_ram",
    "diagnostic_overlay_not_used",
    "observe_runtime_not_executed",
    "quarantine_runtime_not_executed",
)
for key in required_true:
    if claims.get(key) is not True:
        raise SystemExit(f"ERROR: Phase2-G2 claim is not true: {key}")

if claims.get("final_diagnostic_oracle_assigned") is not False:
    raise SystemExit("ERROR: Phase2-G2 unexpectedly assigned a final oracle")

print("Phase2-G2 prerequisite: PASS")
PY

log "Validate the minimal G3 inputs"

for file in \
    "$POLICY" \
    "$MODE_TOOL" \
    "$G3_VALIDATOR" \
    "$SMOKE_SELECTION" \
    "$G2_REPORT" \
    "$FAULT_WRAPPER"
do
    require_file "$file" "Phase2-G3 input"
done

python3 -m json.tool "$POLICY" >/dev/null
python3 -m py_compile "$MODE_TOOL" "$G3_VALIDATOR"
bash -n "$FAULT_WRAPPER"

readarray -t META < <(
python3 - \
    "$G2_REPORT" \
    "$SMOKE_SELECTION" \
    "$FAULT_MONITOR_ROOT" \
    "$FAULT_MANIFEST_ROOT" <<'PY'
import json
import sys
from pathlib import Path

g2 = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
selection = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))

fault_id = str(g2["fault_id"])
if str(selection["fault_id"]) != fault_id:
    raise SystemExit("ERROR: G2 and smoke selection fault IDs differ")

fault_json = Path(str(selection["fault_spec"])).resolve()
fault_monitor = Path(sys.argv[3]).resolve() / f"{fault_id}.sv"
fault_manifest = Path(sys.argv[4]).resolve() / f"{fault_id}.json"

print(fault_id)
print(fault_json)
print(fault_monitor)
print(fault_manifest)
PY
)

[[ "${#META[@]}" -eq 4 ]] || fail "failed to resolve Phase2-G3 metadata"

FAULT_ID="${META[0]}"
FAULT_JSON="${META[1]}"
FAULT_BASE_MONITOR="${META[2]}"
FAULT_BASE_MANIFEST="${META[3]}"

for file in \
    "$FAULT_JSON" \
    "$FAULT_BASE_MONITOR" \
    "$FAULT_BASE_MANIFEST"
do
    require_file "$file" "resolved Phase2-G3 input"
done

log "Create a fresh Phase2-G3 workspace"
create_fresh_root

log "Generate the OBSERVE fault monitor"

python3 "$MODE_TOOL" compose \
    --policy "$POLICY" \
    --base-monitor "$FAULT_BASE_MONITOR" \
    --base-manifest "$FAULT_BASE_MANIFEST" \
    --mode OBSERVE \
    --trace-output "$OBSERVE_TRACE" \
    --output-monitor "$OBSERVE_MONITOR" \
    --output-metadata "$OBSERVE_METADATA"

log "Run the fault in DIAGNOSTIC_OBSERVE"

set +e
STAGE5_PHASE=run \
STAGE5_RUN_PURPOSE=DIAGNOSTIC_OBSERVE \
STAGE5_MM_RAM_PROFILE=diagnostic \
STAGE5_TRACE_OUTPUT="$OBSERVE_TRACE" \
MAXCYCLES=2000000 \
VCD=0 \
KEEP_WORK=0 \
"$FAULT_WRAPPER" "$FAULT_JSON" "$OBSERVE_MONITOR" "$OBSERVE_RUN"
OBSERVE_WRAPPER_STATUS=$?
set -e

if [[ "$OBSERVE_WRAPPER_STATUS" -ne 0 \
      && "$OBSERVE_WRAPPER_STATUS" -ne 2 ]]; then
    fail \
        "DIAGNOSTIC_OBSERVE wrapper returned infrastructure/unknown status ${OBSERVE_WRAPPER_STATUS}"
fi

require_file "$OBSERVE_RUN/result.txt" "OBSERVE result.txt"
OBSERVE_RESULT="$(cat "$OBSERVE_RUN/result.txt")"

case "$OBSERVE_RESULT" in
    DIAGNOSTIC_OUTPUT_MATCH|DIAGNOSTIC_OUTPUT_MISMATCH|DIAGNOSTIC_TIMEOUT)
        ;;
    *)
        fail "invalid DIAGNOSTIC_OBSERVE result: $OBSERVE_RESULT"
        ;;
esac

require_file "$OBSERVE_RUN/result.json" "OBSERVE result.json"
require_file "$OBSERVE_TRACE" "OBSERVE compact trace"
require_file "$OBSERVE_RUN/assertion_events.tsv" "OBSERVE assertion events"
require_file "$OBSERVE_RUN/mm_ram_preparation.json" "OBSERVE preparation report"
require_file "$OBSERVE_RUN/mm_ram_ownership.json" "OBSERVE ownership report"

log "Validate the minimal G3 OBSERVE contract"

python3 "$G3_VALIDATOR" \
    --g2-report "$G2_REPORT" \
    --run-dir "$OBSERVE_RUN" \
    --trace "$OBSERVE_TRACE" \
    --metadata "$OBSERVE_METADATA" \
    --fault-id "$FAULT_ID" \
    --report "$REPORT"

readarray -t SUMMARY < <(
python3 - "$REPORT" <<'PY'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
observe = report["observe"]
print(observe["runner_status"])
print(observe["observe_outcome"])
print(observe["execution_completion"])
print(observe["architectural_outcome"])
PY
)

OBSERVE_RESULT="${SUMMARY[0]}"
OBSERVE_OUTCOME="${SUMMARY[1]}"
OBSERVE_COMPLETION="${SUMMARY[2]}"
OBSERVE_ARCHITECTURAL_OUTCOME="${SUMMARY[3]}"

log "Phase2-G3 completed successfully"

echo
echo "======================================================================"
echo "Phase2-G3 PASS: minimal OBSERVE experiment"
echo "======================================================================"
echo "Fault ID                         : $FAULT_ID"
echo "Execution purpose                : DIAGNOSTIC_OBSERVE"
echo "Assertion mode                   : observe"
echo "mm_ram profile                   : diagnostic"
echo "Existing detector                : out_of_bounds_write"
echo "Detector action                  : RECORD_ONLY"
echo "Termination suppressed           : YES"
echo "Transaction quarantine           : NO"
echo "OBSERVE runner result             : $OBSERVE_RESULT"
echo "OBSERVE outcome                   : $OBSERVE_OUTCOME"
echo "Execution completion              : $OBSERVE_COMPLETION"
echo "Architectural outcome             : $OBSERVE_ARCHITECTURAL_OUTCOME"
echo "Trace                             : $OBSERVE_TRACE"
echo "Assertion events                  : $OBSERVE_RUN/assertion_events.tsv"
echo "Validation report                 : $REPORT"
echo "DIAGNOSTIC_QUARANTINE runtime     : NOT EXECUTED"
echo "Final diagnostic oracle           : NOT ASSIGNED"
echo "VCD generated                     : NO"
