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
STAGE5_TOOL="$FC/stage5_faults.py"

MINI_CAMPAIGN="$MINI_ROOT/campaign/stage_05_campaign.json"
SMOKE_SELECTION="$MINI_ROOT/provenance/smoke_fault_selection.json"

GOLDEN_BASE_MONITOR="$MINI_ROOT/monitors/stage5_golden_monitor.sv"
GOLDEN_BASE_MANIFEST="$MINI_ROOT/manifests/stage5_golden_monitor_manifest.json"

FAULT_MONITOR_ROOT="$MINI_ROOT/monitors/faults"
FAULT_MANIFEST_ROOT="$MINI_ROOT/manifests/faults"

PHASE1_GATE3_REPORT="$MINI_ROOT/reports/gate3_golden_validation.json"
PHASE1_GATE4_REPORT="$MINI_ROOT/reports/gate4_single_fault_validation.json"
PHASE1_GOLDEN_SPLIT="$MINI_ROOT/gate3_golden/site_traces"

GOLDEN_WRAPPER="$F2A_ROOT/scripts/run_xrun_stage5_golden.sh"
FAULT_WRAPPER="$F2A_ROOT/scripts/run_xrun_stage5_fault.sh"

GOLDEN_MONITOR="$G2_ROOT/generated_monitors/golden_native_runtime.sv"
GOLDEN_METADATA="$G2_ROOT/mode_metadata/golden_native_runtime.json"
GOLDEN_TRACE="$G2_ROOT/traces/golden_native_all.trace.tsv"
GOLDEN_RUN="$G2_ROOT/runs/golden_native_runtime"
GOLDEN_SPLIT="$G2_ROOT/golden_site_traces"
GOLDEN_SPLIT_MANIFEST="$G2_ROOT/golden_split_manifest.json"

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
    exit 1
}


require_file() {
    local path="$1"
    local label="$2"
    [[ -s "$path" ]] || fail "$label not found or empty: $path"
}


create_fresh_root() {
    case "$G2_ROOT" in
        "$F2A_ROOT"/runs/stage5_dev/phase2_v1/g2_native_equivalence) ;;
        *) fail "unsafe Phase2-G2 root: $G2_ROOT" ;;
    esac

    if [[ -e "$G2_ROOT" ]]; then
        fail "Phase2-G2 workspace already exists. Preserve or move it before rerun: $G2_ROOT"
    fi

    mkdir -p \
        "$G2_ROOT/generated_monitors" \
        "$G2_ROOT/mode_metadata" \
        "$G2_ROOT/runs" \
        "$G2_ROOT/traces"
}


cd "$F2A_ROOT"

log "Require Phase2-G1 PASS before Phase2-G2"

G1_REPORT="$G1_ROOT/phase2_g1_validation.json"
require_file "$G1_REPORT" "Phase2-G1 report"

python3 - "$G1_REPORT" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
if payload.get("status") != "PASS":
    raise SystemExit("ERROR: Phase2-G1 report is not PASS")
claims = payload.get("gate_claims", {})
required = [
    "mode_infrastructure_available",
    "native_compile_and_elaboration_passed",
    "monitor_sanitation_passed",
    "compile_only_generated_no_trace",
]
for key in required:
    if claims.get(key) is not True:
        raise SystemExit(f"ERROR: Phase2-G1 claim is not true: {key}")
if claims.get("diagnostic_continuation_runtime_implemented") is not False:
    raise SystemExit(
        "ERROR: Phase2-G1 incorrectly claims diagnostic runtime implementation"
    )
PY

log "Validate Phase-1 frozen evidence and Phase2-G2 inputs"

for file in \
    "$POLICY" \
    "$MODE_TOOL" \
    "$STAGE5_TOOL" \
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

[[ -d "$PHASE1_GOLDEN_SPLIT" ]] \
    || fail "Phase-1 golden split directory not found: $PHASE1_GOLDEN_SPLIT"

python3 -m json.tool "$POLICY" >/dev/null
python3 -m py_compile "$MODE_TOOL"

readarray -t META < <(
python3 - \
    "$MINI_CAMPAIGN" \
    "$SMOKE_SELECTION" \
    "$FAULT_MONITOR_ROOT" \
    "$FAULT_MANIFEST_ROOT" \
    "$PHASE1_GATE4_REPORT" <<'PY'
import json
import sys
from pathlib import Path

campaign = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
selection = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
gate4 = json.loads(Path(sys.argv[5]).read_text(encoding="utf-8"))

fault_id = str(selection["fault_id"])
fault_json = Path(str(selection["fault_spec"])).resolve()
fault_monitor = Path(sys.argv[3]).resolve() / f"{fault_id}.sv"
fault_manifest = Path(sys.argv[4]).resolve() / f"{fault_id}.json"
golden_netlist = Path(str(campaign["mapped_netlist"]["path"])).resolve()
phase1_fault_trace = Path(str(gate4["trace"])).resolve()

print(fault_id)
print(fault_json)
print(fault_monitor)
print(fault_manifest)
print(golden_netlist)
print(phase1_fault_trace)
PY
)

[[ "${#META[@]}" -eq 6 ]] || fail "failed to resolve Phase2-G2 metadata"

FAULT_ID="${META[0]}"
FAULT_JSON="${META[1]}"
FAULT_BASE_MONITOR="${META[2]}"
FAULT_BASE_MANIFEST="${META[3]}"
GOLDEN_NETLIST="${META[4]}"
PHASE1_FAULT_TRACE="${META[5]}"

for file in \
    "$FAULT_JSON" \
    "$FAULT_BASE_MONITOR" \
    "$FAULT_BASE_MANIFEST" \
    "$GOLDEN_NETLIST" \
    "$PHASE1_FAULT_TRACE"
do
    require_file "$file" "resolved Phase2-G2 input"
done

log "Confirm Phase-1 fault is the expected assertion-terminated observation"

python3 - "$PHASE1_GATE4_REPORT" "$FAULT_ID" <<'PY'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
fault_id = sys.argv[2]

if report.get("status") != "PASS":
    raise SystemExit("ERROR: Phase-1 Gate-4 validation report is not PASS")
if report.get("fault_id") != fault_id:
    raise SystemExit("ERROR: Phase-1 Gate-4 fault ID mismatch")
if report.get("native_observation_status") != "EXISTING_ASSERTION_DETECTED":
    raise SystemExit(
        "ERROR: Phase2-G2 currently expects the frozen "
        "EXISTING_ASSERTION_DETECTED smoke fault"
    )

raw = report.get("raw_execution_facts", {})
execution = raw.get("execution", {})
workload = raw.get("workload", {})
detector = raw.get("existing_detector_baseline", {})

if execution.get("completion") != "TERMINATED_BY_EXISTING_ASSERTION":
    raise SystemExit("ERROR: Phase-1 completion mismatch")
if workload.get("outcome") != "NOT_REACHED":
    raise SystemExit("ERROR: Phase-1 workload outcome mismatch")
if workload.get("architectural_outcome") != "CENSORED":
    raise SystemExit("ERROR: Phase-1 architectural outcome mismatch")
if detector.get("event_count", 0) < 1:
    raise SystemExit("ERROR: Phase-1 detector event is missing")
PY

log "Create fresh Phase2-G2 workspace"
create_fresh_root

log "Generate Phase2-G2 NATIVE golden monitor"

python3 "$MODE_TOOL" compose \
    --policy "$POLICY" \
    --base-monitor "$GOLDEN_BASE_MONITOR" \
    --base-manifest "$GOLDEN_BASE_MANIFEST" \
    --mode NATIVE \
    --trace-output "$GOLDEN_TRACE" \
    --output-monitor "$GOLDEN_MONITOR" \
    --output-metadata "$GOLDEN_METADATA"

log "Generate Phase2-G2 NATIVE fault monitor"

python3 "$MODE_TOOL" compose \
    --policy "$POLICY" \
    --base-monitor "$FAULT_BASE_MONITOR" \
    --base-manifest "$FAULT_BASE_MANIFEST" \
    --mode NATIVE \
    --trace-output "$FAULT_TRACE" \
    --output-monitor "$FAULT_MONITOR" \
    --output-metadata "$FAULT_METADATA"

log "Run Phase2-G2 NATIVE golden execution"

STAGE5_PHASE=run \
STAGE5_RUN_PURPOSE=NATIVE_CHARACTERIZATION \
STAGE5_TRACE_OUTPUT="$GOLDEN_TRACE" \
GOLDEN_NETLIST="$GOLDEN_NETLIST" \
MAXCYCLES=2000000 \
VCD=0 \
KEEP_WORK=0 \
"$GOLDEN_WRAPPER" "$GOLDEN_MONITOR" "$GOLDEN_RUN"

[[ "$(cat "$GOLDEN_RUN/result.txt")" == "PASS" ]] \
    || fail "Phase2-G2 native golden result is not PASS"

require_file "$GOLDEN_TRACE" "Phase2-G2 golden trace"

log "Split Phase2-G2 golden trace without deleting the raw trace"

python3 "$STAGE5_TOOL" split-golden-trace \
    --trace "$GOLDEN_TRACE" \
    --output-dir "$GOLDEN_SPLIT" \
    --manifest "$GOLDEN_SPLIT_MANIFEST" \
    --force

log "Run Phase2-G2 NATIVE fault execution"

set +e
STAGE5_PHASE=run \
STAGE5_RUN_PURPOSE=NATIVE_CHARACTERIZATION \
STAGE5_TRACE_OUTPUT="$FAULT_TRACE" \
MAXCYCLES=2000000 \
VCD=0 \
KEEP_WORK=0 \
"$FAULT_WRAPPER" "$FAULT_JSON" "$FAULT_MONITOR" "$FAULT_RUN"
FAULT_RUN_STATUS=$?
set -e

if [[ "$FAULT_RUN_STATUS" -ne 2 ]]; then
    fail "Phase2-G2 native fault wrapper returned $FAULT_RUN_STATUS; expected 2"
fi

[[ "$(cat "$FAULT_RUN/result.txt")" == "EXISTING_ASSERTION_DETECTED" ]] \
    || fail "Phase2-G2 native fault result is not EXISTING_ASSERTION_DETECTED"

require_file "$FAULT_TRACE" "Phase2-G2 fault trace"

log "Validate Phase2-G2 native equivalence fail-closed"

python3 "$MODE_TOOL" validate-g2 \
    --policy "$POLICY" \
    --phase1-gate3-report "$PHASE1_GATE3_REPORT" \
    --phase1-gate4-report "$PHASE1_GATE4_REPORT" \
    --candidate-golden-run "$GOLDEN_RUN" \
    --candidate-fault-run "$FAULT_RUN" \
    --phase1-golden-split "$PHASE1_GOLDEN_SPLIT" \
    --candidate-golden-split "$GOLDEN_SPLIT" \
    --phase1-fault-trace "$PHASE1_FAULT_TRACE" \
    --candidate-fault-trace "$FAULT_TRACE" \
    --golden-metadata "$GOLDEN_METADATA" \
    --fault-metadata "$FAULT_METADATA" \
    --report "$REPORT"

log "Phase2-G2 completed successfully"

echo
echo "======================================================================"
echo "Phase2-G2 PASS"
echo "======================================================================"
echo "Fault ID                   : $FAULT_ID"
echo "Golden candidate run       : $GOLDEN_RUN"
echo "Fault candidate run        : $FAULT_RUN"
echo "Golden trace comparison    : PASS"
echo "Fault trace comparison     : PASS"
echo "Existing detector          : EQUIVALENT"
echo "Native completion/outcome  : EQUIVALENT"
echo "Validation report          : $REPORT"
echo "No diagnostic continuation was executed."
echo "No external cv32e40p source was modified."
echo "No VCD was generated."
