#!/usr/bin/env bash
set -euo pipefail

F2A_ROOT="${F2A_ROOT:-/raid/spring2026/fwu44/research/fault2assertion}"
FC="$F2A_ROOT/scripts/fault_characterization"
MINI_ROOT="$F2A_ROOT/runs/stage5_dev/mini_smoke_v1"
MINI_CAMPAIGN="$MINI_ROOT/campaign/stage_05_campaign.json"
MINI_GOLDEN_MONITOR="$MINI_ROOT/monitors/stage5_golden_monitor.sv"
MINI_GOLDEN_MANIFEST="$MINI_ROOT/manifests/stage5_golden_monitor_manifest.json"
MINI_FAULT_MONITOR_ROOT="$MINI_ROOT/monitors/faults"
MINI_FAULT_MANIFEST_ROOT="$MINI_ROOT/manifests/faults"
REPORT_ROOT="$MINI_ROOT/reports"
PROVENANCE_ROOT="$MINI_ROOT/provenance"
LOCK_ROOT="$F2A_ROOT/runs/stage5_locks"
LOCK_ARCHIVE_ROOT="$LOCK_ROOT/archive"
PRECOMPILE_LOCK="$LOCK_ROOT/mini_gate2_precompile_lock.json"
EXECUTION_INPUT_LOCK="$LOCK_ROOT/mini_gate2_execution_inputs.json"
SMOKE_SELECTION="$PROVENANCE_ROOT/smoke_fault_selection.json"

STAGE5_TOOL="$FC/stage5_faults.py"
VERSION_GUARD="$FC/stage5_version_guard.py"
LOCK_VERIFY="$FC/stage5_lock_verify.py"
VERDICT="$FC/stage5_verdict.py"
BUNDLE="$FC/stage5_reproduction_bundle.py"
RUNNER_SELFTEST="$FC/stage5_runner_selftest.py"
RUNNER_INTEGRATION_SELFTEST="$FC/stage5_runner_integration_selftest.py"
SMOKE_SELECTOR="$FC/stage5_select_smoke_fault.py"
VALIDATION_COMMON="$FC/stage5_gate_validation_common.py"
GATE2_VALIDATE="$FC/stage5_gate2_validate.py"
GATE3_VALIDATE="$FC/stage5_gate3_validate.py"
GATE4_VALIDATE="$FC/stage5_gate4_validate.py"
GATE_VALIDATORS_SELFTEST="$FC/stage5_gate_validators_selftest.py"
EXECUTION_INPUT_TOOL="$FC/stage5_execution_input_lock.py"
EXECUTION_INPUT_SELFTEST="$FC/stage5_execution_input_lock_selftest.py"
ARTIFACT_LOCK_TOOL="$FC/stage5_artifact_lock.py"
ARTIFACT_LOCK_SELFTEST="$FC/stage5_artifact_lock_selftest.py"
COMMON_RUNNER="$F2A_ROOT/scripts/lib/xrun_stage5_common.sh"
GOLDEN_WRAPPER="$F2A_ROOT/scripts/run_xrun_stage5_golden.sh"
FAULT_WRAPPER="$F2A_ROOT/scripts/run_xrun_stage5_fault.sh"
GATE2_DRIVER="$FC/run_stage5_gate2.sh"
GATE3_DRIVER="$FC/run_stage5_gate3.sh"
GATE4_DRIVER="$FC/run_stage5_gate4.sh"
ORACLE_DRIVER="$FC/run_stage5_oracle_freeze.sh"
NATIVE_POLICY="$F2A_ROOT/platform/cv32e40p/stage5_native_execution_policy_v1.json"

log() {
    printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

require_file() {
    [[ -s "$1" ]] || fail "missing or empty file: $1"
}

cd "$F2A_ROOT"
mkdir -p "$REPORT_ROOT" "$PROVENANCE_ROOT" "$LOCK_ROOT" "$LOCK_ARCHIVE_ROOT"

log "Verify canonical Gate-1 mini artifacts still exist"
for file in \
    "$STAGE5_TOOL" \
    "$VERSION_GUARD" \
    "$LOCK_VERIFY" \
    "$MINI_CAMPAIGN" \
    "$MINI_GOLDEN_MONITOR" \
    "$MINI_GOLDEN_MANIFEST" \
    "$VERDICT" \
    "$BUNDLE" \
    "$RUNNER_SELFTEST" \
    "$RUNNER_INTEGRATION_SELFTEST" \
    "$SMOKE_SELECTOR" \
    "$VALIDATION_COMMON" \
    "$GATE2_VALIDATE" \
    "$GATE3_VALIDATE" \
    "$GATE4_VALIDATE" \
    "$GATE_VALIDATORS_SELFTEST" \
    "$EXECUTION_INPUT_TOOL" \
    "$EXECUTION_INPUT_SELFTEST" \
    "$ARTIFACT_LOCK_TOOL" \
    "$ARTIFACT_LOCK_SELFTEST" \
    "$COMMON_RUNNER" \
    "$GOLDEN_WRAPPER" \
    "$FAULT_WRAPPER" \
    "$GATE2_DRIVER" \
    "$GATE3_DRIVER" \
    "$GATE4_DRIVER" \
    "$ORACLE_DRIVER" \
    "$NATIVE_POLICY"
do
    require_file "$file"
done

[[ -s "$MINI_ROOT/reports/gate1_static_validation.json" ]] \
    || fail "Gate-1 validation report is missing"
python3 - "$MINI_ROOT/reports/gate1_static_validation.json" <<'PY'
import json
import sys
from pathlib import Path
payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("status") != "PASS":
    raise SystemExit("ERROR: Gate-1 report is not PASS")
PY

log "Syntax-check all hardened runner and gate tools"
for shell_tool in \
    "$COMMON_RUNNER" \
    "$GOLDEN_WRAPPER" \
    "$FAULT_WRAPPER" \
    "$GATE2_DRIVER" \
    "$GATE3_DRIVER" \
    "$GATE4_DRIVER" \
    "$ORACLE_DRIVER"
do
    bash -n "$shell_tool"
done
python3 -m py_compile \
    "$VERDICT" \
    "$BUNDLE" \
    "$RUNNER_SELFTEST" \
    "$RUNNER_INTEGRATION_SELFTEST" \
    "$SMOKE_SELECTOR" \
    "$VALIDATION_COMMON" \
    "$GATE2_VALIDATE" \
    "$GATE3_VALIDATE" \
    "$GATE4_VALIDATE" \
    "$GATE_VALIDATORS_SELFTEST" \
    "$EXECUTION_INPUT_TOOL" \
    "$EXECUTION_INPUT_SELFTEST" \
    "$ARTIFACT_LOCK_TOOL" \
    "$ARTIFACT_LOCK_SELFTEST"

log "Run synthetic strict-verdict and reproduction-bundle tests"
python3 "$RUNNER_SELFTEST" \
    --verdict "$VERDICT" \
    --bundle "$BUNDLE"
python3 "$RUNNER_INTEGRATION_SELFTEST" \
    --common "$COMMON_RUNNER" \
    --verdict "$VERDICT" \
    --bundle "$BUNDLE"
python3 "$GATE_VALIDATORS_SELFTEST" \
    --common "$VALIDATION_COMMON" \
    --gate2 "$GATE2_VALIDATE" \
    --gate3 "$GATE3_VALIDATE" \
    --gate4 "$GATE4_VALIDATE"
python3 "$EXECUTION_INPUT_SELFTEST" \
    --tool "$EXECUTION_INPUT_TOOL"
python3 "$ARTIFACT_LOCK_SELFTEST" \
    --tool "$ARTIFACT_LOCK_TOOL"

log "Select one deterministic control-oriented mini smoke fault"
python3 "$SMOKE_SELECTOR" \
    --campaign "$MINI_CAMPAIGN" \
    --output "$SMOKE_SELECTION" \
    --force

log "Freeze ignored/external execution inputs before any Xcelium gate"
# shellcheck disable=SC1091
source "$F2A_ROOT/scripts/setup_env.sh"
CV_HOME="${CV32E40P_HOME:-/raid/spring2026/fwu44/research/cv32e40p}"
CELL_MODEL="${CV32E40P_CELL_MODEL:-}"
[[ -s "$CELL_MODEL" ]] || fail "CV32E40P_CELL_MODEL is missing after setup_env.sh: $CELL_MODEL"
readarray -t EXEC_META < <(
python3 - "$MINI_CAMPAIGN" "$SMOKE_SELECTION" <<'PY_EXEC'
import json
import sys
from pathlib import Path
campaign = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
selection = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
print(Path(campaign["mapped_netlist"]["path"]).resolve())
print(selection["fault_id"])
PY_EXEC
)
[[ "${#EXEC_META[@]}" -eq 2 ]] || fail "failed to resolve execution-lock metadata"
MAPPED_NETLIST="${EXEC_META[0]}"
SMOKE_FAULT_ID="${EXEC_META[1]}"
SMOKE_FAULT_MONITOR="$MINI_FAULT_MONITOR_ROOT/${SMOKE_FAULT_ID}.sv"
python3 "$EXECUTION_INPUT_TOOL" create \
    --repo-root "$F2A_ROOT" \
    --cv32e40p-home "$CV_HOME" \
    --cell-model "$CELL_MODEL" \
    --mapped-netlist "$MAPPED_NETLIST" \
    --monitor "$MINI_GOLDEN_MONITOR" \
    --monitor "$SMOKE_FAULT_MONITOR" \
    --output "$EXECUTION_INPUT_LOCK" \
    --force
python3 "$EXECUTION_INPUT_TOOL" verify \
    --lock "$EXECUTION_INPUT_LOCK"
HEAD_SHORT="$(git rev-parse --short=12 HEAD)"
STAMP="$(date +%Y%m%d_%H%M%S)"
cp "$EXECUTION_INPUT_LOCK" \
    "$LOCK_ARCHIVE_ROOT/mini_gate2_execution_inputs_${HEAD_SHORT}_${STAMP}.json"

log "Write direct runner-hash audit"
python3 - \
    "$REPORT_ROOT/runner_hardening_audit.json" \
    "$COMMON_RUNNER" \
    "$GOLDEN_WRAPPER" \
    "$FAULT_WRAPPER" \
    "$VERDICT" \
    "$BUNDLE" \
    "$RUNNER_SELFTEST" \
    "$RUNNER_INTEGRATION_SELFTEST" \
    "$GATE_VALIDATORS_SELFTEST" \
    "$EXECUTION_INPUT_TOOL" \
    "$EXECUTION_INPUT_SELFTEST" \
    "$ARTIFACT_LOCK_TOOL" \
    "$ARTIFACT_LOCK_SELFTEST" \
    "$NATIVE_POLICY" <<'PY'
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

output = Path(sys.argv[1])
files = [Path(item).resolve() for item in sys.argv[2:]]
payload = {
    "schema_version": "1.0",
    "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    "kind": "stage5_runner_hardening_audit",
    "status": "PASS",
    "contracts": {
        "strict_pass_requires_exact_signature_and_exit_success": True,
        "exit_success_alone_is_success": False,
        "compile_gate_uses_compile_plus_elaboration": True,
        "infrastructure_failures_retain_work": True,
        "timeout_retains_work": True,
        "output_mismatch_builds_compact_bundle": True,
        "reproduction_bundle_excludes_fault_netlist_vcd_and_xcelium_work": True,
        "fault_wrapper_has_no_unconditional_cleanup_trap": True,
        "native_execution_separates_tool_completion_workload_and_detectors": True,
        "xmsim_asrtst_is_existing_detector_evidence_not_infrastructure_error": True,
        "assertion_terminated_workload_is_not_reached": True,
        "assertion_terminated_architectural_outcome_is_censored": True,
        "existing_assertion_trigger_is_not_final_fault_effect_oracle": True,
        "ai_assertions_do_not_participate_in_native_raw_fact_generation": True
    },
    "files": [
        {"path": str(path), "sha256": sha(path), "size_bytes": path.stat().st_size}
        for path in files
    ]
}
output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY

log "Audit the mini campaign under the new local code state and write precompile lock"
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
    --report "$PROVENANCE_ROOT/gate2_precompile_version_audit.json" \
    --write-lock "$PRECOMPILE_LOCK"

HEAD_SHORT="$(git rev-parse --short=12 HEAD)"
STAMP="$(date +%Y%m%d_%H%M%S)"
cp "$PRECOMPILE_LOCK" \
    "$LOCK_ARCHIVE_ROOT/mini_gate2_precompile_${HEAD_SHORT}_${STAMP}.json"

log "Immediately verify the new precompile lock"
python3 "$LOCK_VERIFY" \
    --repo-root "$F2A_ROOT" \
    --lock "$PRECOMPILE_LOCK"

log "Runner hardening completed"
echo "Runner audit       : $REPORT_ROOT/runner_hardening_audit.json"
echo "Smoke fault        : $SMOKE_SELECTION"
echo "Precompile lock    : $PRECOMPILE_LOCK"
echo "Execution lock     : $EXECUTION_INPUT_LOCK"
echo "No simulation was executed."
