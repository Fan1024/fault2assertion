#!/usr/bin/env bash
set -euo pipefail

F2A_ROOT="${F2A_ROOT:-/raid/spring2026/fwu44/research/fault2assertion}"

FC="$F2A_ROOT/scripts/fault_characterization"
MINI_ROOT="$F2A_ROOT/runs/stage5_dev/mini_smoke_v1"
PHASE2_ROOT="$F2A_ROOT/runs/stage5_dev/phase2_v1"
G1_ROOT="$PHASE2_ROOT/g1_mode_infrastructure"
G2_ROOT="$PHASE2_ROOT/g2_native_equivalence"

POLICY="$F2A_ROOT/platform/cv32e40p/stage5_phase2_execution_policy_v1.json"
MODE_TOOL="$FC/stage5_phase2_modes.py"
G2_VALIDATOR="$FC/stage5_phase2_g2_validate.py"

MINI_CAMPAIGN="$MINI_ROOT/campaign/stage_05_campaign.json"
SMOKE_SELECTION="$MINI_ROOT/provenance/smoke_fault_selection.json"

GOLDEN_BASE_MONITOR="$MINI_ROOT/monitors/stage5_golden_monitor.sv"
GOLDEN_BASE_MANIFEST="$MINI_ROOT/manifests/stage5_golden_monitor_manifest.json"

FAULT_MONITOR_ROOT="$MINI_ROOT/monitors/faults"
FAULT_MANIFEST_ROOT="$MINI_ROOT/manifests/faults"

G1_REPORT="$G1_ROOT/phase2_g1_validation.json"
PHASE1_GATE3_REPORT="$MINI_ROOT/reports/gate3_golden_validation.json"
PHASE1_GATE4_REPORT="$MINI_ROOT/reports/gate4_single_fault_validation.json"

GOLDEN_WRAPPER="$F2A_ROOT/scripts/run_xrun_stage5_golden.sh"
FAULT_WRAPPER="$F2A_ROOT/scripts/run_xrun_stage5_fault.sh"

GOLDEN_MONITOR="$G2_ROOT/generated_monitors/golden_native_runtime.sv"
GOLDEN_METADATA="$G2_ROOT/mode_metadata/golden_native_runtime.json"
GOLDEN_TRACE="$G2_ROOT/traces/golden_native_runtime.trace.tsv"
GOLDEN_RUN="$G2_ROOT/runs/golden_native_runtime"

FAULT_MONITOR="$G2_ROOT/generated_monitors/fault_native_runtime.sv"
FAULT_METADATA="$G2_ROOT/mode_metadata/fault_native_runtime.json"
FAULT_TRACE="$G2_ROOT/traces/fault_native_runtime.trace.tsv"
FAULT_RUN="$G2_ROOT/runs/fault_native_runtime"

REPORT="$G2_ROOT/phase2_g2_validation.json"


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
    case "$G2_ROOT" in
        "$F2A_ROOT"/runs/stage5_dev/phase2_v1/g2_native_equivalence)
            ;;
        *)
            fail "unsafe Phase2-G2 root: $G2_ROOT"
            return 1
            ;;
    esac

    if [[ -e "$G2_ROOT" ]]; then
        fail "Phase2-G2 workspace already exists: $G2_ROOT"
        return 1
    fi

    mkdir -p \
        "$G2_ROOT/generated_monitors" \
        "$G2_ROOT/mode_metadata" \
        "$G2_ROOT/runs" \
        "$G2_ROOT/traces"
}


cd "$F2A_ROOT"

log "Validate the frozen G1 checkpoint"

require_file "$G1_REPORT" "Phase2-G1 report"

python3 - "$G1_REPORT" <<'PY'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if report.get("status") != "PASS":
    raise SystemExit("ERROR: Phase2-G1 report is not PASS")

claims = report.get("gate_claims", {})
required_true = (
    "mode_infrastructure_available",
    "native_compile_and_elaboration_passed",
    "native_profile_uses_original_mm_ram",
    "native_existing_assertion_unmodified",
)
for key in required_true:
    if claims.get(key) is not True:
        raise SystemExit(f"ERROR: Phase2-G1 claim is not true: {key}")

if claims.get("diagnostic_continuation_runtime_executed") is not False:
    raise SystemExit("ERROR: diagnostic runtime was unexpectedly executed in G1")
if claims.get("final_fault_effect_oracle_assigned") is not False:
    raise SystemExit("ERROR: G1 unexpectedly assigned a final fault oracle")

print("Phase2-G1 prerequisite: PASS")
PY

log "Validate the minimal G2 inputs"

for file in \
    "$POLICY" \
    "$MODE_TOOL" \
    "$G2_VALIDATOR" \
    "$MINI_CAMPAIGN" \
    "$SMOKE_SELECTION" \
    "$GOLDEN_BASE_MONITOR" \
    "$GOLDEN_BASE_MANIFEST" \
    "$PHASE1_GATE3_REPORT" \
    "$PHASE1_GATE4_REPORT" \
    "$GOLDEN_WRAPPER" \
    "$FAULT_WRAPPER"
do
    require_file "$file" "Phase2-G2 input"
done

python3 -m json.tool "$POLICY" >/dev/null
python3 -m py_compile "$MODE_TOOL" "$G2_VALIDATOR"
bash -n "$GOLDEN_WRAPPER"
bash -n "$FAULT_WRAPPER"

readarray -t META < <(
python3 - \
    "$MINI_CAMPAIGN" \
    "$SMOKE_SELECTION" \
    "$FAULT_MONITOR_ROOT" \
    "$FAULT_MANIFEST_ROOT" <<'PY'
import json
import sys
from pathlib import Path

campaign = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
selection = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))

fault_id = str(selection["fault_id"])
fault_json = Path(str(selection["fault_spec"])).resolve()
fault_monitor = Path(sys.argv[3]).resolve() / f"{fault_id}.sv"
fault_manifest = Path(sys.argv[4]).resolve() / f"{fault_id}.json"
golden_netlist = Path(str(campaign["mapped_netlist"]["path"])).resolve()

print(fault_id)
print(fault_json)
print(fault_monitor)
print(fault_manifest)
print(golden_netlist)
PY
)

[[ "${#META[@]}" -eq 5 ]] || fail "failed to resolve Phase2-G2 metadata"

FAULT_ID="${META[0]}"
FAULT_JSON="${META[1]}"
FAULT_BASE_MONITOR="${META[2]}"
FAULT_BASE_MANIFEST="${META[3]}"
GOLDEN_NETLIST="${META[4]}"

for file in \
    "$FAULT_JSON" \
    "$FAULT_BASE_MONITOR" \
    "$FAULT_BASE_MANIFEST" \
    "$GOLDEN_NETLIST"
do
    require_file "$file" "resolved Phase2-G2 input"
done

log "Confirm the frozen Phase1 smoke result"

python3 - "$PHASE1_GATE3_REPORT" "$PHASE1_GATE4_REPORT" "$FAULT_ID" <<'PY'
import json
import sys
from pathlib import Path

gate3 = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
gate4 = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
fault_id = sys.argv[3]

if gate3.get("status") != "PASS":
    raise SystemExit("ERROR: Phase1 Gate3 golden report is not PASS")
if gate4.get("status") != "PASS":
    raise SystemExit("ERROR: Phase1 Gate4 fault report is not PASS")
if gate4.get("fault_id") != fault_id:
    raise SystemExit("ERROR: Phase1 fault ID does not match the G2 smoke fault")
if gate4.get("native_observation_status") != "EXISTING_ASSERTION_DETECTED":
    raise SystemExit("ERROR: Phase1 fault baseline is not assertion-terminated")

raw = gate4.get("raw_execution_facts", {})
if raw.get("execution", {}).get("completion") != "TERMINATED_BY_EXISTING_ASSERTION":
    raise SystemExit("ERROR: Phase1 completion boundary mismatch")
if raw.get("workload", {}).get("outcome") != "NOT_REACHED":
    raise SystemExit("ERROR: Phase1 workload outcome mismatch")
if raw.get("workload", {}).get("architectural_outcome") != "CENSORED":
    raise SystemExit("ERROR: Phase1 architectural outcome mismatch")

print("Phase1 Native reference: PASS")
PY

log "Create a fresh Phase2-G2 workspace"
create_fresh_root

log "Generate the golden NATIVE monitor"

python3 "$MODE_TOOL" compose \
    --policy "$POLICY" \
    --base-monitor "$GOLDEN_BASE_MONITOR" \
    --base-manifest "$GOLDEN_BASE_MANIFEST" \
    --mode NATIVE \
    --trace-output "$GOLDEN_TRACE" \
    --output-monitor "$GOLDEN_MONITOR" \
    --output-metadata "$GOLDEN_METADATA"

log "Generate the fault NATIVE monitor"

python3 "$MODE_TOOL" compose \
    --policy "$POLICY" \
    --base-monitor "$FAULT_BASE_MONITOR" \
    --base-manifest "$FAULT_BASE_MANIFEST" \
    --mode NATIVE \
    --trace-output "$FAULT_TRACE" \
    --output-monitor "$FAULT_MONITOR" \
    --output-metadata "$FAULT_METADATA"

log "Run the golden NATIVE sanity check"

STAGE5_PHASE=run \
STAGE5_RUN_PURPOSE=NATIVE_CHARACTERIZATION \
STAGE5_MM_RAM_PROFILE=native \
STAGE5_TRACE_OUTPUT="$GOLDEN_TRACE" \
GOLDEN_NETLIST="$GOLDEN_NETLIST" \
MAXCYCLES=2000000 \
VCD=0 \
KEEP_WORK=0 \
"$GOLDEN_WRAPPER" "$GOLDEN_MONITOR" "$GOLDEN_RUN"

require_file "$GOLDEN_RUN/result.txt" "golden result.txt"
[[ "$(cat "$GOLDEN_RUN/result.txt")" == "PASS" ]] \
    || fail "golden Native result is not PASS"
require_file "$GOLDEN_TRACE" "golden compact trace"

log "Run the fault NATIVE sanity check"

set +e
STAGE5_PHASE=run \
STAGE5_RUN_PURPOSE=NATIVE_CHARACTERIZATION \
STAGE5_MM_RAM_PROFILE=native \
STAGE5_TRACE_OUTPUT="$FAULT_TRACE" \
MAXCYCLES=2000000 \
VCD=0 \
KEEP_WORK=0 \
"$FAULT_WRAPPER" "$FAULT_JSON" "$FAULT_MONITOR" "$FAULT_RUN"
FAULT_WRAPPER_STATUS=$?
set -e

if [[ "$FAULT_WRAPPER_STATUS" -ne 2 ]]; then
    fail \
        "fault Native wrapper returned ${FAULT_WRAPPER_STATUS}; expected scientific status 2"
fi

require_file "$FAULT_RUN/result.txt" "fault result.txt"
[[ "$(cat "$FAULT_RUN/result.txt")" == "EXISTING_ASSERTION_DETECTED" ]] \
    || fail "fault Native result is not EXISTING_ASSERTION_DETECTED"
require_file "$FAULT_TRACE" "fault compact trace"

log "Validate the minimal G2 Native contract"

python3 "$G2_VALIDATOR" \
    --g1-report "$G1_REPORT" \
    --phase1-gate3-report "$PHASE1_GATE3_REPORT" \
    --phase1-gate4-report "$PHASE1_GATE4_REPORT" \
    --golden-run "$GOLDEN_RUN" \
    --fault-run "$FAULT_RUN" \
    --golden-trace "$GOLDEN_TRACE" \
    --fault-trace "$FAULT_TRACE" \
    --golden-metadata "$GOLDEN_METADATA" \
    --fault-metadata "$FAULT_METADATA" \
    --fault-id "$FAULT_ID" \
    --report "$REPORT"

log "Phase2-G2 completed successfully"

echo
echo "======================================================================"
echo "Phase2-G2 PASS: minimal Native sanity check"
echo "======================================================================"
echo "Fault ID                      : $FAULT_ID"
echo "Golden Native                 : PASS + expected CRC"
echo "Fault Native                  : EXISTING_ASSERTION_DETECTED"
echo "Existing detector             : out_of_bounds_write"
echo "Native completion             : TERMINATED_BY_EXISTING_ASSERTION"
echo "Native architectural outcome  : CENSORED"
echo "Native source                 : original mm_ram.sv"
echo "Golden trace                  : $GOLDEN_TRACE"
echo "Fault trace                   : $FAULT_TRACE"
echo "Validation report             : $REPORT"
echo "Observe/Quarantine runtime    : NOT EXECUTED"
echo "Final diagnostic oracle       : NOT ASSIGNED"
echo "VCD generated                 : NO"
