#!/usr/bin/env bash
set -euo pipefail

F2A_ROOT="${F2A_ROOT:-/raid/spring2026/fwu44/research/fault2assertion}"

FC="$F2A_ROOT/scripts/fault_characterization"

MINI_ROOT="$F2A_ROOT/runs/stage5_dev/mini_smoke_v1"
G1_ROOT="$F2A_ROOT/runs/stage5_dev/phase2_g1_v2"

POLICY="$F2A_ROOT/platform/cv32e40p/stage5_phase2_g1_policy_v2.json"
G1_TOOL="$FC/stage5_phase2_g1_v2.py"

COMMON_VALIDATE="$FC/stage5_gate_validation_common.py"
COMPILE_VALIDATE="$FC/stage5_gate2_validate.py"

CAMPAIGN="$MINI_ROOT/campaign/stage_05_campaign.json"
SELECTION="$MINI_ROOT/provenance/smoke_fault_selection.json"

GOLDEN_BASE_MONITOR="$MINI_ROOT/monitors/stage5_golden_monitor.sv"
GOLDEN_BASE_MANIFEST="$MINI_ROOT/manifests/stage5_golden_monitor_manifest.json"

FAULT_MONITOR_ROOT="$MINI_ROOT/monitors/faults"
FAULT_MANIFEST_ROOT="$MINI_ROOT/manifests/faults"

GOLDEN_WRAPPER="$F2A_ROOT/scripts/run_xrun_stage5_golden.sh"
FAULT_WRAPPER="$F2A_ROOT/scripts/run_xrun_stage5_fault.sh"

GOLDEN_SOURCE="$G1_ROOT/generated/golden_phase2_g1.sv"
GOLDEN_METADATA="$G1_ROOT/generated/golden_phase2_g1.json"
GOLDEN_TRACE="$G1_ROOT/traces/golden_compile.trace.tsv"
GOLDEN_RUN="$G1_ROOT/runs/golden_compile"

FAULT_SOURCE="$G1_ROOT/generated/fault_phase2_g1.sv"
FAULT_METADATA="$G1_ROOT/generated/fault_phase2_g1.json"
FAULT_TRACE="$G1_ROOT/traces/fault_compile.trace.tsv"
FAULT_RUN="$G1_ROOT/runs/fault_compile"

STATIC_REPORT="$G1_ROOT/reports/source_validation.json"
COMPILE_REPORT="$G1_ROOT/reports/compile_validation.json"
FINAL_REPORT="$G1_ROOT/reports/phase2_g1_validation.json"


log() {
    printf '\n[%s] %s\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" \
        "$*"
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

log "Phase2-G1 v2 preflight"
echo "F2A_ROOT : $F2A_ROOT"
echo "PWD      : $PWD"
echo "G1_ROOT  : $G1_ROOT"


log "Check Phase2-G1 source inputs"

for file in \
    "$POLICY" \
    "$G1_TOOL" \
    "$COMMON_VALIDATE" \
    "$COMPILE_VALIDATE" \
    "$CAMPAIGN" \
    "$SELECTION" \
    "$GOLDEN_BASE_MONITOR" \
    "$GOLDEN_BASE_MANIFEST" \
    "$GOLDEN_WRAPPER" \
    "$FAULT_WRAPPER"
do
    require_file "$file" "Phase2-G1 input"
done


python3 -m json.tool "$POLICY" >/dev/null

python3 -m py_compile "$G1_TOOL"

python3 "$G1_TOOL" selftest

bash -n "$GOLDEN_WRAPPER"
bash -n "$FAULT_WRAPPER"


readarray -t META < <(
python3 - \
    "$CAMPAIGN" \
    "$SELECTION" \
    "$FAULT_MONITOR_ROOT" \
    "$FAULT_MANIFEST_ROOT" <<'PY'
import json
import sys
from pathlib import Path

campaign_path = Path(sys.argv[1]).resolve()
selection_path = Path(sys.argv[2]).resolve()
fault_monitor_root = Path(sys.argv[3]).resolve()
fault_manifest_root = Path(sys.argv[4]).resolve()

campaign = json.loads(
    campaign_path.read_text(encoding="utf-8")
)

selection = json.loads(
    selection_path.read_text(encoding="utf-8")
)

fault_id = str(selection["fault_id"])
fault_json = Path(
    str(selection["fault_spec"])
).resolve()

fault_monitor = (
    fault_monitor_root / f"{fault_id}.sv"
).resolve()

fault_manifest = (
    fault_manifest_root / f"{fault_id}.json"
).resolve()

golden_netlist = Path(
    str(campaign["mapped_netlist"]["path"])
).resolve()

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


log "Resolved Phase2-G1 inputs"
echo "FAULT_ID           : $FAULT_ID"
echo "FAULT_JSON         : $FAULT_JSON"
echo "FAULT_MONITOR      : $FAULT_BASE_MONITOR"
echo "FAULT_MANIFEST     : $FAULT_BASE_MANIFEST"
echo "GOLDEN_NETLIST     : $GOLDEN_NETLIST"


for file in \
    "$FAULT_JSON" \
    "$FAULT_BASE_MONITOR" \
    "$FAULT_BASE_MANIFEST" \
    "$GOLDEN_NETLIST"
do
    require_file "$file" "resolved Phase2-G1 input"
done


if [[ -e "$G1_ROOT" ]]; then
    fail "Phase2-G1 workspace already exists: $G1_ROOT"
fi


mkdir -p \
    "$G1_ROOT/generated" \
    "$G1_ROOT/traces" \
    "$G1_ROOT/runs" \
    "$G1_ROOT/reports"


log "Generate mode-aware golden compile source"

python3 "$G1_TOOL" prepare \
    --policy "$POLICY" \
    --base-monitor "$GOLDEN_BASE_MONITOR" \
    --base-manifest "$GOLDEN_BASE_MANIFEST" \
    --role golden \
    --trace-output "$GOLDEN_TRACE" \
    --output-source "$GOLDEN_SOURCE" \
    --output-metadata "$GOLDEN_METADATA"


log "Generate mode-aware fault compile source"

python3 "$G1_TOOL" prepare \
    --policy "$POLICY" \
    --base-monitor "$FAULT_BASE_MONITOR" \
    --base-manifest "$FAULT_BASE_MANIFEST" \
    --role fault \
    --trace-output "$FAULT_TRACE" \
    --output-source "$FAULT_SOURCE" \
    --output-metadata "$FAULT_METADATA"


log "Validate mode configuration and generated-source ownership"

python3 "$G1_TOOL" validate-sources \
    --policy "$POLICY" \
    --golden-source "$GOLDEN_SOURCE" \
    --golden-metadata "$GOLDEN_METADATA" \
    --fault-source "$FAULT_SOURCE" \
    --fault-metadata "$FAULT_METADATA" \
    --report "$STATIC_REPORT"


log "Compile and elaborate golden environment"

STAGE5_PHASE=compile \
STAGE5_RUN_PURPOSE=COMPILE_CHECK \
STAGE5_TRACE_OUTPUT="$GOLDEN_TRACE" \
GOLDEN_NETLIST="$GOLDEN_NETLIST" \
MAXCYCLES=2000000 \
VCD=0 \
KEEP_WORK=0 \
"$GOLDEN_WRAPPER" \
    "$GOLDEN_SOURCE" \
    "$GOLDEN_RUN"


log "Compile and elaborate one fault netlist"

STAGE5_PHASE=compile \
STAGE5_RUN_PURPOSE=COMPILE_CHECK \
STAGE5_TRACE_OUTPUT="$FAULT_TRACE" \
MAXCYCLES=2000000 \
VCD=0 \
KEEP_WORK=0 \
"$FAULT_WRAPPER" \
    "$FAULT_JSON" \
    "$FAULT_SOURCE" \
    "$FAULT_RUN"


log "Confirm original mm_ram.sv was used"

grep -Fq \
    "/verification/shared/tb/mm_ram.sv" \
    "$GOLDEN_RUN/command.txt" \
    || fail "golden compile did not use original mm_ram.sv"

grep -Fq \
    "/verification/shared/tb/mm_ram.sv" \
    "$FAULT_RUN/command.txt" \
    || fail "fault compile did not use original mm_ram.sv"


if grep -Rqs \
    "mm_ram.stage5.sv" \
    "$GOLDEN_RUN" \
    "$FAULT_RUN"
then
    fail "Phase2-G1 unexpectedly used mm_ram.stage5.sv"
fi


log "Validate compile and elaboration results"

python3 "$COMPILE_VALIDATE" \
    --common "$COMMON_VALIDATE" \
    --golden-run "$GOLDEN_RUN" \
    --fault-run "$FAULT_RUN" \
    --golden-trace "$GOLDEN_TRACE" \
    --fault-trace "$FAULT_TRACE" \
    --report "$COMPILE_REPORT"


log "Finalize Phase2-G1 report"

python3 "$G1_TOOL" finalize \
    --policy "$POLICY" \
    --static-report "$STATIC_REPORT" \
    --compile-report "$COMPILE_REPORT" \
    --report "$FINAL_REPORT"


log "Phase2-G1 completed successfully"

echo "Fault ID           : $FAULT_ID"
echo "Workspace          : $G1_ROOT"
echo "Static report      : $STATIC_REPORT"
echo "Compile report     : $COMPILE_REPORT"
echo "Final report       : $FINAL_REPORT"
echo "Observe runtime    : NOT EXECUTED"
echo "Quarantine runtime : NOT EXECUTED"
echo "mm_ram overlay     : NOT USED"
