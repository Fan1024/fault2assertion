#!/usr/bin/env bash
set -euo pipefail

# Run one comprehensive Stage-5 golden compact-monitor simulation.
# Usage:
#   ./scripts/run_xrun_stage5_golden.sh <monitor.sv> <absolute-run-dir>

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
F2A_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
MONITOR_SV="${1:?monitor.sv is required}"
RUN_DIR="${2:?absolute run directory is required}"

[[ "${RUN_DIR}" == /* ]] || {
    echo "ERROR: run directory must be absolute: ${RUN_DIR}" >&2
    exit 1
}
[[ -s "${MONITOR_SV}" ]] || {
    echo "ERROR: monitor not found or empty: ${MONITOR_SV}" >&2
    exit 1
}
[[ ! -e "${RUN_DIR}" ]] || {
    echo "ERROR: run directory already exists: ${RUN_DIR}" >&2
    exit 1
}

DESIGN="cv32e40p"
WORKLOAD="crc32"
SIM_LEVEL="netlist"
RUN_KIND="golden"
RUN_NAME="$(basename -- "${RUN_DIR}")"
GOLDEN_NETLIST="${GOLDEN_NETLIST:-}"
EXTRA_SV_SOURCE="$(readlink -f -- "${MONITOR_SV}")"
VCD="${VCD:-0}"
KEEP_WORK="${KEEP_WORK:-0}"

# shellcheck disable=SC1091
source "${F2A_ROOT}/scripts/lib/xrun_stage5_common.sh"
f2a_stage5_run_xrun
