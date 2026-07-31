#!/usr/bin/env bash
set -euo pipefail

# Rebuild only the isolated Stage-5 mini Gate 1 after the v1.0.8
# declaration-before-use materialization correction.
#
# This script intentionally does NOT:
#   * rerun Stage 0;
#   * overwrite the canonical 1056-fault full campaign;
#   * invoke Xcelium;
#   * commit or push Git changes.

F2A_ROOT="${F2A_ROOT:-/raid/spring2026/fwu44/research/fault2assertion}"
FC="$F2A_ROOT/scripts/fault_characterization"
STAGE5_TOOL="$FC/stage5_faults.py"
PRESERVED_IMPL="$FC/stage5_faults_v107_impl.py"
VERSION_GUARD="$FC/stage5_version_guard.py"
LOCK_VERIFY="$FC/stage5_lock_verify.py"
GUARD_SELFTEST="$FC/stage5_guard_selftest.py"
MINI_SELECTOR="$FC/make_stage5_mini_selection.py"
GATE1_VALIDATE="$FC/stage5_gate1_validate.py"
LAYOUT_SELFTEST="$FC/stage5_materialization_layout_selftest.py"
ORDER_VALIDATE="$FC/stage5_materialization_order_validate.py"
SITE_CATALOG_TOOL="$F2A_ROOT/scripts/fault_sites/site_catalog.py"
SITE_POLICY="$F2A_ROOT/platform/cv32e40p/fault_site_policy.json"
PREPARE_NETLIST="$F2A_ROOT/platform/cv32e40p/prepare_netlist.py"

CANDIDATES="$F2A_ROOT/faults/cv32e40p/site_catalog/stage_04_candidates.json"
PARENT_SELECTION="$F2A_ROOT/faults/cv32e40p/site_catalog/stage_04_selection.json"
FULL_CAMPAIGN="$F2A_ROOT/faults/cv32e40p/stage5/stage_05_campaign.json"

MINI_ROOT="$F2A_ROOT/runs/stage5_dev/mini_smoke_v1"
MINI_INPUT_ROOT="$MINI_ROOT/input"
MINI_CAMPAIGN_ROOT="$MINI_ROOT/campaign"
MINI_MONITOR_ROOT="$MINI_ROOT/monitors"
MINI_FAULT_MONITOR_ROOT="$MINI_MONITOR_ROOT/faults"
MINI_MANIFEST_ROOT="$MINI_ROOT/manifests"
MINI_FAULT_MANIFEST_ROOT="$MINI_MANIFEST_ROOT/faults"
MINI_TRACE_ROOT="$MINI_ROOT/traces"
MINI_REPORT_ROOT="$MINI_ROOT/reports"
MINI_PROVENANCE_ROOT="$MINI_ROOT/provenance"
MINI_STATIC_SCRATCH="$MINI_ROOT/static_materialization_scratch"

MINI_SELECTION="$MINI_INPUT_ROOT/stage_04_mini_selection.json"
MINI_CAMPAIGN="$MINI_CAMPAIGN_ROOT/stage_05_campaign.json"
MINI_GOLDEN_MONITOR="$MINI_MONITOR_ROOT/stage5_golden_monitor.sv"
MINI_GOLDEN_MANIFEST="$MINI_MANIFEST_ROOT/stage5_golden_monitor_manifest.json"
MINI_GOLDEN_TRACE="$MINI_TRACE_ROOT/golden_all.trace.tsv"
MINI_GATE1_REPORT="$MINI_REPORT_ROOT/gate1_static_validation.json"
MINI_ORDER_REPORT="$MINI_REPORT_ROOT/materialization_order_validation.json"
MINI_AUDIT="$MINI_PROVENANCE_ROOT/version_audit_v108.json"

LOCK_ROOT="$F2A_ROOT/runs/stage5_locks"
LOCK_ARCHIVE_ROOT="$LOCK_ROOT/archive"
MINI_LOCK="$LOCK_ROOT/mini_gate1_lock.json"
ARCHIVE_BASE="$F2A_ROOT/runs/stage5_failed_archive"
STAMP="$(date +%Y%m%d_%H%M%S)"
ARCHIVE_ROOT="$ARCHIVE_BASE/dupidn_v107_${STAMP}"

log() {
    printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

require_file() {
    [[ -s "$1" ]] || fail "missing or empty required file: $1"
}

archive_if_exists() {
    local source="$1"
    local destination_name="$2"
    if [[ -e "$source" ]]; then
        mkdir -p "$ARCHIVE_ROOT"
        mv -- "$source" "$ARCHIVE_ROOT/$destination_name"
    fi
}

cd "$F2A_ROOT"
mkdir -p "$LOCK_ROOT" "$LOCK_ARCHIVE_ROOT" "$ARCHIVE_BASE"

log "Verify v1.0.8 source and immutable parent inputs"
for file in \
    "$STAGE5_TOOL" \
    "$PRESERVED_IMPL" \
    "$VERSION_GUARD" \
    "$LOCK_VERIFY" \
    "$GUARD_SELFTEST" \
    "$MINI_SELECTOR" \
    "$GATE1_VALIDATE" \
    "$LAYOUT_SELFTEST" \
    "$ORDER_VALIDATE" \
    "$SITE_CATALOG_TOOL" \
    "$SITE_POLICY" \
    "$PREPARE_NETLIST" \
    "$CANDIDATES" \
    "$PARENT_SELECTION" \
    "$FULL_CAMPAIGN"
do
    require_file "$file"
done

python3 "$STAGE5_TOOL" --version | grep -q '1.0.8' || fail \
    "active Stage-5 tool is not version 1.0.8"
python3 - "$FULL_CAMPAIGN" <<'PY'
import json
import sys
from pathlib import Path
campaign = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if campaign.get("program_version") != "1.0.7":
    raise SystemExit(
        "ERROR: expected the preserved canonical full campaign to remain at 1.0.7"
    )
print("Canonical full campaign remains preserved at version 1.0.7: PASS")
PY

log "Archive the complete old mini workspace and obsolete mini locks"
if [[ -e "$MINI_ROOT" ]]; then
    mkdir -p "$ARCHIVE_ROOT"
    mv -- "$MINI_ROOT" "$ARCHIVE_ROOT/mini_smoke_v1_v107"
fi
archive_if_exists "$LOCK_ROOT/mini_gate1_lock.json" "mini_gate1_lock_v107.json"
archive_if_exists "$LOCK_ROOT/mini_gate2_precompile_lock.json" "mini_gate2_precompile_lock_v107.json"
archive_if_exists "$LOCK_ROOT/mini_gate2_execution_inputs.json" "mini_gate2_execution_inputs_v107.json"
archive_if_exists "$F2A_ROOT/runs/stage5_dev/gate2_compile.log" "gate2_compile_v107.log"
archive_if_exists "$F2A_ROOT/runs/stage5_dev/runner_hardening.log" "runner_hardening_v107.log"

log "Create a fresh isolated mini Gate-1 workspace"
mkdir -p \
    "$MINI_INPUT_ROOT" \
    "$MINI_CAMPAIGN_ROOT" \
    "$MINI_MONITOR_ROOT" \
    "$MINI_FAULT_MONITOR_ROOT" \
    "$MINI_MANIFEST_ROOT" \
    "$MINI_FAULT_MANIFEST_ROOT" \
    "$MINI_TRACE_ROOT" \
    "$MINI_REPORT_ROOT" \
    "$MINI_PROVENANCE_ROOT"

log "Compile all static tools"
python3 -m py_compile \
    "$STAGE5_TOOL" \
    "$PRESERVED_IMPL" \
    "$VERSION_GUARD" \
    "$LOCK_VERIFY" \
    "$GUARD_SELFTEST" \
    "$MINI_SELECTOR" \
    "$GATE1_VALIDATE" \
    "$LAYOUT_SELFTEST" \
    "$ORDER_VALIDATE" \
    "$SITE_CATALOG_TOOL" \
    "$PREPARE_NETLIST"

log "Run digest and declaration-layout regression self-tests"
python3 "$GUARD_SELFTEST" \
    --stage5-tool "$STAGE5_TOOL" \
    --version-guard "$VERSION_GUARD"
python3 "$LAYOUT_SELFTEST" \
    --stage5-tool "$STAGE5_TOOL"

log "Recreate the deterministic four-class mini selection"
python3 "$MINI_SELECTOR" \
    --candidates "$CANDIDATES" \
    --parent-selection "$PARENT_SELECTION" \
    --parent-campaign "$FULL_CAMPAIGN" \
    --output "$MINI_SELECTION" \
    --max-receivers 32 \
    --force

log "Regenerate the 4-site/8-fault mini campaign with Stage-5 v1.0.8"
python3 "$STAGE5_TOOL" prepare \
    --candidates "$CANDIDATES" \
    --selection "$MINI_SELECTION" \
    --site-catalog-tool "$SITE_CATALOG_TOOL" \
    --policy "$SITE_POLICY" \
    --output-root "$MINI_CAMPAIGN_ROOT" \
    --force

log "Run built-in Stage-5 metadata validation"
python3 "$STAGE5_TOOL" validate \
    --campaign "$MINI_CAMPAIGN"

log "Generate the mini golden monitor"
python3 "$STAGE5_TOOL" make-golden-monitor \
    --campaign "$MINI_CAMPAIGN" \
    --trace-output "$MINI_GOLDEN_TRACE" \
    --output "$MINI_GOLDEN_MONITOR" \
    --manifest "$MINI_GOLDEN_MANIFEST" \
    --force

log "Generate all eight local fault monitors"
while IFS=$'\t' read -r fault_id fault_spec; do
    [[ -n "$fault_id" && -n "$fault_spec" ]] || fail \
        "invalid campaign fault row"
    python3 "$STAGE5_TOOL" make-fault-monitor \
        --fault-json "$fault_spec" \
        --trace-output "$MINI_TRACE_ROOT/${fault_id}.trace.tsv" \
        --output "$MINI_FAULT_MONITOR_ROOT/${fault_id}.sv" \
        --manifest "$MINI_FAULT_MANIFEST_ROOT/${fault_id}.json" \
        --force
done < <(
    python3 - "$MINI_CAMPAIGN" <<'PY'
import json
import sys
from pathlib import Path
campaign = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for record in sorted(campaign["faults"], key=lambda item: item["fault_id"]):
    print(f"{record['fault_id']}\t{record['fault_spec']}")
PY
)

log "Run the original fail-closed Gate-1 artifact validator"
python3 "$GATE1_VALIDATE" \
    --stage5-tool "$STAGE5_TOOL" \
    --candidates "$CANDIDATES" \
    --parent-selection "$PARENT_SELECTION" \
    --parent-campaign "$FULL_CAMPAIGN" \
    --mini-selection "$MINI_SELECTION" \
    --mini-campaign "$MINI_CAMPAIGN" \
    --golden-monitor "$MINI_GOLDEN_MONITOR" \
    --golden-manifest "$MINI_GOLDEN_MANIFEST" \
    --fault-monitor-dir "$MINI_FAULT_MONITOR_ROOT" \
    --fault-manifest-dir "$MINI_FAULT_MANIFEST_ROOT" \
    --trace-dir "$MINI_TRACE_ROOT" \
    --mini-root "$MINI_ROOT" \
    --report "$MINI_GATE1_REPORT" \
    --max-receivers 32

log "Apply and prepare all eight faults, then validate declaration-before-use"
python3 "$ORDER_VALIDATE" \
    --stage5-tool "$STAGE5_TOOL" \
    --campaign "$MINI_CAMPAIGN" \
    --prepare-netlist "$PREPARE_NETLIST" \
    --scratch-root "$MINI_STATIC_SCRATCH" \
    --report "$MINI_ORDER_REPORT"

log "Audit regenerated mini artifacts and write the new Gate-1 lock"
monitor_args=(--monitor "$MINI_GOLDEN_MONITOR")
manifest_args=(--manifest "$MINI_GOLDEN_MANIFEST")
for monitor in "$MINI_FAULT_MONITOR_ROOT"/*.sv; do
    monitor_args+=(--monitor "$monitor")
done
for manifest in "$MINI_FAULT_MANIFEST_ROOT"/*.json; do
    manifest_args+=(--manifest "$manifest")
done

python3 "$VERSION_GUARD" \
    --repo-root "$F2A_ROOT" \
    --tool "$STAGE5_TOOL" \
    --campaign "$MINI_CAMPAIGN" \
    "${monitor_args[@]}" \
    "${manifest_args[@]}" \
    --report "$MINI_AUDIT" \
    --write-lock "$MINI_LOCK"

HEAD_SHORT="$(git rev-parse --short=12 HEAD)"
cp -- "$MINI_LOCK" \
    "$LOCK_ARCHIVE_ROOT/mini_gate1_v108_${HEAD_SHORT}_${STAMP}.json"

log "Immediately verify the new mini Gate-1 lock"
python3 "$LOCK_VERIFY" \
    --repo-root "$F2A_ROOT" \
    --lock "$MINI_LOCK"

log "Final Gate-1 no-simulation/no-permanent-netlist checks"
[[ ! -e "$MINI_GOLDEN_TRACE" ]] || fail \
    "golden trace exists unexpectedly during static Gate 1"
if find "$MINI_TRACE_ROOT" -type f -name '*.tsv' -print -quit | grep -q .; then
    fail "fault trace exists unexpectedly during static Gate 1"
fi
if find "$MINI_ROOT" -type f \
    \( -name '*.vcd' -o -name 'fault_netlist.v' -o -name '*.mapped.sim.v' \) \
    -print -quit | grep -q .; then
    fail "temporary simulation/netlist artifact remains after Gate 1"
fi

log "Mini Gate 1 v1.0.8 completed successfully"
echo "Archived v1.0.7 evidence : $ARCHIVE_ROOT"
echo "Mini campaign             : $MINI_CAMPAIGN"
echo "Gate-1 report             : $MINI_GATE1_REPORT"
echo "Order-validation report   : $MINI_ORDER_REPORT"
echo "Mini Gate-1 lock          : $MINI_LOCK"
echo
echo "Stage 0 and the canonical full 1.0.7 campaign were not modified."
echo "No Xcelium run, commit, or push was performed."
echo
git status --short
