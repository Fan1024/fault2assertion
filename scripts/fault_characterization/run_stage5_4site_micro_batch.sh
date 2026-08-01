#!/usr/bin/env bash
set -euo pipefail

# Complete, validate, and freeze the already reference-qualified Stage-5
# four-site micro batch. This wrapper does not prepare 20 sites and does not
# enforce any storage threshold.
#
# ACTION values:
#   run       Run/resume all eight fault instances in the four selected sites.
#   validate  Refresh status/storage observations and independently validate.
#   freeze    Validate and write the durable four-site checkpoint.
#   complete  run -> validate -> freeze.

F2A_ROOT="${F2A_ROOT:-/raid/spring2026/fwu44/research/fault2assertion}"
ACTION="${ACTION:-run}"
MAXCYCLES="${MAXCYCLES:-2000000}"
STORAGE_INTERVAL="${STORAGE_INTERVAL:-2}"

BATCH_INTERFACE="$F2A_ROOT/scripts/fault_characterization/run_stage5_batch.sh"
FREEZE_TOOL="$F2A_ROOT/scripts/fault_characterization/stage5_4site_freeze.py"
PLAN_ROOT="$F2A_ROOT/runs/stage5_plans/cv32e40p/crc32/sites_4"
BATCH_ROOT="$F2A_ROOT/runs/stage5_campaign_v2/cv32e40p/crc32/sites_4"
CHECKPOINT="$F2A_ROOT/docs/stage5/stage5_4site_micro_batch_checkpoint.json"

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
    [[ "$value" =~ ^[1-9][0-9]*$ ]] || \
        fail "$label must be a positive integer: $value"
}

static_validate() {
    require_file "$BATCH_INTERFACE" "Stage-5 batch interface"
    require_file "$FREEZE_TOOL" "four-site freeze tool"
    require_file "$PLAN_ROOT/batch_plan.json" "four-site plan"
    require_file "$BATCH_ROOT/pilot_manifest.json" "four-site pilot manifest"
    require_file \
        "$BATCH_ROOT/reference_qualification.json" \
        "canonical reference qualification"
    python3 -m py_compile "$FREEZE_TOOL"
    bash -n "$BATCH_INTERFACE"
}

refresh_reports() {
    F2A_ROOT="$F2A_ROOT" SITE_COUNT=4 ACTION=status \
        bash "$BATCH_INTERFACE"
    F2A_ROOT="$F2A_ROOT" SITE_COUNT=4 ACTION=storage \
        bash "$BATCH_INTERFACE"
}

run_batch() {
    F2A_ROOT="$F2A_ROOT" \
    SITE_COUNT=4 \
    ACTION=run \
    MAXCYCLES="$MAXCYCLES" \
    STORAGE_INTERVAL="$STORAGE_INTERVAL" \
        bash "$BATCH_INTERFACE"
}

validate_batch() {
    refresh_reports
    python3 "$FREEZE_TOOL" \
        --root "$F2A_ROOT" \
        --plan-root "$PLAN_ROOT" \
        --batch-root "$BATCH_ROOT" \
        --validation-output "$BATCH_ROOT/micro_batch_validation.json" \
        validate
}

freeze_batch() {
    python3 "$FREEZE_TOOL" \
        --root "$F2A_ROOT" \
        --plan-root "$PLAN_ROOT" \
        --batch-root "$BATCH_ROOT" \
        --validation-output "$BATCH_ROOT/micro_batch_validation.json" \
        freeze \
        --checkpoint "$CHECKPOINT"
}

main() {
    require_positive_integer "$MAXCYCLES" "MAXCYCLES"
    require_positive_integer "$STORAGE_INTERVAL" "STORAGE_INTERVAL"
    cd "$F2A_ROOT"
    static_validate

    case "$ACTION" in
        run)
            log "Run or resume the complete four-site micro batch"
            run_batch
            ;;
        validate)
            log "Independently validate all four sites and eight fault instances"
            validate_batch
            ;;
        freeze)
            log "Freeze the validated four-site micro batch"
            freeze_batch
            ;;
        complete)
            log "Complete four-site micro batch: run"
            run_batch
            log "Complete four-site micro batch: validate"
            validate_batch
            log "Complete four-site micro batch: freeze"
            freeze_batch
            ;;
        *)
            fail "unsupported ACTION=$ACTION; allowed: run, validate, freeze, complete"
            ;;
    esac

    printf '\nPlan root  : %s\n' "$PLAN_ROOT"
    printf 'Batch root : %s\n' "$BATCH_ROOT"
    printf 'Checkpoint : %s\n' "$CHECKPOINT"
}

main "$@"
