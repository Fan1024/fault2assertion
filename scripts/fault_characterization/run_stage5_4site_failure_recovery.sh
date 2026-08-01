#!/usr/bin/env bash
set -euo pipefail

# Recover only the two FAILED cases in the prepared Stage-5 four-site batch.
# Run each action separately and review diagnose before archive-reset.
#
# ACTION=diagnose       Extract failed IDs, first errors, phases and evidence.
# ACTION=archive-reset  Preserve ERROR evidence and reset only those two faults.
# ACTION=rerun          Rerun only the two reset faults; require final 8/8.
# ACTION=validate       Run the existing independent four-site validation.
# ACTION=freeze         Freeze the validated four-site checkpoint.

F2A_ROOT="${F2A_ROOT:-/raid/spring2026/fwu44/research/fault2assertion}"
ACTION="${ACTION:-diagnose}"
MAXCYCLES="${MAXCYCLES:-2000000}"
ALLOW_DISTINCT="${ALLOW_DISTINCT:-0}"

TOOL="$F2A_ROOT/scripts/fault_characterization/stage5_4site_failure_recovery.py"
FOURSITE="$F2A_ROOT/scripts/fault_characterization/run_stage5_4site_micro_batch.sh"
BATCH="$F2A_ROOT/runs/stage5_campaign_v2/cv32e40p/crc32/sites_4"
DIAGNOSIS="$BATCH/recovery/failure_diagnosis.json"
DIAGNOSIS_TEXT="$BATCH/recovery/failure_diagnosis.txt"
RERUN_PLAN="$BATCH/recovery/latest_rerun_plan.json"
RERUN_VERIFY="$BATCH/recovery/rerun_verification.json"
ARCHIVES="$F2A_ROOT/runs/stage5_failure_archives/cv32e40p/crc32/sites_4"

log() {
    printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    return 1
}

require_file() {
    [[ -s "$1" ]] || fail "$2 not found or empty: $1"
}

check_inputs() {
    require_file "$TOOL" "recovery tool"
    require_file "$FOURSITE" "four-site wrapper"
    require_file "$BATCH/pilot_manifest.json" "pilot manifest"
    require_file "$BATCH/pilot_status.json" "pilot status"
    python3 -m py_compile "$TOOL"
    bash -n "$FOURSITE"
    [[ "$MAXCYCLES" =~ ^[1-9][0-9]*$ ]] || fail "invalid MAXCYCLES=$MAXCYCLES"
    [[ "$ALLOW_DISTINCT" == 0 || "$ALLOW_DISTINCT" == 1 ]] || fail "ALLOW_DISTINCT must be 0 or 1"
}

diagnose() {
    python3 "$TOOL" --root "$F2A_ROOT" --batch-root "$BATCH" \
        diagnose --output "$DIAGNOSIS" --text-output "$DIAGNOSIS_TEXT"
}

archive_reset() {
    require_file "$DIAGNOSIS" "failure diagnosis"
    local -a command=(
        python3 "$TOOL" --root "$F2A_ROOT" --batch-root "$BATCH"
        archive-reset --diagnosis "$DIAGNOSIS" --archive-parent "$ARCHIVES"
    )
    [[ "$ALLOW_DISTINCT" == 1 ]] && command+=(--allow-distinct)
    "${command[@]}"
}

rerun_failed() {
    require_file "$RERUN_PLAN" "rerun plan"
    python3 "$TOOL" --root "$F2A_ROOT" --batch-root "$BATCH" \
        rerun --rerun-plan "$RERUN_PLAN" --maxcycles "$MAXCYCLES" \
        --verification-output "$RERUN_VERIFY"
}

main() {
    cd "$F2A_ROOT"
    check_inputs
    case "$ACTION" in
        diagnose)
            log "Diagnose the two FAILED four-site cases"
            diagnose
            ;;
        archive-reset)
            log "Archive ERROR evidence and reset only the failed cases"
            archive_reset
            ;;
        rerun)
            log "Rerun only the two reset fault instances"
            rerun_failed
            ;;
        validate)
            log "Independently validate the recovered four-site batch"
            F2A_ROOT="$F2A_ROOT" ACTION=validate bash "$FOURSITE"
            ;;
        freeze)
            log "Freeze the recovered four-site batch"
            F2A_ROOT="$F2A_ROOT" ACTION=freeze bash "$FOURSITE"
            ;;
        *)
            fail "unsupported ACTION=$ACTION; use diagnose, archive-reset, rerun, validate, or freeze"
            ;;
    esac
    printf '\nBatch root         : %s\n' "$BATCH"
    printf 'Diagnosis          : %s\n' "$DIAGNOSIS"
    printf 'Rerun verification : %s\n' "$RERUN_VERIFY"
    printf 'Failure archives   : %s\n' "$ARCHIVES"
}

main "$@"
