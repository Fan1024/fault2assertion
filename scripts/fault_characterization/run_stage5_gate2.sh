#!/usr/bin/env bash
set -euo pipefail

F2A_ROOT="${F2A_ROOT:-/raid/spring2026/fwu44/research/fault2assertion}"
FC="$F2A_ROOT/scripts/fault_characterization"
MINI_ROOT="$F2A_ROOT/runs/stage5_dev/mini_smoke_v1"
MINI_CAMPAIGN="$MINI_ROOT/campaign/stage_05_campaign.json"
GOLDEN_MONITOR="$MINI_ROOT/monitors/stage5_golden_monitor.sv"
GOLDEN_MANIFEST="$MINI_ROOT/manifests/stage5_golden_monitor_manifest.json"
FAULT_MONITOR_ROOT="$MINI_ROOT/monitors/faults"
FAULT_MANIFEST_ROOT="$MINI_ROOT/manifests/faults"
SMOKE_SELECTION="$MINI_ROOT/provenance/smoke_fault_selection.json"
LOCK="$F2A_ROOT/runs/stage5_locks/mini_gate2_precompile_lock.json"
EXECUTION_INPUT_LOCK="$F2A_ROOT/runs/stage5_locks/mini_gate2_execution_inputs.json"
GATE2_ROOT="$MINI_ROOT/gate2_compile"
REPORT="$MINI_ROOT/reports/gate2_compile_validation.json"

LOCK_VERIFY="$FC/stage5_lock_verify.py"
EXECUTION_INPUT_TOOL="$FC/stage5_execution_input_lock.py"
COMMON_VALIDATE="$FC/stage5_gate_validation_common.py"
GATE2_VALIDATE="$FC/stage5_gate2_validate.py"
GOLDEN_WRAPPER="$F2A_ROOT/scripts/run_xrun_stage5_golden.sh"
FAULT_WRAPPER="$F2A_ROOT/scripts/run_xrun_stage5_fault.sh"

log() {
    printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

create_fresh_gate_root() {
    case "$GATE2_ROOT" in
        "$F2A_ROOT"/runs/stage5_dev/mini_smoke_v1/gate2_compile) ;;
        *) fail "unsafe Gate-2 root: $GATE2_ROOT" ;;
    esac
    if [[ -e "$GATE2_ROOT" ]]; then
        fail "Gate-2 workspace already exists. Preserve or archive it before an intentional rerun: $GATE2_ROOT"
    fi
    if [[ -e "$REPORT" ]]; then
        fail "Gate-2 report already exists. Preserve or archive it before an intentional rerun: $REPORT"
    fi
    mkdir -p "$GATE2_ROOT"
}

cd "$F2A_ROOT"

log "Verify the exact precompile code/artifact lock"
python3 "$LOCK_VERIFY" --repo-root "$F2A_ROOT" --lock "$LOCK"
python3 "$EXECUTION_INPUT_TOOL" verify --lock "$EXECUTION_INPUT_LOCK"

readarray -t META < <(
python3 - \
    "$MINI_CAMPAIGN" \
    "$GOLDEN_MANIFEST" \
    "$SMOKE_SELECTION" \
    "$FAULT_MANIFEST_ROOT" <<'PY'
import json
import sys
from pathlib import Path
campaign = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
golden_manifest = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
selection = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
fault_id = selection["fault_id"]
fault_manifest_path = Path(sys.argv[4]) / f"{fault_id}.json"
fault_manifest = json.loads(fault_manifest_path.read_text(encoding="utf-8"))
print(Path(campaign["mapped_netlist"]["path"]).resolve())
print(Path(golden_manifest["trace_output"]).resolve())
print(Path(selection["fault_spec"]).resolve())
print(Path(fault_manifest["trace_output"]).resolve())
print(fault_id)
PY
)

[[ "${#META[@]}" -eq 5 ]] || fail "failed to resolve Gate-2 metadata"
GOLDEN_NETLIST="${META[0]}"
GOLDEN_TRACE="${META[1]}"
FAULT_JSON="${META[2]}"
FAULT_TRACE="${META[3]}"
FAULT_ID="${META[4]}"
FAULT_MONITOR="$FAULT_MONITOR_ROOT/${FAULT_ID}.sv"
GOLDEN_RUN="$GATE2_ROOT/golden_compile"
FAULT_RUN="$GATE2_ROOT/${FAULT_ID}_compile"

for file in \
    "$MINI_CAMPAIGN" "$GOLDEN_MONITOR" "$FAULT_MONITOR" \
    "$FAULT_JSON" "$GOLDEN_NETLIST"
do
    [[ -s "$file" ]] || fail "missing Gate-2 input: $file"
done
[[ ! -e "$GOLDEN_TRACE" ]] || fail "golden trace already exists before Gate 2: $GOLDEN_TRACE"
[[ ! -e "$FAULT_TRACE" ]] || fail "fault trace already exists before Gate 2: $FAULT_TRACE"

log "Create a fresh isolated Gate-2 workspace without deleting prior evidence"
create_fresh_gate_root

log "Gate 2A: compile and elaborate golden netlist plus comprehensive mini monitor"
STAGE5_PHASE=compile \
STAGE5_TRACE_OUTPUT="$GOLDEN_TRACE" \
GOLDEN_NETLIST="$GOLDEN_NETLIST" \
MAXCYCLES=2000000 \
VCD=0 \
KEEP_WORK=0 \
"$GOLDEN_WRAPPER" "$GOLDEN_MONITOR" "$GOLDEN_RUN"

log "Gate 2B: materialize one run-local fault, then compile and elaborate its monitor"
STAGE5_PHASE=compile \
STAGE5_TRACE_OUTPUT="$FAULT_TRACE" \
MAXCYCLES=2000000 \
VCD=0 \
KEEP_WORK=0 \
"$FAULT_WRAPPER" "$FAULT_JSON" "$FAULT_MONITOR" "$FAULT_RUN"

log "Fail-closed Gate-2 validation"
python3 "$GATE2_VALIDATE" \
    --common "$COMMON_VALIDATE" \
    --golden-run "$GOLDEN_RUN" \
    --fault-run "$FAULT_RUN" \
    --golden-trace "$GOLDEN_TRACE" \
    --fault-trace "$FAULT_TRACE" \
    --report "$REPORT"

log "Gate 2 completed successfully"
echo "Golden compile run : $GOLDEN_RUN"
echo "Fault compile run  : $FAULT_RUN"
echo "Gate-2 report      : $REPORT"
echo "No simulation trace or VCD was generated."
