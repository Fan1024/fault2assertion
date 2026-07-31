#!/usr/bin/env bash
set -euo pipefail

F2A_ROOT="${F2A_ROOT:-/raid/spring2026/fwu44/research/fault2assertion}"
MINI_ROOT="$F2A_ROOT/runs/stage5_dev/mini_smoke_v1"
LOCK_ROOT="$F2A_ROOT/runs/stage5_locks"
STAMP="$(date +%Y%m%d_%H%M%S)"
EVIDENCE_ROOT="$F2A_ROOT/runs/stage5_evidence/native_rawfacts_pre_v300_${STAMP}"

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

[[ -d "$F2A_ROOT" ]] || fail "repository root not found: $F2A_ROOT"
[[ -d "$MINI_ROOT" ]] || fail "mini workspace not found: $MINI_ROOT"
mkdir -p "$EVIDENCE_ROOT/reports" "$EVIDENCE_ROOT/locks" "$EVIDENCE_ROOT/logs"

move_if_exists() {
    local source="$1"
    local destination_dir="$2"
    if [[ -e "$source" ]]; then
        mv -- "$source" "$destination_dir/"
        echo "Moved: $source"
    fi
}

# Preserve execution evidence.  Gate-1 campaign/specs/patches/monitors/manifests
# are intentionally left in place because Phase 1 does not change them.
move_if_exists "$MINI_ROOT/gate2_compile" "$EVIDENCE_ROOT"
move_if_exists "$MINI_ROOT/gate3_golden" "$EVIDENCE_ROOT"
move_if_exists "$MINI_ROOT/gate4_fault" "$EVIDENCE_ROOT"

for report in \
    runner_hardening_audit.json \
    gate2_compile_validation.json \
    gate3_golden_validation.json \
    gate4_single_fault_validation.json
 do
    move_if_exists "$MINI_ROOT/reports/$report" "$EVIDENCE_ROOT/reports"
 done

# Move any fault-run compact traces.  Golden split traces live under gate3_golden
# and were already moved with that directory.
if [[ -d "$MINI_ROOT/traces" ]]; then
    shopt -s nullglob
    for trace in "$MINI_ROOT"/traces/TF??????_SA?.trace.tsv; do
        move_if_exists "$trace" "$EVIDENCE_ROOT"
    done
    shopt -u nullglob
fi

move_if_exists "$LOCK_ROOT/mini_gate2_precompile_lock.json" "$EVIDENCE_ROOT/locks"
move_if_exists "$LOCK_ROOT/mini_gate2_execution_inputs.json" "$EVIDENCE_ROOT/locks"

shopt -s nullglob
for log in \
    "$F2A_ROOT"/runs/stage5_dev/runner_hardening*.log \
    "$F2A_ROOT"/runs/stage5_dev/gate2_compile*.log \
    "$F2A_ROOT"/runs/stage5_dev/gate3_golden*.log \
    "$F2A_ROOT"/runs/stage5_dev/gate4_single_fault*.log
 do
    move_if_exists "$log" "$EVIDENCE_ROOT/logs"
 done
shopt -u nullglob

cat > "$EVIDENCE_ROOT/README.txt" <<EOF
This directory preserves the last pre-v3.0 native-execution evidence.
It may include the TF000002_SA0 out_of_bounds_write ASRTST run.
The code itself was not backed up; Phase-1 installation directly replaced it.
Gate-1 campaign/specs/patches/monitors/manifests remain active in the mini root.
Created: $(date --iso-8601=seconds)
EOF

echo
echo "Phase-1 rerun workspace prepared."
echo "Evidence preserved: $EVIDENCE_ROOT"
echo "Gate-1 artifacts preserved in: $MINI_ROOT"
