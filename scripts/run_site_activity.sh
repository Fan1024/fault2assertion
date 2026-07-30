#!/usr/bin/env bash

set -euo pipefail

# Run one compact golden activity-profiling simulation for the Stage-2 sites.
#
# Usage:
#   ./scripts/run_site_activity.sh [tag]
#
# Useful overrides:
#   MAXCYCLES=2000000 GROUP_WIDTH=256 KEEP_ACTIVITY_WORK=0 \
#     ./scripts/run_site_activity.sh crc32_v1
#
# This workflow always uses VCD=0.  It creates one observation-only monitor,
# runs one golden gate-level simulation, parses the compact TSV, validates the
# Stage-3 JSON, and removes the Xcelium work directory only after success.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
F2A_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

TAG="${1:-crc32_$(date +%Y%m%d_%H%M%S)}"
DESIGN="cv32e40p"
WORKLOAD="crc32"
SIM_LEVEL="netlist"

MAXCYCLES="${MAXCYCLES:-2000000}"
GROUP_WIDTH="${GROUP_WIDTH:-256}"
KEEP_ACTIVITY_WORK="${KEEP_ACTIVITY_WORK:-0}"
EXPECTED_STAGE2_DIGEST="${EXPECTED_STAGE2_DIGEST:-272abe2269bef1f7f5c92c1aa290abac262f1cf8548334c8cefa367ad2032dde}"

SITE_DIR="${F2A_ROOT}/faults/${DESIGN}/site_catalog"
STAGE2_JSON="${SITE_DIR}/stage_02_static_safe.json"
MONITOR_SV="${SITE_DIR}/stage_03_activity_monitor.sv"
MONITOR_MANIFEST="${SITE_DIR}/stage_03_monitor_manifest.json"
RAW_ACTIVITY="${SITE_DIR}/stage_03_activity_raw.tsv"
STAGE3_JSON="${SITE_DIR}/stage_03_activity.json"
STAGE3_REPORT="${SITE_DIR}/stage_03_report.txt"

ACTIVITY_TOOL="${F2A_ROOT}/scripts/fault_sites/activity_profile.py"
GOLDEN_RUNNER="${F2A_ROOT}/scripts/run_xrun_golden.sh"
XRUN_COMMON="${F2A_ROOT}/scripts/lib/xrun_common.sh"
RUN_NAME="stage3_activity_${TAG}"
RUN_DIR="${F2A_ROOT}/golden/${DESIGN}/${WORKLOAD}/${SIM_LEVEL}/${RUN_NAME}"

if [[ ! "${TAG}" =~ ^[A-Za-z0-9_.-]+$ ]]; then
    echo "ERROR: tag may contain only letters, digits, dot, underscore, and dash" >&2
    exit 1
fi

if [[ ! "${MAXCYCLES}" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: MAXCYCLES must be a positive integer" >&2
    exit 1
fi

if [[ ! "${GROUP_WIDTH}" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: GROUP_WIDTH must be a positive integer" >&2
    exit 1
fi

if (( GROUP_WIDTH > 2048 )); then
    echo "ERROR: GROUP_WIDTH must not exceed 2048" >&2
    exit 1
fi

case "${KEEP_ACTIVITY_WORK}" in
    0|1) ;;
    *)
        echo "ERROR: KEEP_ACTIVITY_WORK must be 0 or 1" >&2
        exit 1
        ;;
esac

for required in "${STAGE2_JSON}" "${ACTIVITY_TOOL}" "${GOLDEN_RUNNER}" "${XRUN_COMMON}"; do
    if [[ ! -s "${required}" ]]; then
        echo "ERROR: required file not found or empty: ${required}" >&2
        exit 1
    fi
done

if ! grep -q 'F2A_ACTIVITY_OUTPUT' "${XRUN_COMMON}"; then
    echo "ERROR: xrun_common.sh does not contain the Stage-3 activity hook." >&2
    echo "Install the complete Stage-3 xrun_common.sh before running this script." >&2
    exit 1
fi

mkdir -p "${SITE_DIR}"

if [[ -e "${RUN_DIR}" ]]; then
    echo "ERROR: golden activity run directory already exists:" >&2
    echo "  ${RUN_DIR}" >&2
    echo "Choose another tag or preserve/remove the old run intentionally." >&2
    exit 1
fi

for output in "${RAW_ACTIVITY}" "${STAGE3_JSON}" "${STAGE3_REPORT}"; do
    if [[ -e "${output}" ]]; then
        echo "ERROR: Stage-3 output already exists: ${output}" >&2
        echo "Preserve or remove it intentionally before rerunning." >&2
        exit 1
    fi
done

if [[ -e "${MONITOR_SV}" || -e "${MONITOR_MANIFEST}" ]]; then
    if [[ ! -s "${MONITOR_SV}" || ! -s "${MONITOR_MANIFEST}" ]]; then
        echo "ERROR: incomplete Stage-3 monitor pair exists." >&2
        echo "  ${MONITOR_SV}" >&2
        echo "  ${MONITOR_MANIFEST}" >&2
        exit 1
    fi

    echo "Reusing and validating existing Stage-3 monitor..."
    python3 "${ACTIVITY_TOOL}" validate-monitor \
        --stage2-json "${STAGE2_JSON}" \
        --sv "${MONITOR_SV}" \
        --manifest "${MONITOR_MANIFEST}"
else
    echo
    echo "======================================================================"
    echo "1/4 Generate compact Stage-3 monitor"
    echo "======================================================================"
    python3 "${ACTIVITY_TOOL}" make-monitor \
        --stage2-json "${STAGE2_JSON}" \
        --sv-output "${MONITOR_SV}" \
        --manifest-output "${MONITOR_MANIFEST}" \
        --group-width "${GROUP_WIDTH}" \
        --expect-stage2-digest "${EXPECTED_STAGE2_DIGEST}"

    python3 "${ACTIVITY_TOOL}" validate-monitor \
        --stage2-json "${STAGE2_JSON}" \
        --sv "${MONITOR_SV}" \
        --manifest "${MONITOR_MANIFEST}"
fi

echo
echo "======================================================================"
echo "2/4 Run one golden gate-level activity simulation"
echo "======================================================================"

set +e
F2A_ACTIVITY=1 \
EXTRA_SV_SOURCE="${MONITOR_SV}" \
F2A_ACTIVITY_OUTPUT="${RAW_ACTIVITY}" \
VCD=0 \
LOCAL_PROBE=0 \
KEEP_WORK=1 \
MAXCYCLES="${MAXCYCLES}" \
"${GOLDEN_RUNNER}" \
    "${DESIGN}" "${WORKLOAD}" "${SIM_LEVEL}" "${RUN_NAME}"
RUNNER_STATUS=$?
set -e

if [[ ${RUNNER_STATUS} -ne 0 ]]; then
    echo "ERROR: golden activity simulation failed with status ${RUNNER_STATUS}." >&2
    echo "Run directory was preserved for diagnosis:" >&2
    echo "  ${RUN_DIR}" >&2
    exit "${RUNNER_STATUS}"
fi

if [[ ! -s "${RUN_DIR}/result.txt" ]]; then
    echo "ERROR: simulation result.txt was not generated" >&2
    exit 1
fi

RUN_RESULT="$(tr -d '[:space:]' < "${RUN_DIR}/result.txt")"
if [[ "${RUN_RESULT}" != "PASS" ]]; then
    echo "ERROR: golden activity run result is ${RUN_RESULT}, expected PASS" >&2
    echo "Run directory was preserved:" >&2
    echo "  ${RUN_DIR}" >&2
    exit 1
fi

if [[ ! -s "${RAW_ACTIVITY}" ]]; then
    echo "ERROR: compact activity TSV was not generated: ${RAW_ACTIVITY}" >&2
    exit 1
fi

if find "${RUN_DIR}" -type f -name '*.vcd' -print -quit | grep -q .; then
    echo "ERROR: a VCD was generated even though VCD=0" >&2
    find "${RUN_DIR}" -type f -name '*.vcd' -print >&2
    exit 1
fi

if ! grep -qx '#F2A_ACTIVITY_V1' "${RAW_ACTIVITY}"; then
    echo "ERROR: compact activity TSV marker is missing" >&2
    exit 1
fi

echo
echo "======================================================================"
echo "3/4 Parse activity and select workload-eligible polarities"
echo "======================================================================"

python3 "${ACTIVITY_TOOL}" parse-results \
    --stage2-json "${STAGE2_JSON}" \
    --manifest "${MONITOR_MANIFEST}" \
    --raw-activity "${RAW_ACTIVITY}" \
    --workload "${WORKLOAD}" \
    --run-directory "${RUN_DIR}" \
    --json-output "${STAGE3_JSON}" \
    --text-output "${STAGE3_REPORT}"

echo
echo "======================================================================"
echo "4/4 Fully rebuild and validate Stage-3 output"
echo "======================================================================"

python3 "${ACTIVITY_TOOL}" validate-output \
    --json "${STAGE3_JSON}"

if [[ "${KEEP_ACTIVITY_WORK}" == "0" && -d "${RUN_DIR}/work" ]]; then
    rm -rf -- "${RUN_DIR}/work"
    echo "Removed Xcelium work directory after successful Stage-3 validation."
fi

echo
echo "======================================================================"
echo "Stage 3 completed"
echo "======================================================================"
echo "Golden result    : ${RUN_DIR}/result.txt"
echo "Xcelium log      : ${RUN_DIR}/xrun.log"
echo "Monitor SV       : ${MONITOR_SV}"
echo "Monitor manifest : ${MONITOR_MANIFEST}"
echo "Raw activity TSV : ${RAW_ACTIVITY}"
echo "Stage-3 JSON     : ${STAGE3_JSON}"
echo "Stage-3 report   : ${STAGE3_REPORT}"
echo "VCD generated    : 0"
