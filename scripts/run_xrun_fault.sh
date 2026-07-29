#!/usr/bin/env bash

set -euo pipefail

# Usage:
#   ./scripts/run_xrun_fault.sh <design> <workload> <fault_type> <fault_id> [run_name]
# Example:
#   VCD=1 ./scripts/run_xrun_fault.sh \
#       cv32e40p crc32 branchfault BF0001_SA0 run_001_vcd

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
F2A_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

DESIGN="${1:-cv32e40p}"
WORKLOAD="${2:-crc32}"
FAULT_TYPE="${3:-branchfault}"
FAULT_ID="${4:-}"
RUN_NAME="${5:-run_$(date +%Y%m%d_%H%M%S)}"

if [[ -z "${FAULT_ID}" ]]; then
    echo "ERROR: fault_id is required."
    echo "Usage: $0 <design> <workload> <fault_type> <fault_id> [run_name]"
    exit 1
fi

if [[ ! "${FAULT_TYPE}" =~ ^[A-Za-z0-9_]+$ ]]; then
    echo "ERROR: invalid fault_type: ${FAULT_TYPE}"
    exit 1
fi

if [[ "${FAULT_TYPE}" == "branchfault" && ! "${FAULT_ID}" =~ ^BF[0-9]{4}_SA[01]$ ]]; then
    echo "ERROR: invalid branch fault ID: ${FAULT_ID}"
    echo "Expected format: BF0001_SA0 or BF0001_SA1"
    exit 1
fi

RUN_KIND="fault"
SIM_LEVEL="netlist"
FAULT_DIR="${F2A_ROOT}/faults/${DESIGN}/${FAULT_TYPE}/${FAULT_ID}"
FAULT_JSON="${FAULT_DIR}/fault.json"
RUN_DIR="${FAULT_DIR}/results/${WORKLOAD}/${RUN_NAME}"

if [[ ! -s "${FAULT_JSON}" ]]; then
    echo "ERROR: fault metadata not found or empty:"
    echo "  ${FAULT_JSON}"
    exit 1
fi

# shellcheck disable=SC1091
source "${F2A_ROOT}/scripts/lib/xrun_common.sh"
f2a_run_xrun
