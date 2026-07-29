#!/usr/bin/env bash

set -euo pipefail

# Usage:
#   ./scripts/run_xrun_fault.sh \
#       <design> <workload> <fault_type> <fault_id> [run_name]
#
# Example:
#   VCD=1 ./scripts/run_xrun_fault.sh \
#       cv32e40p crc32 branchfault BF0001_SA0 run_001_vcd
#
# Default behavior:
#   1. Materialize fault.json + fault.patch on demand if they do not exist.
#   2. Generate run-local fault netlists under <run>/work/.
#   3. Run Xcelium through scripts/lib/xrun_common.sh.
#   4. Delete the run-local netlists when this script exits.
#
# Set KEEP_NETLIST=1 only when debugging netlist generation.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
F2A_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

DESIGN="${1:-cv32e40p}"
WORKLOAD="${2:-crc32}"
FAULT_TYPE="${3:-branchfault}"
FAULT_ID="${4:-}"
RUN_NAME="${5:-run_$(date +%Y%m%d_%H%M%S)}"
KEEP_NETLIST="${KEEP_NETLIST:-0}"

if [[ -z "${FAULT_ID}" ]]; then
    echo "ERROR: fault_id is required." >&2
    echo "Usage: $0 <design> <workload> <fault_type> <fault_id> [run_name]" >&2
    exit 1
fi

if [[ ! "${FAULT_TYPE}" =~ ^[A-Za-z0-9_]+$ ]]; then
    echo "ERROR: invalid fault_type: ${FAULT_TYPE}" >&2
    exit 1
fi

if [[ "${FAULT_TYPE}" != "branchfault" ]]; then
    echo "ERROR: only branchfault is supported by this runner for now." >&2
    echo "Received fault_type: ${FAULT_TYPE}" >&2
    exit 1
fi

if [[ ! "${FAULT_ID}" =~ ^BF[0-9]{4}_SA[01]$ ]]; then
    echo "ERROR: invalid branch fault ID: ${FAULT_ID}" >&2
    echo "Expected format: BF0001_SA0 or BF0001_SA1" >&2
    exit 1
fi

case "${KEEP_NETLIST}" in
    0|1) ;;
    *)
        echo "ERROR: KEEP_NETLIST must be 0 or 1; got ${KEEP_NETLIST}" >&2
        exit 1
        ;;
esac

RUN_KIND="fault"
SIM_LEVEL="netlist"
CAMPAIGN_ROOT="${F2A_ROOT}/faults/${DESIGN}/${FAULT_TYPE}"
FAULT_DIR="${CAMPAIGN_ROOT}/${FAULT_ID}"
FAULT_JSON="${FAULT_DIR}/fault.json"
RUN_DIR="${FAULT_DIR}/results/${WORKLOAD}/${RUN_NAME}"
INJECTOR="${F2A_ROOT}/scripts/fault_injection/branch_fault.py"
POPULATION_JSON="${CAMPAIGN_ROOT}/population.json"
SELECTION_JSON="${CAMPAIGN_ROOT}/selection.json"

if [[ -e "${RUN_DIR}" ]]; then
    echo "ERROR: run directory already exists: ${RUN_DIR}" >&2
    echo "Choose another run_name or remove the old run intentionally." >&2
    exit 1
fi

if [[ ! -f "${INJECTOR}" ]]; then
    echo "ERROR: branch fault tool not found: ${INJECTOR}" >&2
    exit 1
fi

# Campaign preparation intentionally creates only population.json and
# selection.json. Materialize one selected fault only when it is requested.
if [[ ! -s "${FAULT_JSON}" ]]; then
    if [[ -d "${FAULT_DIR}" ]]; then
        echo "ERROR: incomplete fault directory exists: ${FAULT_DIR}" >&2
        echo "It must either be absent or contain a non-empty fault.json." >&2
        exit 1
    fi

    if [[ ! -s "${POPULATION_JSON}" ]]; then
        echo "ERROR: population.json not found or empty: ${POPULATION_JSON}" >&2
        exit 1
    fi
    if [[ ! -s "${SELECTION_JSON}" ]]; then
        echo "ERROR: selection.json not found or empty: ${SELECTION_JSON}" >&2
        exit 1
    fi

    echo "Materializing metadata for ${FAULT_ID} ..."
    python3 "${INJECTOR}" materialize \
        --output-root "${CAMPAIGN_ROOT}" \
        --fault-id "${FAULT_ID}"
fi

if [[ ! -s "${FAULT_JSON}" ]]; then
    echo "ERROR: fault metadata was not generated: ${FAULT_JSON}" >&2
    exit 1
fi

cleanup_run_local_netlists() {
    local status=$?
    local work_dir="${RUN_DIR}/work"

    if [[ "${KEEP_NETLIST}" == "1" ]]; then
        if [[ -d "${work_dir}" ]]; then
            echo "KEEP_NETLIST=1: preserved run-local netlists in ${work_dir}"
        fi
        return "${status}"
    fi

    local removed=0
    local path
    for path in \
        "${work_dir}/fault_netlist.v" \
        "${work_dir}/cv32e40p.mapped.sim.v"
    do
        if [[ -f "${path}" ]]; then
            if rm -f -- "${path}"; then
                echo "Removed run-local netlist: ${path}"
                removed=1
            else
                echo "WARNING: failed to remove run-local netlist: ${path}" >&2
            fi
        fi
    done

    if [[ ${removed} -eq 0 && -d "${work_dir}" ]]; then
        echo "No run-local netlist remained to remove."
    fi

    return "${status}"
}

# EXIT also runs when Xcelium or an earlier preparation step fails, so a failed
# experiment does not leave a large temporary fault netlist behind.
trap cleanup_run_local_netlists EXIT

# shellcheck disable=SC1091
source "${F2A_ROOT}/scripts/lib/xrun_common.sh"
f2a_run_xrun
