#!/usr/bin/env bash
set -euo pipefail

# Run one Stage-5 golden compile/elaboration or functional simulation.
# Usage:
#   STAGE5_PHASE=compile|run \
#   STAGE5_TRACE_OUTPUT=/absolute/path/to/golden.trace.tsv \
#   GOLDEN_NETLIST=/absolute/path/to/cv32e40p.mapped.v \
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
: "${GOLDEN_NETLIST:?GOLDEN_NETLIST must be set}"
: "${STAGE5_TRACE_OUTPUT:?STAGE5_TRACE_OUTPUT must be set}"

DESIGN="cv32e40p"
WORKLOAD="crc32"
SIM_LEVEL="netlist"
RUN_KIND="golden"
RUN_NAME="$(basename -- "${RUN_DIR}")"
GOLDEN_NETLIST="$(readlink -f -- "${GOLDEN_NETLIST}")"
EXTRA_SV_SOURCE="$(readlink -f -- "${MONITOR_SV}")"
STAGE5_PHASE="${STAGE5_PHASE:-run}"
STAGE5_TRACE_OUTPUT="$(readlink -m -- "${STAGE5_TRACE_OUTPUT}")"
VCD="${VCD:-0}"
KEEP_WORK="${KEEP_WORK:-0}"
printf -v WRAPPER_COMMAND \
    'STAGE5_PHASE=%q STAGE5_TRACE_OUTPUT=%q GOLDEN_NETLIST=%q MAXCYCLES=%q VCD=%q VERBOSE=%q KEEP_WORK=%q %q %q %q' \
    "${STAGE5_PHASE}" \
    "${STAGE5_TRACE_OUTPUT}" \
    "${GOLDEN_NETLIST}" \
    "${MAXCYCLES:-2000000}" \
    "${VCD}" \
    "${VERBOSE:-0}" \
    "${KEEP_WORK}" \
    "$(readlink -f -- "${BASH_SOURCE[0]}")" \
    "${EXTRA_SV_SOURCE}" \
    "${RUN_DIR}"

# shellcheck disable=SC1091
source "${F2A_ROOT}/scripts/lib/xrun_stage5_common.sh"
f2a_stage5_run_xrun
