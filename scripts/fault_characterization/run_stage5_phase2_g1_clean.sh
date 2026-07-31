#!/usr/bin/env bash
set -euo pipefail

F2A_ROOT="${F2A_ROOT:-/raid/spring2026/fwu44/research/fault2assertion}"
CV32E40P_HOME="${CV32E40P_HOME:-/raid/spring2026/fwu44/research/cv32e40p}"

FC="$F2A_ROOT/scripts/fault_characterization"
MINI_ROOT="$F2A_ROOT/runs/stage5_dev/mini_smoke_v1"
G1_ROOT="$F2A_ROOT/runs/stage5_dev/phase2_g1_clean_v1"

PREPARE_MONITOR="$FC/stage5_phase2_g1_prepare_monitor.py"
VALIDATE_G1="$FC/stage5_phase2_g1_validate.py"

CAMPAIGN="$MINI_ROOT/campaign/stage_05_campaign.json"
SELECTION="$MINI_ROOT/provenance/smoke_fault_selection.json"

GOLDEN_BASE_MONITOR="$MINI_ROOT/monitors/stage5_golden_monitor.sv"
GOLDEN_BASE_MANIFEST="$MINI_ROOT/manifests/stage5_golden_monitor_manifest.json"
FAULT_MONITOR_ROOT="$MINI_ROOT/monitors/faults"
FAULT_MANIFEST_ROOT="$MINI_ROOT/manifests/faults"

GOLDEN_WRAPPER="$F2A_ROOT/scripts/run_xrun_stage5_golden.sh"
FAULT_WRAPPER="$F2A_ROOT/scripts/run_xrun_stage5_fault.sh"
ORIGINAL_MM_RAM="$CV32E40P_HOME/verification/shared/tb/mm_ram.sv"

GOLDEN_MONITOR="$G1_ROOT/monitors/golden_compile.sv"
GOLDEN_MONITOR_METADATA="$G1_ROOT/monitors/golden_compile.json"
GOLDEN_TRACE="$G1_ROOT/traces/golden_compile.trace.tsv"
GOLDEN_EVENTS="$G1_ROOT/events/golden_compile.events.tsv"
GOLDEN_RUN="$G1_ROOT/runs/golden_compile"

FAULT_MONITOR="$G1_ROOT/monitors/fault_compile.sv"
FAULT_MONITOR_METADATA="$G1_ROOT/monitors/fault_compile.json"
FAULT_TRACE="$G1_ROOT/traces/fault_compile.trace.tsv"
FAULT_EVENTS="$G1_ROOT/events/fault_compile.events.tsv"
FAULT_RUN="$G1_ROOT/runs/fault_compile"

FINAL_REPORT="$G1_ROOT/reports/phase2_g1_validation.json"


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
    if [[ ! -s "$path" ]]; then
        fail "$label not found or empty: $path"
        return 1
    fi
}


cd "$F2A_ROOT"

log "Phase2-G1 clean preflight"
echo "F2A_ROOT       : $F2A_ROOT"
echo "CV32E40P_HOME  : $CV32E40P_HOME"
echo "G1_ROOT        : $G1_ROOT"

for file in \
    "$PREPARE_MONITOR" \
    "$VALIDATE_G1" \
    "$CAMPAIGN" \
    "$SELECTION" \
    "$GOLDEN_BASE_MONITOR" \
    "$GOLDEN_BASE_MANIFEST" \
    "$GOLDEN_WRAPPER" \
    "$FAULT_WRAPPER" \
    "$ORIGINAL_MM_RAM"
do
    require_file "$file" "Phase2-G1 input"
done

python3 -m py_compile "$PREPARE_MONITOR" "$VALIDATE_G1"
python3 "$PREPARE_MONITOR" selftest
bash -n "$GOLDEN_WRAPPER"
bash -n "$FAULT_WRAPPER"

if [[ -e "$G1_ROOT" ]]; then
    fail "fresh Phase2-G1 workspace already exists: $G1_ROOT"
fi

readarray -t META < <(
python3 - \
    "$CAMPAIGN" \
    "$SELECTION" \
    "$FAULT_MONITOR_ROOT" \
    "$FAULT_MANIFEST_ROOT" <<'PY'
import json
import sys
from pathlib import Path

campaign = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
selection = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))

fault_id = str(selection["fault_id"])
fault_json = Path(str(selection["fault_spec"])).resolve()
fault_monitor = (Path(sys.argv[3]).resolve() / f"{fault_id}.sv").resolve()
fault_manifest = (Path(sys.argv[4]).resolve() / f"{fault_id}.json").resolve()
golden_netlist = Path(str(campaign["mapped_netlist"]["path"])).resolve()

print(fault_id)
print(fault_json)
print(fault_monitor)
print(fault_manifest)
print(golden_netlist)
PY
)

if [[ "${#META[@]}" -ne 5 ]]; then
    fail "failed to resolve Phase2-G1 metadata"
fi

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
    require_file "$file" "resolved Phase2-G1 input"
done

mkdir -p \
    "$G1_ROOT/monitors" \
    "$G1_ROOT/traces" \
    "$G1_ROOT/events" \
    "$G1_ROOT/runs" \
    "$G1_ROOT/reports"

log "Prepare golden monitor: trace-path substitution only"
python3 "$PREPARE_MONITOR" prepare \
    --base-monitor "$GOLDEN_BASE_MONITOR" \
    --base-manifest "$GOLDEN_BASE_MANIFEST" \
    --trace-output "$GOLDEN_TRACE" \
    --output "$GOLDEN_MONITOR" \
    --metadata "$GOLDEN_MONITOR_METADATA" \
    --role golden

log "Prepare fault monitor: trace-path substitution only"
python3 "$PREPARE_MONITOR" prepare \
    --base-monitor "$FAULT_BASE_MONITOR" \
    --base-manifest "$FAULT_BASE_MANIFEST" \
    --trace-output "$FAULT_TRACE" \
    --output "$FAULT_MONITOR" \
    --metadata "$FAULT_MONITOR_METADATA" \
    --role fault

log "Compile/elaborate golden with the single run-local detector overlay"
STAGE5_PHASE=compile \
STAGE5_RUN_PURPOSE=COMPILE_CHECK \
STAGE5_USE_ASSERTION_OVERLAY=1 \
STAGE5_ASSERTION_MODE=native \
STAGE5_ASSERTION_EVENT_OUTPUT="$GOLDEN_EVENTS" \
STAGE5_TRACE_OUTPUT="$GOLDEN_TRACE" \
GOLDEN_NETLIST="$GOLDEN_NETLIST" \
MAXCYCLES=2000000 \
VCD=0 \
KEEP_WORK=1 \
"$GOLDEN_WRAPPER" \
    "$GOLDEN_MONITOR" \
    "$GOLDEN_RUN"

log "Compile/elaborate fault with the same single run-local detector overlay"
STAGE5_PHASE=compile \
STAGE5_RUN_PURPOSE=COMPILE_CHECK \
STAGE5_USE_ASSERTION_OVERLAY=1 \
STAGE5_ASSERTION_MODE=native \
STAGE5_ASSERTION_EVENT_OUTPUT="$FAULT_EVENTS" \
STAGE5_TRACE_OUTPUT="$FAULT_TRACE" \
MAXCYCLES=2000000 \
VCD=0 \
KEEP_WORK=1 \
"$FAULT_WRAPPER" \
    "$FAULT_JSON" \
    "$FAULT_MONITOR" \
    "$FAULT_RUN"

log "Validate ownership, single transformation, NATIVE preservation, and elaboration"
python3 "$VALIDATE_G1" \
    --original-mm-ram "$ORIGINAL_MM_RAM" \
    --golden-run "$GOLDEN_RUN" \
    --fault-run "$FAULT_RUN" \
    --golden-monitor-metadata "$GOLDEN_MONITOR_METADATA" \
    --fault-monitor-metadata "$FAULT_MONITOR_METADATA" \
    --report "$FINAL_REPORT"

log "Phase2-G1 clean experiment completed successfully"
echo "Fault ID       : $FAULT_ID"
echo "Workspace      : $G1_ROOT"
echo "Golden result  : $(cat "$GOLDEN_RUN/result.txt")"
echo "Fault result   : $(cat "$FAULT_RUN/result.txt")"
echo "Final report   : $FINAL_REPORT"
echo "Observe runtime: NOT EXECUTED"
echo "Quarantine run : NOT EXECUTED"
