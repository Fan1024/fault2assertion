#!/usr/bin/env bash

set -euo pipefail

# One-command local diagnosis for a selected branch stuck-at fault.
#
# Usage:
#   ./scripts/run_branch_probe.sh BF0001_SA0 [tag]
#
# Useful overrides:
#   MAXCYCLES=2000000 KEEP_PROBE_VCD=1 KEEP_PROBE_WORK=1 \
#     ./scripts/run_branch_probe.sh BF0001_SA0 debug1

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
F2A_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

FAULT_ID="${1:-}"
TAG="${2:-$(date +%Y%m%d_%H%M%S)}"
MAXCYCLES="${MAXCYCLES:-2000000}"
KEEP_PROBE_VCD="${KEEP_PROBE_VCD:-0}"
KEEP_PROBE_WORK="${KEEP_PROBE_WORK:-0}"

DESIGN="cv32e40p"
WORKLOAD="crc32"
FAULT_TYPE="branchfault"
CAMPAIGN_ROOT="${F2A_ROOT}/faults/${DESIGN}/${FAULT_TYPE}"
FAULT_DIR="${CAMPAIGN_ROOT}/${FAULT_ID}"
FAULT_JSON="${FAULT_DIR}/fault.json"
INJECTOR="${F2A_ROOT}/scripts/fault_injection/branch_fault.py"
COMPARE_TOOL="${F2A_ROOT}/scripts/fault_injection/compare_local_probe.py"

if [[ ! "${FAULT_ID}" =~ ^BF[0-9]{4}_SA[01]$ ]]; then
    echo "ERROR: expected fault ID such as BF0001_SA0 or BF0001_SA1" >&2
    exit 1
fi
if [[ ! "${TAG}" =~ ^[A-Za-z0-9_.-]+$ ]]; then
    echo "ERROR: tag may contain only letters, digits, dot, underscore, and dash" >&2
    exit 1
fi
for flag_name in KEEP_PROBE_VCD KEEP_PROBE_WORK; do
    value="${!flag_name}"
    if [[ "${value}" != "0" && "${value}" != "1" ]]; then
        echo "ERROR: ${flag_name} must be 0 or 1" >&2
        exit 1
    fi
done

if [[ ! -s "${FAULT_JSON}" ]]; then
    echo "Materializing ${FAULT_ID} metadata ..."
    python3 "${INJECTOR}" materialize \
        --output-root "${CAMPAIGN_ROOT}" \
        --fault-id "${FAULT_ID}"
fi

if [[ ! -s "${FAULT_JSON}" ]]; then
    echo "ERROR: fault metadata was not created: ${FAULT_JSON}" >&2
    exit 1
fi

GOLDEN_RUN_NAME="probe_${FAULT_ID}_${TAG}_golden"
FAULT_RUN_NAME="probe_${TAG}_fault"
GOLDEN_RUN_DIR="${F2A_ROOT}/golden/${DESIGN}/${WORKLOAD}/netlist/${GOLDEN_RUN_NAME}"
FAULT_RUN_DIR="${FAULT_DIR}/results/${WORKLOAD}/${FAULT_RUN_NAME}"
GOLDEN_VCD="${GOLDEN_RUN_DIR}/work/riscy_tb.vcd"
FAULT_VCD="${FAULT_RUN_DIR}/work/riscy_tb.vcd"
REPORT_JSON="${FAULT_RUN_DIR}/local_probe_compare.json"
REPORT_TEXT="${FAULT_RUN_DIR}/local_probe_compare.txt"

if [[ -e "${GOLDEN_RUN_DIR}" || -e "${FAULT_RUN_DIR}" ]]; then
    echo "ERROR: run directory already exists. Choose a different tag." >&2
    echo "  golden: ${GOLDEN_RUN_DIR}" >&2
    echo "  fault : ${FAULT_RUN_DIR}" >&2
    exit 1
fi

echo
echo "======================================================================"
echo "1/3 Golden local-probe simulation"
echo "======================================================================"
LOCAL_PROBE=1 \
PROBE_FAULT_JSON="${FAULT_JSON}" \
VCD=1 \
MAXCYCLES="${MAXCYCLES}" \
"${F2A_ROOT}/scripts/run_xrun_golden.sh" \
    "${DESIGN}" "${WORKLOAD}" netlist "${GOLDEN_RUN_NAME}"

if [[ ! -s "${GOLDEN_VCD}" ]]; then
    echo "ERROR: golden local-probe VCD was not generated: ${GOLDEN_VCD}" >&2
    exit 1
fi

echo
echo "======================================================================"
echo "2/3 Fault local-probe simulation"
echo "======================================================================"
set +e
LOCAL_PROBE=1 \
VCD=1 \
MAXCYCLES="${MAXCYCLES}" \
"${F2A_ROOT}/scripts/run_xrun_fault.sh" \
    "${DESIGN}" "${WORKLOAD}" "${FAULT_TYPE}" "${FAULT_ID}" "${FAULT_RUN_NAME}"
FAULT_RUN_STATUS=$?
set -e

if [[ ! -s "${FAULT_VCD}" ]]; then
    echo "ERROR: fault local-probe VCD was not generated: ${FAULT_VCD}" >&2
    echo "Fault runner exit status: ${FAULT_RUN_STATUS}" >&2
    if [[ "${FAULT_RUN_STATUS}" -eq 0 ]]; then
        exit 1
    fi
    exit "${FAULT_RUN_STATUS}"
fi

echo
echo "======================================================================"
echo "3/3 Activation and local-propagation comparison"
echo "======================================================================"
COMPARE_ARGS=(
    --golden-vcd "${GOLDEN_VCD}"
    --fault-vcd "${FAULT_VCD}"
    --fault-json "${FAULT_JSON}"
    --json-output "${REPORT_JSON}"
    --text-output "${REPORT_TEXT}"
)
if [[ "${KEEP_PROBE_VCD}" == "0" ]]; then
    COMPARE_ARGS+=(--delete-vcds)
fi
python3 "${COMPARE_TOOL}" "${COMPARE_ARGS[@]}"

if [[ "${KEEP_PROBE_WORK}" == "0" && "${KEEP_PROBE_VCD}" == "0" ]]; then
    rm -rf -- "${GOLDEN_RUN_DIR}/work" "${FAULT_RUN_DIR}/work"
    echo "Removed local Xcelium work directories after successful comparison."
fi

echo
echo "======================================================================"
echo "Local probe completed"
echo "======================================================================"
cat "${REPORT_TEXT}"
echo "Golden result : ${GOLDEN_RUN_DIR}/result.txt"
echo "Fault result  : ${FAULT_RUN_DIR}/result.txt"
echo "Probe report : ${REPORT_TEXT}"
echo "Probe JSON   : ${REPORT_JSON}"
echo "Fault runner exit status: ${FAULT_RUN_STATUS}"
