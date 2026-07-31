#!/usr/bin/env bash
set -euo pipefail

F2A_ROOT="${F2A_ROOT:-/raid/spring2026/fwu44/research/fault2assertion}"

FC="$F2A_ROOT/scripts/fault_characterization"
MINI_ROOT="$F2A_ROOT/runs/stage5_dev/mini_smoke_v1"
PHASE2_ROOT="$F2A_ROOT/runs/stage5_dev/phase2_v1"
G1_ROOT="$PHASE2_ROOT/g1_mode_infrastructure"

POLICY="$F2A_ROOT/platform/cv32e40p/stage5_phase2_execution_policy_v1.json"
MODE_TOOL="$FC/stage5_phase2_modes.py"

MINI_CAMPAIGN="$MINI_ROOT/campaign/stage_05_campaign.json"
SMOKE_SELECTION="$MINI_ROOT/provenance/smoke_fault_selection.json"

GOLDEN_BASE_MONITOR="$MINI_ROOT/monitors/stage5_golden_monitor.sv"
GOLDEN_BASE_MANIFEST="$MINI_ROOT/manifests/stage5_golden_monitor_manifest.json"

FAULT_MONITOR_ROOT="$MINI_ROOT/monitors/faults"
FAULT_MANIFEST_ROOT="$MINI_ROOT/manifests/faults"

GOLDEN_WRAPPER="$F2A_ROOT/scripts/run_xrun_stage5_golden.sh"
FAULT_WRAPPER="$F2A_ROOT/scripts/run_xrun_stage5_fault.sh"

CASES_JSON="$G1_ROOT/phase2_g1_cases.json"
REPORT="$G1_ROOT/phase2_g1_validation.json"


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
    case "$G1_ROOT" in
        "$F2A_ROOT"/runs/stage5_dev/phase2_v1/g1_mode_infrastructure) ;;
        *) fail "unsafe Phase2-G1 root: $G1_ROOT" ;;
    esac

    if [[ -e "$G1_ROOT" ]]; then
        fail "Phase2-G1 workspace already exists. Preserve or move it before rerun: $G1_ROOT"
    fi

    mkdir -p \
        "$G1_ROOT/generated_monitors" \
        "$G1_ROOT/mode_metadata" \
        "$G1_ROOT/runs" \
        "$G1_ROOT/traces" \
        "$G1_ROOT/logs"
}


compose_monitor() {
    local name="$1"
    local mode="$2"
    local base_monitor="$3"
    local base_manifest="$4"
    local trace="$5"

    python3 "$MODE_TOOL" compose \
        --policy "$POLICY" \
        --base-monitor "$base_monitor" \
        --base-manifest "$base_manifest" \
        --mode "$mode" \
        --trace-output "$trace" \
        --output-monitor "$G1_ROOT/generated_monitors/${name}.sv" \
        --output-metadata "$G1_ROOT/mode_metadata/${name}.json"
}


run_golden_compile() {
    local name="$1"
    local monitor="$G1_ROOT/generated_monitors/${name}.sv"
    local trace="$G1_ROOT/traces/${name}.trace.tsv"
    local run_dir="$G1_ROOT/runs/${name}"

    STAGE5_PHASE=compile \
    STAGE5_RUN_PURPOSE=COMPILE_CHECK \
    STAGE5_TRACE_OUTPUT="$trace" \
    GOLDEN_NETLIST="$GOLDEN_NETLIST" \
    MAXCYCLES=2000000 \
    VCD=0 \
    KEEP_WORK=0 \
    "$GOLDEN_WRAPPER" "$monitor" "$run_dir"
}


run_fault_compile() {
    local name="$1"
    local monitor="$G1_ROOT/generated_monitors/${name}.sv"
    local trace="$G1_ROOT/traces/${name}.trace.tsv"
    local run_dir="$G1_ROOT/runs/${name}"

    STAGE5_PHASE=compile \
    STAGE5_RUN_PURPOSE=COMPILE_CHECK \
    STAGE5_TRACE_OUTPUT="$trace" \
    MAXCYCLES=2000000 \
    VCD=0 \
    KEEP_WORK=0 \
    "$FAULT_WRAPPER" "$FAULT_JSON" "$monitor" "$run_dir"
}


cd "$F2A_ROOT"

log "Validate Phase-2 source inputs"
for file in \
    "$POLICY" \
    "$MODE_TOOL" \
    "$MINI_CAMPAIGN" \
    "$SMOKE_SELECTION" \
    "$GOLDEN_BASE_MONITOR" \
    "$GOLDEN_BASE_MANIFEST" \
    "$GOLDEN_WRAPPER" \
    "$FAULT_WRAPPER"
do
    require_file "$file" "Phase2-G1 input"
done

python3 -m json.tool "$POLICY" >/dev/null
python3 -m py_compile "$MODE_TOOL"

readarray -t META < <(
python3 - "$MINI_CAMPAIGN" "$SMOKE_SELECTION" "$FAULT_MONITOR_ROOT" "$FAULT_MANIFEST_ROOT" <<'PY'
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

[[ "${#META[@]}" -eq 5 ]] || fail "failed to resolve Phase2-G1 metadata"

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

log "Create fresh Phase2-G1 workspace"
create_fresh_root

log "Generate mode-aware compile monitors"

compose_monitor \
    golden_native_compile \
    NATIVE \
    "$GOLDEN_BASE_MONITOR" \
    "$GOLDEN_BASE_MANIFEST" \
    "$G1_ROOT/traces/golden_native_compile.trace.tsv"

compose_monitor \
    fault_native_compile \
    NATIVE \
    "$FAULT_BASE_MONITOR" \
    "$FAULT_BASE_MANIFEST" \
    "$G1_ROOT/traces/fault_native_compile.trace.tsv"

compose_monitor \
    fault_observe_safe_compile \
    OBSERVE_SAFE \
    "$FAULT_BASE_MONITOR" \
    "$FAULT_BASE_MANIFEST" \
    "$G1_ROOT/traces/fault_observe_safe_compile.trace.tsv"

compose_monitor \
    fault_quarantine_compile \
    QUARANTINE \
    "$FAULT_BASE_MONITOR" \
    "$FAULT_BASE_MANIFEST" \
    "$G1_ROOT/traces/fault_quarantine_compile.trace.tsv"

log "Verify NON_CONTINUABLE is rejected as an execution mode"

set +e
python3 "$MODE_TOOL" compose \
    --policy "$POLICY" \
    --base-monitor "$FAULT_BASE_MONITOR" \
    --base-manifest "$FAULT_BASE_MANIFEST" \
    --mode NON_CONTINUABLE \
    --trace-output "$G1_ROOT/traces/non_continuable.trace.tsv" \
    --output-monitor "$G1_ROOT/generated_monitors/non_continuable.sv" \
    --output-metadata "$G1_ROOT/mode_metadata/non_continuable.json" \
    >"$G1_ROOT/logs/non_continuable.stdout.log" \
    2>"$G1_ROOT/logs/non_continuable.stderr.log"
NON_CONTINUABLE_STATUS=$?
set -e

if [[ "$NON_CONTINUABLE_STATUS" -eq 0 ]]; then
    fail "NON_CONTINUABLE was incorrectly accepted as an execution mode"
fi

if [[ -e "$G1_ROOT/generated_monitors/non_continuable.sv" ]]; then
    fail "NON_CONTINUABLE unexpectedly generated a monitor"
fi

grep -q \
    'detector capability, not an execution mode' \
    "$G1_ROOT/logs/non_continuable.stderr.log" \
    || fail "NON_CONTINUABLE rejection reason is missing"

log "Compile/elaborate Phase2-G1 golden NATIVE"
run_golden_compile golden_native_compile

log "Compile/elaborate Phase2-G1 fault NATIVE"
run_fault_compile fault_native_compile

log "Compile/elaborate Phase2-G1 fault OBSERVE_SAFE infrastructure"
run_fault_compile fault_observe_safe_compile

log "Compile/elaborate Phase2-G1 fault QUARANTINE infrastructure"
run_fault_compile fault_quarantine_compile

log "Write deterministic Phase2-G1 case list"

python3 - "$CASES_JSON" "$G1_ROOT" "$NON_CONTINUABLE_STATUS" "$FAULT_ID" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

output = Path(sys.argv[1]).resolve()
root = Path(sys.argv[2]).resolve()
non_continuable_status = int(sys.argv[3])
fault_id = sys.argv[4]

case_specs = [
    ("golden_native_compile", "NATIVE", "golden"),
    ("fault_native_compile", "NATIVE", "fault"),
    ("fault_observe_safe_compile", "OBSERVE_SAFE", "fault"),
    ("fault_quarantine_compile", "QUARANTINE", "fault"),
]

payload = {
    "schema_version": "1.0",
    "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    "fault_id": fault_id,
    "cases": [
        {
            "name": name,
            "mode": mode,
            "run_kind": run_kind,
            "run_dir": str(root / "runs" / name),
            "trace": str(root / "traces" / f"{name}.trace.tsv"),
            "monitor": str(root / "generated_monitors" / f"{name}.sv"),
            "metadata": str(root / "mode_metadata" / f"{name}.json"),
        }
        for name, mode, run_kind in case_specs
    ],
    "non_continuable_rejection": {
        "rejected": non_continuable_status != 0,
        "exit_status": non_continuable_status,
        "stderr": str(root / "logs" / "non_continuable.stderr.log"),
    },
}
output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY

log "Validate Phase2-G1 fail-closed"

python3 "$MODE_TOOL" validate-g1 \
    --policy "$POLICY" \
    --cases "$CASES_JSON" \
    --report "$REPORT"

log "Phase2-G1 completed successfully"

echo
echo "======================================================================"
echo "Phase2-G1 PASS"
echo "======================================================================"
echo "Fault ID          : $FAULT_ID"
echo "Workspace         : $G1_ROOT"
echo "Validation report : $REPORT"
echo "No diagnostic continuation runtime was executed."
echo "No external cv32e40p source was modified."
echo "No VCD was generated."
