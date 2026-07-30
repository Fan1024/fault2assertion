#!/usr/bin/env bash
set -euo pipefail

# Run every selected branch stuck-at fault without VCD and summarize final status.
#
# Usage:
#   ./scripts/run_branch_campaign_vcd0.sh [run_name]
#
# Common overrides:
#   MAXCYCLES=2000000
#   START_INDEX=1
#   LIMIT=0                    # 0 means all remaining faults
#   STOP_ON_INFRA_ERROR=1      # stop on ERROR/UNKNOWN/RUNNER_ERROR
#   RERUN_INCOMPLETE=0         # set to 1 to remove and rerun an incomplete run dir
#
# Example:
#   MAXCYCLES=2000000 \
#   ./scripts/run_branch_campaign_vcd0.sh screen_seed20260729

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
F2A_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

DESIGN="${DESIGN:-cv32e40p}"
WORKLOAD="${WORKLOAD:-crc32}"
FAULT_TYPE="${FAULT_TYPE:-branchfault}"
RUN_NAME="${1:-screen_seed20260729}"

MAXCYCLES="${MAXCYCLES:-2000000}"
START_INDEX="${START_INDEX:-1}"
LIMIT="${LIMIT:-0}"
STOP_ON_INFRA_ERROR="${STOP_ON_INFRA_ERROR:-1}"
RERUN_INCOMPLETE="${RERUN_INCOMPLETE:-0}"

CAMPAIGN_ROOT="${F2A_ROOT}/faults/${DESIGN}/${FAULT_TYPE}"
SELECTION_JSON="${CAMPAIGN_ROOT}/selection.json"
RUNNER="${F2A_ROOT}/scripts/run_xrun_fault.sh"
SUMMARIZER="${F2A_ROOT}/scripts/fault_injection/summarize_branch_campaign.py"
SCREEN_ROOT="${CAMPAIGN_ROOT}/screening/${RUN_NAME}"
DRIVER_LOG_DIR="${SCREEN_ROOT}/driver_logs"
BATCH_LOG="${SCREEN_ROOT}/batch.log"
FAULT_LIST="${SCREEN_ROOT}/fault_ids.txt"

die() {
    echo "ERROR: $*" >&2
    exit 1
}

is_nonnegative_integer() {
    [[ "$1" =~ ^[0-9]+$ ]]
}

is_positive_integer() {
    [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

[[ "${FAULT_TYPE}" == "branchfault" ]] \
    || die "this script currently supports FAULT_TYPE=branchfault only"

[[ "${RUN_NAME}" =~ ^[A-Za-z0-9_.-]+$ ]] \
    || die "invalid run name: ${RUN_NAME}"

is_positive_integer "${MAXCYCLES}" \
    || die "MAXCYCLES must be a positive integer"
is_positive_integer "${START_INDEX}" \
    || die "START_INDEX must be a positive integer"
is_nonnegative_integer "${LIMIT}" \
    || die "LIMIT must be zero or a positive integer"

case "${STOP_ON_INFRA_ERROR}" in 0|1) ;; *) die "STOP_ON_INFRA_ERROR must be 0 or 1" ;; esac
case "${RERUN_INCOMPLETE}" in 0|1) ;; *) die "RERUN_INCOMPLETE must be 0 or 1" ;; esac

[[ -s "${SELECTION_JSON}" ]] \
    || die "selection.json not found or empty: ${SELECTION_JSON}"
[[ -f "${RUNNER}" ]] \
    || die "fault runner not found: ${RUNNER}"
[[ -f "${SUMMARIZER}" ]] \
    || die "campaign summarizer not found: ${SUMMARIZER}"

mkdir -p "${DRIVER_LOG_DIR}"

python3 - "${SELECTION_JSON}" > "${FAULT_LIST}" <<'PY'
import json
import sys
from pathlib import Path

selection_path = Path(sys.argv[1])
payload = json.loads(selection_path.read_text(encoding="utf-8"))

count = 0
for location in payload.get("selected_locations", []):
    for fault in location.get("faults", []):
        fault_id = fault.get("fault_id")
        if not isinstance(fault_id, str):
            raise SystemExit("ERROR: selection.json contains an invalid fault_id")
        print(fault_id)
        count += 1

if count == 0:
    raise SystemExit("ERROR: selection.json contains no fault IDs")
PY

TOTAL="$(wc -l < "${FAULT_LIST}")"
TOTAL="${TOTAL//[[:space:]]/}"

if (( START_INDEX > TOTAL )); then
    die "START_INDEX=${START_INDEX} exceeds total fault count ${TOTAL}"
fi

END_INDEX="${TOTAL}"
if (( LIMIT > 0 )); then
    candidate_end=$((START_INDEX + LIMIT - 1))
    if (( candidate_end < END_INDEX )); then
        END_INDEX="${candidate_end}"
    fi
fi

summarize_current_state() {
    python3 "${SUMMARIZER}" \
        --campaign-root "${CAMPAIGN_ROOT}" \
        --workload "${WORKLOAD}" \
        --run-name "${RUN_NAME}" \
        --output-dir "${SCREEN_ROOT}" \
        >/dev/null 2>&1 || true
}
trap summarize_current_state EXIT

{
    echo "======================================================================"
    echo "Branch fault VCD=0 campaign"
    echo "======================================================================"
    echo "Project root       : ${F2A_ROOT}"
    echo "Campaign root      : ${CAMPAIGN_ROOT}"
    echo "Selection          : ${SELECTION_JSON}"
    echo "Design             : ${DESIGN}"
    echo "Workload           : ${WORKLOAD}"
    echo "Run name           : ${RUN_NAME}"
    echo "MAXCYCLES           : ${MAXCYCLES}"
    echo "Selected faults    : ${TOTAL}"
    echo "Requested range    : ${START_INDEX}-${END_INDEX}"
    echo "VCD                 : 0"
    echo "KEEP_NETLIST        : 0"
    echo "KEEP_WORK           : 0"
    echo "Stop on infra error : ${STOP_ON_INFRA_ERROR}"
    echo "Started             : $(date --iso-8601=seconds)"
    echo "======================================================================"
} | tee -a "${BATCH_LOG}"

completed_now=0
skipped_complete=0
skipped_incomplete=0
infra_stop=0

for ((index = START_INDEX; index <= END_INDEX; index++)); do
    fault_id="$(sed -n "${index}p" "${FAULT_LIST}")"
    [[ -n "${fault_id}" ]] || die "cannot read fault ID at index ${index}"

    run_dir="${CAMPAIGN_ROOT}/${fault_id}/results/${WORKLOAD}/${RUN_NAME}"
    result_file="${run_dir}/result.txt"
    driver_log="${DRIVER_LOG_DIR}/${fault_id}.log"

    if [[ -s "${result_file}" ]]; then
        status="$(head -n 1 "${result_file}" | tr -d '\r\n')"
        printf '[%d/%d] SKIP %-12s existing=%s\n' \
            "${index}" "${TOTAL}" "${fault_id}" "${status}" \
            | tee -a "${BATCH_LOG}"
        skipped_complete=$((skipped_complete + 1))
        continue
    fi

    if [[ -e "${run_dir}" ]]; then
        if [[ "${RERUN_INCOMPLETE}" == "1" ]]; then
            echo "[${index}/${TOTAL}] REMOVE incomplete run: ${run_dir}" \
                | tee -a "${BATCH_LOG}"
            rm -rf -- "${run_dir}"
        else
            echo "[${index}/${TOTAL}] INCOMPLETE ${fault_id}: ${run_dir}" \
                | tee -a "${BATCH_LOG}"
            skipped_incomplete=$((skipped_incomplete + 1))
            continue
        fi
    fi

    echo "[${index}/${TOTAL}] RUN ${fault_id}" | tee -a "${BATCH_LOG}"

    set +e
    VCD=0 \
    VERBOSE=0 \
    KEEP_NETLIST=0 \
    KEEP_WORK=0 \
    MAXCYCLES="${MAXCYCLES}" \
    bash "${RUNNER}" \
        "${DESIGN}" \
        "${WORKLOAD}" \
        "${FAULT_TYPE}" \
        "${fault_id}" \
        "${RUN_NAME}" \
        > "${driver_log}" 2>&1
    runner_status=$?
    set -e

    if [[ -s "${result_file}" ]]; then
        status="$(head -n 1 "${result_file}" | tr -d '\r\n')"
    else
        status="RUNNER_ERROR"
    fi

    printf '[%d/%d] DONE %-12s result=%-18s runner_exit=%d\n' \
        "${index}" "${TOTAL}" "${fault_id}" "${status}" "${runner_status}" \
        | tee -a "${BATCH_LOG}"

    completed_now=$((completed_now + 1))

    case "${status}" in
        ERROR|UNKNOWN|RUNNER_ERROR)
            if [[ "${STOP_ON_INFRA_ERROR}" == "1" ]]; then
                echo "Stopping on infrastructure result ${status}." \
                    | tee -a "${BATCH_LOG}"
                echo "Inspect: ${driver_log}" | tee -a "${BATCH_LOG}"
                [[ -f "${run_dir}/xrun.log" ]] \
                    && echo "Xrun log: ${run_dir}/xrun.log" | tee -a "${BATCH_LOG}"
                infra_stop=1
            fi
            ;;
    esac

    if (( infra_stop == 1 )); then
        break
    fi
done

summarize_current_state
trap - EXIT

{
    echo "======================================================================"
    echo "Batch pass finished"
    echo "Completed now       : ${completed_now}"
    echo "Skipped complete    : ${skipped_complete}"
    echo "Skipped incomplete  : ${skipped_incomplete}"
    echo "Infrastructure stop : ${infra_stop}"
    echo "Finished            : $(date --iso-8601=seconds)"
    echo "Summary text        : ${SCREEN_ROOT}/campaign_summary.txt"
    echo "Summary JSON        : ${SCREEN_ROOT}/campaign_results.json"
    echo "Summary CSV         : ${SCREEN_ROOT}/campaign_results.csv"
    echo "======================================================================"
} | tee -a "${BATCH_LOG}"

if (( infra_stop == 1 )); then
    exit 3
fi
