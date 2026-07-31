#!/usr/bin/env bash
set -euo pipefail

F2A_ROOT="${F2A_ROOT:-/raid/spring2026/fwu44/research/fault2assertion}"
FC="$F2A_ROOT/scripts/fault_characterization"
MINI_ROOT="$F2A_ROOT/runs/stage5_dev/mini_smoke_v1"
MINI_CAMPAIGN="$MINI_ROOT/campaign/stage_05_campaign.json"
GOLDEN_SITE_TRACE_ROOT="$MINI_ROOT/gate3_golden/site_traces"
SMOKE_SELECTION="$MINI_ROOT/provenance/smoke_fault_selection.json"
GATE4_REPORT="$MINI_ROOT/reports/gate4_single_fault_validation.json"
GATE4_ROOT="$MINI_ROOT/gate4_fault"
ORACLE_ROOT="$MINI_ROOT/oracle_v2"
ORACLE_DIR="$ORACLE_ROOT/oracles"
ORACLE_REPORT_DIR="$ORACLE_ROOT/reports"
SVA_DIR="$ORACLE_ROOT/sva_seeds"
VALIDATION_DIR="$ORACLE_ROOT/validation"
FREEZE_REPORT="$MINI_ROOT/reports/oracle_semantics_v2_freeze.json"

PRECOMPILE_LOCK="$F2A_ROOT/runs/stage5_locks/mini_gate2_precompile_lock.json"
EXECUTION_INPUT_LOCK="$F2A_ROOT/runs/stage5_locks/mini_gate2_execution_inputs.json"
ORACLE_CODE_LOCK="$F2A_ROOT/runs/stage5_locks/mini_oracle_v2_code_lock.json"
ORACLE_LOCK="$F2A_ROOT/runs/stage5_locks/mini_oracle_v2_lock.json"
LOCK_ARCHIVE_ROOT="$F2A_ROOT/runs/stage5_locks/archive"

SEMANTICS="$FC/stage5_oracle_semantics.py"
POLICY="$F2A_ROOT/platform/cv32e40p/stage5_oracle_semantics_v2.json"
SEMANTICS_SELFTEST="$FC/stage5_oracle_semantics_selftest.py"
ANALYZER="$FC/stage5_oracle_v2.py"
VALIDATOR="$FC/stage5_oracle_validate.py"
E2E_SELFTEST="$FC/stage5_oracle_end_to_end_selftest.py"
ARTIFACT_LOCK_TOOL="$FC/stage5_artifact_lock.py"
ARTIFACT_LOCK_SELFTEST="$FC/stage5_artifact_lock_selftest.py"
LOCK_VERIFY="$FC/stage5_lock_verify.py"
EXECUTION_INPUT_TOOL="$FC/stage5_execution_input_lock.py"
VERSION_GUARD="$FC/stage5_version_guard.py"
STAGE5_TOOL="$FC/stage5_faults.py"

GOLDEN_MONITOR="$MINI_ROOT/monitors/stage5_golden_monitor.sv"
GOLDEN_MANIFEST="$MINI_ROOT/manifests/stage5_golden_monitor_manifest.json"
FAULT_MONITOR_ROOT="$MINI_ROOT/monitors/faults"
FAULT_MANIFEST_ROOT="$MINI_ROOT/manifests/faults"
GATE2_REPORT="$MINI_ROOT/reports/gate2_compile_validation.json"
GATE3_REPORT="$MINI_ROOT/reports/gate3_golden_validation.json"
GOLDEN_SPLIT_MANIFEST="$MINI_ROOT/gate3_golden/golden_split_manifest.json"

log() {
    printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

create_fresh_oracle_root() {
    case "$ORACLE_ROOT" in
        "$F2A_ROOT"/runs/stage5_dev/mini_smoke_v1/oracle_v2) ;;
        *) fail "unsafe oracle root: $ORACLE_ROOT" ;;
    esac
    if [[ -e "$ORACLE_ROOT" ]]; then
        fail "Oracle-v2 workspace already exists. Preserve or archive it before an intentional rerun: $ORACLE_ROOT"
    fi
    if [[ -e "$FREEZE_REPORT" || -e "$ORACLE_CODE_LOCK" || -e "$ORACLE_LOCK" ]]; then
        fail "Oracle-v2 freeze outputs already exist. Preserve or archive them before an intentional rerun."
    fi
    mkdir -p "$ORACLE_DIR" "$ORACLE_REPORT_DIR" "$SVA_DIR" "$VALIDATION_DIR"
}

cd "$F2A_ROOT"

log "Verify Gate 4 passed and the local precompile lock still matches"
python3 - "$GATE4_REPORT" <<'PY'
import json
import sys
from pathlib import Path
path = Path(sys.argv[1])
if not path.is_file():
    raise SystemExit(f"ERROR: Gate-4 report not found: {path}")
payload = json.loads(path.read_text(encoding="utf-8"))
if payload.get("status") != "PASS":
    raise SystemExit("ERROR: Gate-4 report is not PASS")
PY
python3 "$LOCK_VERIFY" --repo-root "$F2A_ROOT" --lock "$PRECOMPILE_LOCK"
python3 "$EXECUTION_INPUT_TOOL" verify --lock "$EXECUTION_INPUT_LOCK"

for file in \
    "$SEMANTICS" "$POLICY" "$SEMANTICS_SELFTEST" "$ANALYZER" \
    "$VALIDATOR" "$E2E_SELFTEST" "$ARTIFACT_LOCK_TOOL" \
    "$ARTIFACT_LOCK_SELFTEST" "$SMOKE_SELECTION" "$MINI_CAMPAIGN" \
    "$GATE2_REPORT" "$GATE3_REPORT" "$GOLDEN_SPLIT_MANIFEST"
do
    [[ -s "$file" ]] || fail "missing oracle input/tool: $file"
done

log "Compile and unit-test the frozen semantic policy before touching real data"
python3 -m py_compile \
    "$SEMANTICS" \
    "$SEMANTICS_SELFTEST" \
    "$ANALYZER" \
    "$VALIDATOR" \
    "$E2E_SELFTEST" \
    "$ARTIFACT_LOCK_TOOL" \
    "$ARTIFACT_LOCK_SELFTEST"
python3 "$SEMANTICS_SELFTEST" \
    --semantics "$SEMANTICS" \
    --policy "$POLICY"
python3 "$E2E_SELFTEST" \
    --analyzer "$ANALYZER" \
    --validator "$VALIDATOR" \
    --semantics "$SEMANTICS" \
    --policy "$POLICY"
python3 "$ARTIFACT_LOCK_SELFTEST" \
    --tool "$ARTIFACT_LOCK_TOOL"

readarray -t META < <(
python3 - "$SMOKE_SELECTION" <<'PY'
import json
import sys
from pathlib import Path
selection = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(selection["fault_id"])
print(selection["selection_id"])
print(Path(selection["fault_spec"]).resolve())
PY
)
[[ "${#META[@]}" -eq 3 ]] || fail "failed to resolve oracle metadata"
FAULT_ID="${META[0]}"
SELECTION_ID="${META[1]}"
FAULT_JSON="${META[2]}"
FAULT_RUN="$GATE4_ROOT/$FAULT_ID"
FAULT_TRACE="$MINI_ROOT/traces/${FAULT_ID}.trace.tsv"
GOLDEN_TRACE="$GOLDEN_SITE_TRACE_ROOT/${SELECTION_ID}.trace.tsv.gz"
ORACLE_JSON="$ORACLE_DIR/${FAULT_ID}.json"
ORACLE_REPORT="$ORACLE_REPORT_DIR/${FAULT_ID}.txt"
SVA_SEED="$SVA_DIR/${FAULT_ID}.sva"
VALIDATION_REPORT="$VALIDATION_DIR/${FAULT_ID}.json"

for file in \
    "$FAULT_JSON" "$FAULT_TRACE" "$GOLDEN_TRACE" \
    "$FAULT_RUN/result.json" "$FAULT_RUN/xrun.log"
do
    [[ -s "$file" ]] || fail "missing real oracle input: $file"
done

log "Create a fresh derived v2 oracle workspace without deleting prior evidence"
create_fresh_oracle_root

log "Generate one real oracle with raw facts and semantic label stored separately"
python3 "$ANALYZER" \
    --fault-json "$FAULT_JSON" \
    --golden-trace "$GOLDEN_TRACE" \
    --fault-trace "$FAULT_TRACE" \
    --result-json "$FAULT_RUN/result.json" \
    --xrun-log "$FAULT_RUN/xrun.log" \
    --semantics "$SEMANTICS" \
    --policy "$POLICY" \
    --oracle-output "$ORACLE_JSON" \
    --report-output "$ORACLE_REPORT" \
    --sva-output "$SVA_SEED"

log "Recompute classification and every durable hash independently"
python3 "$VALIDATOR" \
    --oracle "$ORACLE_JSON" \
    --fault-json "$FAULT_JSON" \
    --analyzer "$ANALYZER" \
    --semantics "$SEMANTICS" \
    --policy "$POLICY" \
    --report "$VALIDATION_REPORT"

log "Require one scientifically valid real mini-fault characterization"
python3 - "$ORACLE_JSON" <<'PY_SCIENCE'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
oracle = json.loads(path.read_text(encoding="utf-8"))
raw = oracle["raw_facts"]
semantic = oracle["semantic_classification"]["primary_class"]
allowed = {
    "DETECTED_HANG",
    "DETECTED_OUTPUT_CORRUPTION",
    "LOCAL_PROPAGATION_MASKED_AT_OUTPUT",
    "SITE_DIVERGENCE_LOCALLY_MASKED",
    "FUNCTIONALLY_EQUIVALENT_UNDER_WORKLOAD",
}
checks = {
    "runner_valid": raw["runner"]["valid_fault_run"] is True,
    "golden_trace_valid": raw["trace_validity"]["golden_valid"] is True,
    "fault_trace_valid": raw["trace_validity"]["fault_valid"] is True,
    "common_scope": int(raw["scope_alignment"]["common_scope_count"]) > 0,
    "activated": raw["activation"]["activated"] is True,
    "injection_effective": raw["injection"]["effective"] is True,
    "scientific_class": semantic in allowed,
}
failed = [name for name, passed in checks.items() if not passed]
if failed:
    raise SystemExit(
        "ERROR: real mini-fault oracle is not scientifically valid for freeze; "
        f"class={semantic}, failed_checks={failed}"
    )
print(f"Real mini-fault semantic class         : {semantic}")
print("Real mini-fault scientific validity    : PASS")
PY_SCIENCE

log "Write the semantic freeze report with direct implementation and policy hashes"
python3 - \
    "$FREEZE_REPORT" \
    "$POLICY" \
    "$SEMANTICS" \
    "$ANALYZER" \
    "$VALIDATOR" \
    "$SEMANTICS_SELFTEST" \
    "$E2E_SELFTEST" \
    "$ORACLE_JSON" \
    "$VALIDATION_REPORT" <<'PY'
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
paths = [Path(item).resolve() for item in sys.argv[2:]]
oracle = json.loads(paths[-2].read_text(encoding="utf-8"))
validation = json.loads(paths[-1].read_text(encoding="utf-8"))
if validation.get("status") != "PASS":
    raise SystemExit("ERROR: refusing to freeze an invalid oracle")
payload = {
    "schema_version": "1.0",
    "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    "kind": "stage5_oracle_semantics_v2_freeze",
    "status": "PASS",
    "semantics_version": oracle["semantic_classification"]["semantics_version"],
    "semantic_classification": oracle["semantic_classification"],
    "raw_facts_digest_sha256": hashlib.sha256(
        json.dumps(
            oracle["raw_facts"],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest(),
    "files": [
        {"path": str(path), "sha256": sha(path), "size_bytes": path.stat().st_size}
        for path in paths
    ],
    "contracts": {
        "raw_facts_separate_from_semantic_classification": True,
        "classification_priority_table_tested": True,
        "end_to_end_trace_parser_tested": True,
        "real_mini_fault_oracle_validated": True,
        "architectural_masking_not_claimed_from_direct_receivers": True
    }
}
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY

log "Delete temporary raw fault trace and retained work only after oracle validation"
case "$FAULT_TRACE" in
    "$MINI_ROOT"/traces/TF??????_SA?.trace.tsv)
        rm -f "$FAULT_TRACE"
        ;;
    *)
        fail "unsafe fault trace cleanup path: $FAULT_TRACE"
        ;;
esac
if [[ -d "$FAULT_RUN/work" ]]; then
    # TIMEOUT is the only valid Gate-4 result that retains work.  Gate-4 already
    # required its compact reproduction bundle before this cleanup.
    [[ -s "$FAULT_RUN/reproduction_bundle.tar.gz" ]] \
        || fail "retained work exists without a reproduction bundle"
    rm -rf "$FAULT_RUN/work"
fi
[[ ! -e "$FAULT_TRACE" ]] || fail "raw fault trace cleanup failed"
[[ ! -d "$FAULT_RUN/work" ]] || fail "run-local work cleanup failed"

log "Write and verify the oracle-v2 code-state lock"
monitor_args=(--monitor "$GOLDEN_MONITOR")
manifest_args=(--manifest "$GOLDEN_MANIFEST")
for monitor in "$FAULT_MONITOR_ROOT"/*.sv; do
    monitor_args+=(--monitor "$monitor")
done
for manifest in "$FAULT_MANIFEST_ROOT"/*.json; do
    manifest_args+=(--manifest "$manifest")
done

python3 "$VERSION_GUARD" \
    --repo-root "$F2A_ROOT" \
    --tool "$STAGE5_TOOL" \
    --campaign "$MINI_CAMPAIGN" \
    "${monitor_args[@]}" \
    "${manifest_args[@]}" \
    --report "$MINI_ROOT/provenance/oracle_v2_version_audit.json" \
    --write-lock "$ORACLE_CODE_LOCK"
python3 "$LOCK_VERIFY" --repo-root "$F2A_ROOT" --lock "$ORACLE_CODE_LOCK"

log "Lock the durable oracle, validation, semantics, and execution evidence"
artifact_args=(
    --file "oracle_code_lock=$ORACLE_CODE_LOCK"
    --file "precompile_lock=$PRECOMPILE_LOCK"
    --file "execution_input_lock=$EXECUTION_INPUT_LOCK"
    --file "mini_campaign=$MINI_CAMPAIGN"
    --file "smoke_selection=$SMOKE_SELECTION"
    --file "fault_spec=$FAULT_JSON"
    --file "gate2_report=$GATE2_REPORT"
    --file "gate3_report=$GATE3_REPORT"
    --file "golden_split_manifest=$GOLDEN_SPLIT_MANIFEST"
    --file "gate4_report=$GATE4_REPORT"
    --file "fault_run_result=$FAULT_RUN/result.json"
    --file "fault_run_log=$FAULT_RUN/xrun.log"
    --file "fault_run_retention=$FAULT_RUN/retention.json"
    --file "golden_site_trace=$GOLDEN_TRACE"
    --file "semantics_policy=$POLICY"
    --file "semantics_implementation=$SEMANTICS"
    --file "oracle_analyzer=$ANALYZER"
    --file "oracle_validator=$VALIDATOR"
    --file "semantics_selftest=$SEMANTICS_SELFTEST"
    --file "oracle_e2e_selftest=$E2E_SELFTEST"
    --file "artifact_lock_tool=$ARTIFACT_LOCK_TOOL"
    --file "artifact_lock_selftest=$ARTIFACT_LOCK_SELFTEST"
    --file "oracle_json=$ORACLE_JSON"
    --file "oracle_report=$ORACLE_REPORT"
    --file "sva_seed=$SVA_SEED"
    --file "oracle_validation=$VALIDATION_REPORT"
    --file "freeze_report=$FREEZE_REPORT"
)
if [[ -s "$FAULT_RUN/reproduction_bundle.tar.gz" ]]; then
    artifact_args+=(--file "fault_reproduction_bundle=$FAULT_RUN/reproduction_bundle.tar.gz")
fi
if [[ -s "$FAULT_RUN/reproduction_bundle_manifest.json" ]]; then
    artifact_args+=(--file "fault_reproduction_manifest=$FAULT_RUN/reproduction_bundle_manifest.json")
fi
python3 "$ARTIFACT_LOCK_TOOL" create \
    --kind stage5_oracle_semantics_v2_frozen_artifacts \
    "${artifact_args[@]}" \
    --output "$ORACLE_LOCK"
python3 "$ARTIFACT_LOCK_TOOL" verify --lock "$ORACLE_LOCK"

mkdir -p "$LOCK_ARCHIVE_ROOT"
HEAD_SHORT="$(git rev-parse --short=12 HEAD)"
STAMP="$(date +%Y%m%d_%H%M%S)"
cp "$ORACLE_CODE_LOCK" \
    "$LOCK_ARCHIVE_ROOT/mini_oracle_v2_code_${HEAD_SHORT}_${STAMP}.json"
cp "$ORACLE_LOCK" \
    "$LOCK_ARCHIVE_ROOT/mini_oracle_v2_artifacts_${HEAD_SHORT}_${STAMP}.json"

log "Oracle semantics v2 completed and frozen"
echo "Oracle JSON         : $ORACLE_JSON"
echo "Oracle report       : $ORACLE_REPORT"
echo "Oracle validation   : $VALIDATION_REPORT"
echo "Freeze report       : $FREEZE_REPORT"
echo "Oracle code lock    : $ORACLE_CODE_LOCK"
echo "Oracle artifact lock: $ORACLE_LOCK"
echo "Raw fault trace and run-local work were deleted after validation."
