#!/usr/bin/env bash
set -euo pipefail

F2A_ROOT="${F2A_ROOT:-/raid/spring2026/fwu44/research/fault2assertion}"
ACTION="${ACTION:-prepare}"
PILOT_COUNT="${PILOT_COUNT:-20}"
PILOT_SEED="${PILOT_SEED:-20260801}"
REFERENCE_FAULT="${REFERENCE_FAULT:-TF000002_SA0}"
MAXCYCLES="${MAXCYCLES:-2000000}"
STORAGE_INTERVAL="${STORAGE_INTERVAL:-5}"

TOOL="$F2A_ROOT/scripts/fault_characterization/stage5_batch.py"
ORACLE_TOOL="$F2A_ROOT/scripts/fault_characterization/stage5_batch_oracle.py"
VALIDATOR="$F2A_ROOT/scripts/fault_characterization/stage5_batch_oracle_validate.py"
VERDICT_TOOL="$F2A_ROOT/scripts/fault_characterization/stage5_verdict.py"
ASSERTION_POLICY="$F2A_ROOT/platform/cv32e40p/stage5_assertion_policy_v1.json"
CHECKPOINT="${CHECKPOINT:-$F2A_ROOT/runs/stage5_dev/phase2_v1/g5_oracle/reports/TF000002_SA0_validation.json}"
SELECTED_DIR="${SELECTED_DIR:-$F2A_ROOT/faults/cv32e40p/stage5/fault_specs}"
PILOT_ROOT="$F2A_ROOT/runs/stage5_campaign_v1/cv32e40p/crc32/pilot_${PILOT_COUNT}"

log() {
    printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

require_file() {
    local path="$1"
    local label="$2"
    if [[ ! -s "$path" ]]; then
        printf 'ERROR: %s not found or empty: %s\n' "$label" "$path" >&2
        return 1
    fi
}

static_validation() {
    log "Static validation"
    bash -n "$F2A_ROOT/scripts/run_xrun_stage5_fault.sh"
    bash -n "$F2A_ROOT/scripts/lib/xrun_stage5_common.sh"
    bash -n "$0"
    python3 -m py_compile \
        "$TOOL" \
        "$ORACLE_TOOL" \
        "$VALIDATOR" \
        "$VERDICT_TOOL"
    python3 -m json.tool "$CHECKPOINT" >/dev/null
    python3 -m json.tool "$ASSERTION_POLICY" >/dev/null
    python3 "$VERDICT_TOOL" --version
}

main() {
    cd "$F2A_ROOT"

    require_file "$TOOL" "batch orchestrator"
    require_file "$ORACLE_TOOL" "generic oracle builder"
    require_file "$VALIDATOR" "generic oracle validator"
    require_file "$VERDICT_TOOL" "Stage-5 verdict engine"
    require_file "$ASSERTION_POLICY" "Stage-5 assertion policy"
    require_file "$CHECKPOINT" "Phase2-G5 validation report"

    if [[ ! -d "$SELECTED_DIR" ]]; then
        echo "ERROR: selected fault directory not found: $SELECTED_DIR" >&2
        return 1
    fi

    static_validation

    case "$ACTION" in
        prepare)
            log "Prepare the site-based pilot without running Xcelium"
            python3 "$TOOL" \
                --root "$F2A_ROOT" \
                --checkpoint "$CHECKPOINT" \
                --pilot-root "$PILOT_ROOT" \
                prepare \
                --selected-dir "$SELECTED_DIR" \
                --count "$PILOT_COUNT" \
                --seed "$PILOT_SEED" \
                --include-fault "$REFERENCE_FAULT"
            ;;
        reference)
            log "Run the previously validated reference fault through the generic path"
            python3 "$TOOL" \
                --root "$F2A_ROOT" \
                --checkpoint "$CHECKPOINT" \
                --pilot-root "$PILOT_ROOT" \
                run-one \
                --fault-id "$REFERENCE_FAULT" \
                --maxcycles "$MAXCYCLES"
            ;;
        run)
            log "Run or resume the complete pilot"
            python3 "$TOOL" \
                --root "$F2A_ROOT" \
                --checkpoint "$CHECKPOINT" \
                --pilot-root "$PILOT_ROOT" \
                run-pilot \
                --maxcycles "$MAXCYCLES" \
                --storage-interval "$STORAGE_INTERVAL"
            ;;
        status)
            python3 "$TOOL" \
                --root "$F2A_ROOT" \
                --checkpoint "$CHECKPOINT" \
                --pilot-root "$PILOT_ROOT" \
                status
            ;;
        storage)
            python3 "$TOOL" \
                --root "$F2A_ROOT" \
                --checkpoint "$CHECKPOINT" \
                --pilot-root "$PILOT_ROOT" \
                storage-report
            ;;
        all)
            log "Prepare pilot"
            ACTION=prepare \
            F2A_ROOT="$F2A_ROOT" \
            PILOT_COUNT="$PILOT_COUNT" \
            PILOT_SEED="$PILOT_SEED" \
            REFERENCE_FAULT="$REFERENCE_FAULT" \
            MAXCYCLES="$MAXCYCLES" \
            STORAGE_INTERVAL="$STORAGE_INTERVAL" \
            bash "$0"

            log "Run reference fault"
            ACTION=reference \
            F2A_ROOT="$F2A_ROOT" \
            PILOT_COUNT="$PILOT_COUNT" \
            PILOT_SEED="$PILOT_SEED" \
            REFERENCE_FAULT="$REFERENCE_FAULT" \
            MAXCYCLES="$MAXCYCLES" \
            STORAGE_INTERVAL="$STORAGE_INTERVAL" \
            bash "$0"

            log "Run remaining pilot faults"
            ACTION=run \
            F2A_ROOT="$F2A_ROOT" \
            PILOT_COUNT="$PILOT_COUNT" \
            PILOT_SEED="$PILOT_SEED" \
            REFERENCE_FAULT="$REFERENCE_FAULT" \
            MAXCYCLES="$MAXCYCLES" \
            STORAGE_INTERVAL="$STORAGE_INTERVAL" \
            bash "$0"
            ;;
        *)
            echo "ERROR: unsupported ACTION: $ACTION" >&2
            echo "Allowed: prepare, reference, run, status, storage, all" >&2
            return 1
            ;;
    esac

    echo
    echo "Pilot root: $PILOT_ROOT"
}

main "$@"
