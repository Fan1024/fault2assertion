#!/usr/bin/env bash
set -euo pipefail

F2A_ROOT="${F2A_ROOT:-/raid/spring2026/fwu44/research/fault2assertion}"
FC="$F2A_ROOT/scripts/fault_characterization"
MINI_ROOT="$F2A_ROOT/runs/stage5_dev/mini_smoke_v1"
PHASE_ROOT="$F2A_ROOT/runs/stage5_dev/phase23_smoke_v1"
CAMPAIGN="$MINI_ROOT/campaign/stage_05_campaign.json"
SELECTION="$MINI_ROOT/provenance/smoke_fault_selection.json"
POLICY="$F2A_ROOT/platform/cv32e40p/stage5_assertion_policy_v1.json"
PREP_MM_RAM="$F2A_ROOT/platform/cv32e40p/prepare_stage5_mm_ram.py"
STAGE5_TOOL="$FC/stage5_faults.py"
SELFTEST="$FC/stage5_phase23_selftest.py"
LOCK_TOOL="$FC/stage5_execution_input_lock.py"
INVENTORY_TOOL="$FC/stage5_detector_inventory.py"
INVENTORY_REPORT="$PHASE_ROOT/static/preexisting_detector_inventory.json"
LOCK="$F2A_ROOT/runs/stage5_locks/phase23_smoke_execution_inputs.json"

log() { printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail() { echo "ERROR: $*" >&2; exit 1; }
require_file() { [[ -s "$1" ]] || fail "missing or empty file: $1"; }

cd "$F2A_ROOT"
[[ ! -e "$PHASE_ROOT" ]] || fail "Phase-2/3 root already exists: $PHASE_ROOT"
[[ ! -e "$LOCK" ]] || fail "Phase-2/3 input lock already exists: $LOCK"

log "Verify Phase 1 completed and reusable mini artifacts exist"
for file in \
  "$MINI_ROOT/reports/gate1_static_validation.json" \
  "$MINI_ROOT/reports/gate4_single_fault_validation.json" \
  "$CAMPAIGN" "$SELECTION" "$POLICY" "$PREP_MM_RAM" "$STAGE5_TOOL" "$SELFTEST" "$LOCK_TOOL" "$INVENTORY_TOOL"
do
  require_file "$file"
done
python3 - \
  "$MINI_ROOT/reports/gate1_static_validation.json" \
  "$MINI_ROOT/reports/gate4_single_fault_validation.json" <<'PY'
import json, sys
from pathlib import Path
for raw in sys.argv[1:]:
    path = Path(raw)
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("status") != "PASS":
        raise SystemExit(f"ERROR: reusable Phase-1 report is not PASS: {path}")
print("Phase-1 prerequisite reports: PASS")
PY

log "Run synthetic Phase-2/3 semantic and replay self-test"
python3 "$SELFTEST"

mkdir -p \
  "$PHASE_ROOT/monitors/faults" \
  "$PHASE_ROOT/manifests/faults" \
  "$PHASE_ROOT/traces" \
  "$PHASE_ROOT/reports" \
  "$PHASE_ROOT/provenance" \
  "$PHASE_ROOT/static"

log "Resolve the deterministic smoke fault and write Phase-2/3 metadata"
python3 - "$CAMPAIGN" "$SELECTION" "$PHASE_ROOT/provenance/smoke_metadata.json" <<'PY'
import json, sys
from pathlib import Path
campaign_path, selection_path, output = map(Path, sys.argv[1:])
campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
selection = json.loads(selection_path.read_text(encoding="utf-8"))
fault_id = selection["fault_id"]
fault_spec = Path(selection["fault_spec"]).resolve()
spec = json.loads(fault_spec.read_text(encoding="utf-8"))
if spec["fault_id"] != fault_id:
    raise SystemExit("ERROR: smoke selection/spec fault ID mismatch")
payload = {
    "schema_version": "1.0",
    "kind": "stage5_phase23_smoke_metadata",
    "campaign": str(campaign_path.resolve()),
    "campaign_digest_sha256": campaign.get("campaign_digest_sha256"),
    "fault_id": fault_id,
    "selection_id": spec["selection_id"],
    "fault_spec": str(fault_spec),
    "mapped_netlist": str(Path(campaign["mapped_netlist"]["path"]).resolve()),
}
output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
print(f"Smoke fault: {fault_id}")
PY

readarray -t META < <(python3 - "$PHASE_ROOT/provenance/smoke_metadata.json" <<'PY'
import json, sys
from pathlib import Path
m=json.loads(Path(sys.argv[1]).read_text())
print(m["fault_id"]); print(m["fault_spec"]); print(m["mapped_netlist"])
PY
)
[[ "${#META[@]}" -eq 3 ]] || fail "failed to read smoke metadata"
FAULT_ID="${META[0]}"
FAULT_JSON="${META[1]}"
MAPPED_NETLIST="${META[2]}"

GOLDEN_TRACE="$PHASE_ROOT/traces/golden_all.trace.tsv"
GOLDEN_MONITOR="$PHASE_ROOT/monitors/stage5_golden_monitor.sv"
GOLDEN_MANIFEST="$PHASE_ROOT/manifests/stage5_golden_monitor_manifest.json"

log "Generate a fresh comprehensive golden monitor for Phase 2/3"
python3 "$STAGE5_TOOL" make-golden-monitor \
  --campaign "$CAMPAIGN" \
  --trace-output "$GOLDEN_TRACE" \
  --output "$GOLDEN_MONITOR" \
  --manifest "$GOLDEN_MANIFEST" \
  --force

for mode in native observe diagnostic_quarantine; do
  trace="$PHASE_ROOT/traces/${FAULT_ID}.${mode}.trace.tsv"
  monitor="$PHASE_ROOT/monitors/faults/${FAULT_ID}.${mode}.sv"
  manifest="$PHASE_ROOT/manifests/faults/${FAULT_ID}.${mode}.json"
  log "Generate exact ${mode} fault monitor"
  python3 "$STAGE5_TOOL" make-fault-monitor \
    --fault-json "$FAULT_JSON" \
    --trace-output "$trace" \
    --output "$monitor" \
    --manifest "$manifest" \
    --force
done

log "Transform the real external mm_ram source and validate all three modes"
# shellcheck disable=SC1091
source "$F2A_ROOT/scripts/setup_env.sh"
CV_HOME="${CV32E40P_HOME:-/raid/spring2026/fwu44/research/cv32e40p}"
CELL_MODEL="${CV32E40P_CELL_MODEL:-}"
require_file "$CV_HOME/verification/shared/tb/mm_ram.sv"
require_file "$CELL_MODEL"
require_file "$MAPPED_NETLIST"

log "Inventory pre-existing assertions and terminal points"
python3 "$INVENTORY_TOOL" \
  --policy "$POLICY" \
  --source "$CV_HOME/verification/shared/tb/amo_shim.sv" \
  --source "$CV_HOME/verification/shared/tb/cv32e40p_random_interrupt_generator.sv" \
  --source "$CV_HOME/verification/shared/tb/dp_ram.sv" \
  --source "$CV_HOME/verification/shared/tb/riscv_gnt_stall.sv" \
  --source "$CV_HOME/verification/shared/tb/riscv_rvalid_stall.sv" \
  --source "$CV_HOME/verification/shared/tb/mm_ram.sv" \
  --source "$F2A_ROOT/platform/cv32e40p/tb/cv32e40p_tb_subsystem.sv" \
  --source "$CV_HOME/verification/shared/tb/tb_top.sv" \
  --output "$INVENTORY_REPORT"

python3 "$PREP_MM_RAM" \
  "$CV_HOME/verification/shared/tb/mm_ram.sv" \
  "$PHASE_ROOT/static/mm_ram.stage5.sv" \
  --policy "$POLICY" \
  --report "$PHASE_ROOT/static/mm_ram_preparation.json"

log "Freeze exact Phase-2/3 execution inputs"
python3 "$LOCK_TOOL" create \
  --repo-root "$F2A_ROOT" \
  --cv32e40p-home "$CV_HOME" \
  --cell-model "$CELL_MODEL" \
  --mapped-netlist "$MAPPED_NETLIST" \
  --monitor "$GOLDEN_MONITOR" \
  --monitor "$PHASE_ROOT/monitors/faults/${FAULT_ID}.native.sv" \
  --monitor "$PHASE_ROOT/monitors/faults/${FAULT_ID}.observe.sv" \
  --monitor "$PHASE_ROOT/monitors/faults/${FAULT_ID}.diagnostic_quarantine.sv" \
  --output "$LOCK" \
  --force
python3 "$LOCK_TOOL" verify --lock "$LOCK"

log "Write Phase-2/3 static-preparation report"
python3 - \
  "$PHASE_ROOT/reports/phase23_static_validation.json" \
  "$PHASE_ROOT/provenance/smoke_metadata.json" \
  "$PHASE_ROOT/static/mm_ram_preparation.json" \
  "$INVENTORY_REPORT" \
  "$LOCK" <<'PY'
import hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path

def sha(p):
    h=hashlib.sha256(); h.update(p.read_bytes()); return h.hexdigest()
out, metadata, prep, inventory, lock = map(Path, sys.argv[1:])
payload={
  "schema_version":"1.0",
  "generated_at_utc":datetime.now(timezone.utc).isoformat(),
  "kind":"stage5_phase23_static_validation",
  "status":"PASS",
  "smoke_metadata":str(metadata.resolve()),
  "smoke_metadata_sha256":sha(metadata),
  "mm_ram_preparation":str(prep.resolve()),
  "mm_ram_preparation_sha256":sha(prep),
  "detector_inventory":str(inventory.resolve()),
  "detector_inventory_sha256":sha(inventory),
  "execution_input_lock":str(lock.resolve()),
  "execution_input_lock_sha256":sha(lock),
  "contracts":{
    "external_mm_ram_source_not_modified":True,
    "native_mode_preserves_preexisting_fatal_action":True,
    "observe_mode_suppresses_termination_only":True,
    "quarantine_mode_acknowledges_and_drops_unsafe_write":True,
    "diagnostic_modes_are_counterfactual_after_first_event":True,
    "ai_assertion_generation_out_of_scope":True,
    "known_detector_smoke_inventory_complete":True,
    "unrestricted_full_campaign_requires_inventory_blockers_to_be_resolved":True,
  }
}
out.write_text(json.dumps(payload,indent=2)+"\n",encoding="utf-8")
PY

echo
printf 'Phase-2/3 static preparation: PASS\n'
printf 'Phase root              : %s\n' "$PHASE_ROOT"
printf 'Smoke fault             : %s\n' "$FAULT_ID"
printf 'Execution input lock    : %s\n' "$LOCK"
printf 'No Xcelium simulation was executed.\n'
