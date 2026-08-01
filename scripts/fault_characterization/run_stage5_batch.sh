#!/usr/bin/env bash
set -euo pipefail

# One public Stage-5 batch interface.
#
# ACTION values:
#   freeze          Freeze Stage-4 inputs and recover canonical reference ID.
#   rebuild         Rebuild the full canonical Stage-5 campaign with v1.0.8.
#   prepare         Build one N-site plan and prepare its batch directory.
#   reference       Run only the canonical reference fault in this batch.
#   verify-reference Independently compare the reference run with G5 checkpoint.
#   run             Run/resume the complete batch; requires reference PASS.
#   status          Print/write batch status.
#   storage         Print/write batch storage report.
#   reference-flow  prepare -> reference -> verify-reference.
#   all             prepare -> reference -> verify-reference -> run.

F2A_ROOT="${F2A_ROOT:-/raid/spring2026/fwu44/research/fault2assertion}"
ACTION="${ACTION:-prepare}"
SITE_COUNT="${SITE_COUNT:-4}"
MAXCYCLES="${MAXCYCLES:-2000000}"
STORAGE_INTERVAL="${STORAGE_INTERVAL:-5}"

CONTROL="$F2A_ROOT/scripts/fault_characterization/stage5_batch_control.py"
ENGINE="$F2A_ROOT/scripts/fault_characterization/stage5_batch.py"
ORACLE_TOOL="$F2A_ROOT/scripts/fault_characterization/stage5_batch_oracle.py"
ORACLE_VALIDATOR="$F2A_ROOT/scripts/fault_characterization/stage5_batch_oracle_validate.py"
CHECKPOINT="$F2A_ROOT/runs/stage5_dev/phase2_v1/g5_oracle/reports/TF000002_SA0_validation.json"
FREEZE_ROOT="$F2A_ROOT/faults/cv32e40p/site_catalog/frozen_stage4_batch_v1"
PLAN_ROOT="$F2A_ROOT/runs/stage5_plans/cv32e40p/crc32/sites_${SITE_COUNT}"
BATCH_ROOT="$F2A_ROOT/runs/stage5_campaign_v2/cv32e40p/crc32/sites_${SITE_COUNT}"
PLAN_JSON="$PLAN_ROOT/batch_plan.json"
REFERENCE_JSON="$FREEZE_ROOT/reference.json"
PREPARED_VALIDATION_JSON="$BATCH_ROOT/prepared_validation.json"
QUALIFICATION_JSON="$BATCH_ROOT/reference_qualification.json"

log() {
    printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    return 1
}

require_file() {
    local path="$1"
    local label="$2"
    [[ -s "$path" ]] || fail "$label not found or empty: $path"
}

require_positive_integer() {
    local value="$1"
    local label="$2"
    [[ "$value" =~ ^[1-9][0-9]*$ ]] || fail "$label must be a positive integer: $value"
}

json_value() {
    local path="$1"
    local key="$2"
    python3 - "$path" "$key" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
value = payload
for part in sys.argv[2].split("."):
    if not isinstance(value, dict) or part not in value:
        raise SystemExit(f"missing JSON key: {sys.argv[2]}")
    value = value[part]
print(value)
PY
}

static_validate() {
    local stage5_tool
    local stage5_impl
    local mode_tool
    local verdict_tool
    local fault_wrapper
    local common_runner

    stage5_tool="$F2A_ROOT/scripts/fault_characterization/stage5_faults.py"
    stage5_impl="$F2A_ROOT/scripts/fault_characterization/stage5_faults_v107_impl.py"
    mode_tool="$F2A_ROOT/scripts/fault_characterization/stage5_phase2_modes.py"
    verdict_tool="$F2A_ROOT/scripts/fault_characterization/stage5_verdict.py"
    fault_wrapper="$F2A_ROOT/scripts/run_xrun_stage5_fault.sh"
    common_runner="$F2A_ROOT/scripts/lib/xrun_stage5_common.sh"

    require_file "$CONTROL" "batch control tool"
    require_file "$ENGINE" "batch execution engine"
    require_file "$ORACLE_TOOL" "batch oracle builder"
    require_file "$ORACLE_VALIDATOR" "batch oracle validator"
    require_file "$stage5_tool" "active Stage-5 tool"
    require_file "$stage5_impl" "preserved Stage-5 implementation"
    require_file "$mode_tool" "Stage-5 mode composer"
    require_file "$verdict_tool" "Stage-5 verdict engine"
    require_file "$fault_wrapper" "Stage-5 Xcelium wrapper"
    require_file "$common_runner" "Stage-5 common runner"

    python3 -m py_compile \
        "$CONTROL" \
        "$ENGINE" \
        "$ORACLE_TOOL" \
        "$ORACLE_VALIDATOR" \
        "$stage5_tool" \
        "$stage5_impl" \
        "$mode_tool" \
        "$verdict_tool"

    bash -n "$fault_wrapper"
    bash -n "$common_runner"

    python3 "$CONTROL" --version >/dev/null
    python3 "$stage5_tool" --version | grep -F "1.0.8" >/dev/null
}

prepare_plan() {
    require_file "$REFERENCE_JSON" "frozen canonical reference"
    python3 "$CONTROL" \
        --root "$F2A_ROOT" \
        plan \
        --site-count "$SITE_COUNT" \
        --plan-root "$PLAN_ROOT"
}

prepare_batch() {
    prepare_plan
    require_file "$PLAN_JSON" "batch plan"
    require_file "$CHECKPOINT" "G5 checkpoint validation"

    local fault_count
    local reference_fault
    local selected_dir
    fault_count="$(json_value "$PLAN_JSON" selected_fault_instance_count)"
    reference_fault="$(json_value "$PLAN_JSON" reference_fault_id)"
    selected_dir="$(json_value "$PLAN_JSON" selected_fault_specs_dir)"

    # selected_dir contains exactly every fault instance belonging to the N
    # planned sites.  Therefore count equals the directory population and the
    # engine's legacy seed cannot change which scientific faults are selected.
    python3 "$ENGINE" \
        --root "$F2A_ROOT" \
        --checkpoint "$CHECKPOINT" \
        --pilot-root "$BATCH_ROOT" \
        prepare \
        --selected-dir "$selected_dir" \
        --count "$fault_count" \
        --seed 0 \
        --include-fault "$reference_fault"

    python3 "$CONTROL" \
        --root "$F2A_ROOT" \
        verify-prepared \
        --plan "$PLAN_JSON" \
        --batch-root "$BATCH_ROOT"
}

run_reference() {
    require_file "$PLAN_JSON" "batch plan"
    require_file "$PREPARED_VALIDATION_JSON" "prepared batch validation"
    require_file "$CHECKPOINT" "G5 checkpoint validation"

    local prepared_status
    local reference_fault
    prepared_status="$(json_value "$PREPARED_VALIDATION_JSON" status)"
    [[ "$prepared_status" == "PASS" ]] || \
        fail "prepared batch validation is not PASS: $prepared_status"

    reference_fault="$(json_value "$PLAN_JSON" reference_fault_id)"
    python3 "$ENGINE" \
        --root "$F2A_ROOT" \
        --checkpoint "$CHECKPOINT" \
        --pilot-root "$BATCH_ROOT" \
        run-one \
        --fault-id "$reference_fault" \
        --maxcycles "$MAXCYCLES"
}

verify_reference() {
    python3 "$CONTROL" \
        --root "$F2A_ROOT" \
        verify-reference \
        --batch-root "$BATCH_ROOT"
}

run_batch() {
    require_file "$PREPARED_VALIDATION_JSON" "prepared batch validation"
    require_file "$QUALIFICATION_JSON" "canonical reference qualification"

    local prepared_status
    local qualification_status
    prepared_status="$(json_value "$PREPARED_VALIDATION_JSON" status)"
    qualification_status="$(json_value "$QUALIFICATION_JSON" status)"

    [[ "$prepared_status" == "PASS" ]] || \
        fail "prepared batch validation is not PASS: $prepared_status"
    [[ "$qualification_status" == "PASS" ]] || \
        fail "reference qualification is not PASS: $qualification_status"

    python3 "$ENGINE" \
        --root "$F2A_ROOT" \
        --checkpoint "$CHECKPOINT" \
        --pilot-root "$BATCH_ROOT" \
        run-pilot \
        --maxcycles "$MAXCYCLES" \
        --storage-interval "$STORAGE_INTERVAL"
}

main() {
    require_positive_integer "$SITE_COUNT" "SITE_COUNT"
    require_positive_integer "$MAXCYCLES" "MAXCYCLES"
    cd "$F2A_ROOT"

    case "$ACTION" in
        freeze)
            static_validate
            log "Freeze deterministic Stage-4 inputs and recover canonical reference"
            python3 "$CONTROL" --root "$F2A_ROOT" freeze-reference
            ;;
        rebuild)
            static_validate
            log "Rebuild the complete canonical Stage-5 campaign with v1.0.8"
            python3 "$CONTROL" --root "$F2A_ROOT" rebuild-campaign
            ;;
        prepare)
            static_validate
            log "Prepare deterministic ${SITE_COUNT}-site batch"
            prepare_batch
            ;;
        reference)
            static_validate
            log "Run canonical reference fault inside the ${SITE_COUNT}-site batch"
            run_reference
            ;;
        verify-reference)
            static_validate
            log "Qualify canonical reference against the frozen G5 checkpoint"
            verify_reference
            ;;
        run)
            static_validate
            log "Run or resume complete ${SITE_COUNT}-site batch"
            run_batch
            ;;
        status)
            static_validate
            python3 "$ENGINE" \
                --root "$F2A_ROOT" \
                --checkpoint "$CHECKPOINT" \
                --pilot-root "$BATCH_ROOT" \
                status
            ;;
        storage)
            static_validate
            python3 "$ENGINE" \
                --root "$F2A_ROOT" \
                --checkpoint "$CHECKPOINT" \
                --pilot-root "$BATCH_ROOT" \
                storage-report
            ;;
        reference-flow)
            log "Reference flow: prepare"
            ACTION=prepare SITE_COUNT="$SITE_COUNT" MAXCYCLES="$MAXCYCLES" \
                STORAGE_INTERVAL="$STORAGE_INTERVAL" F2A_ROOT="$F2A_ROOT" bash "$0"
            log "Reference flow: execute canonical reference"
            ACTION=reference SITE_COUNT="$SITE_COUNT" MAXCYCLES="$MAXCYCLES" \
                STORAGE_INTERVAL="$STORAGE_INTERVAL" F2A_ROOT="$F2A_ROOT" bash "$0"
            log "Reference flow: independent qualification"
            ACTION=verify-reference SITE_COUNT="$SITE_COUNT" MAXCYCLES="$MAXCYCLES" \
                STORAGE_INTERVAL="$STORAGE_INTERVAL" F2A_ROOT="$F2A_ROOT" bash "$0"
            ;;
        all)
            ACTION=reference-flow SITE_COUNT="$SITE_COUNT" MAXCYCLES="$MAXCYCLES" \
                STORAGE_INTERVAL="$STORAGE_INTERVAL" F2A_ROOT="$F2A_ROOT" bash "$0"
            ACTION=run SITE_COUNT="$SITE_COUNT" MAXCYCLES="$MAXCYCLES" \
                STORAGE_INTERVAL="$STORAGE_INTERVAL" F2A_ROOT="$F2A_ROOT" bash "$0"
            ;;
        *)
            fail "unsupported ACTION=$ACTION; allowed: freeze, rebuild, prepare, reference, verify-reference, run, status, storage, reference-flow, all"
            ;;
    esac

    printf '\nSite count : %s\n' "$SITE_COUNT"
    printf 'Plan root  : %s\n' "$PLAN_ROOT"
    printf 'Batch root : %s\n' "$BATCH_ROOT"
}

main "$@"
