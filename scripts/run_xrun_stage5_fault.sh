#!/usr/bin/env bash
set -euo pipefail

# Run one Stage-5 TF fault compile/elaboration or functional simulation.
# Usage:
#   STAGE5_PHASE=compile|run \
#   STAGE5_TRACE_OUTPUT=/absolute/path/to/fault.trace.tsv \
#   ./scripts/run_xrun_stage5_fault.sh \
#       <fault.json> <monitor.sv> <absolute-run-dir>

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
F2A_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
FAULT_JSON="${1:?fault.json is required}"
MONITOR_SV="${2:?monitor.sv is required}"
RUN_DIR="${3:?absolute run directory is required}"

[[ "${RUN_DIR}" == /* ]] || {
    echo "ERROR: run directory must be absolute: ${RUN_DIR}" >&2
    exit 1
}
[[ -s "${FAULT_JSON}" ]] || {
    echo "ERROR: fault spec not found or empty: ${FAULT_JSON}" >&2
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
: "${STAGE5_TRACE_OUTPUT:?STAGE5_TRACE_OUTPUT must be set}"

FAULT_JSON="$(readlink -f -- "${FAULT_JSON}")"
EXTRA_SV_SOURCE="$(readlink -f -- "${MONITOR_SV}")"
STAGE5_TRACE_OUTPUT="$(readlink -m -- "${STAGE5_TRACE_OUTPUT}")"
FAULT_ID="$(python3 - "${FAULT_JSON}" <<'PY'
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
fault_id = str(payload.get("fault_id", ""))
if payload.get("stage") != "stage_05_fault_materialization":
    raise SystemExit(f"ERROR: not a Stage-5 fault spec: {path}")
if not re.fullmatch(r"TF\d{6}_SA[01]", fault_id):
    raise SystemExit(f"ERROR: invalid Stage-5 fault ID: {fault_id!r}")
print(fault_id)
PY
)"

DESIGN="cv32e40p"
WORKLOAD="crc32"
SIM_LEVEL="netlist"
RUN_KIND="fault"
RUN_NAME="$(basename -- "${RUN_DIR}")"
STAGE5_FAULT_APPLIER="${F2A_ROOT}/scripts/fault_characterization/stage5_faults.py"
STAGE5_PHASE="${STAGE5_PHASE:-run}"
VCD="${VCD:-0}"
KEEP_WORK="${KEEP_WORK:-0}"
printf -v WRAPPER_COMMAND \
    'STAGE5_PHASE=%q STAGE5_TRACE_OUTPUT=%q MAXCYCLES=%q VCD=%q VERBOSE=%q KEEP_WORK=%q %q %q %q %q' \
    "${STAGE5_PHASE}" \
    "${STAGE5_TRACE_OUTPUT}" \
    "${MAXCYCLES:-2000000}" \
    "${VCD}" \
    "${VERBOSE:-0}" \
    "${KEEP_WORK}" \
    "$(readlink -f -- "${BASH_SOURCE[0]}")" \
    "${FAULT_JSON}" \
    "${EXTRA_SV_SOURCE}" \
    "${RUN_DIR}"

# Cleanup is intentionally not implemented as an EXIT trap.  The common runner
# decides retention only after a strict verdict and after building a compact
# reproduction bundle.  This prevents compile/simulation failures from losing
# the run-local netlist and Xcelium work library before debugging.

# shellcheck disable=SC1091
source "${F2A_ROOT}/scripts/lib/xrun_stage5_common.sh"
f2a_stage5_run_xrun
