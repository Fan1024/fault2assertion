#!/usr/bin/env bash

set -euo pipefail

# Usage:
#   ./scripts/run_xrun_golden.sh <design> <workload> <sim_level> [run_name]
# Example:
#   VCD=1 ./scripts/run_xrun_golden.sh cv32e40p crc32 netlist run_golden

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
F2A_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

DESIGN="${1:-cv32e40p}"
WORKLOAD="${2:-crc32}"
SIM_LEVEL="${3:-rtl}"
RUN_NAME="${4:-run_$(date +%Y%m%d_%H%M%S)}"

RUN_KIND="golden"
RUN_DIR="${F2A_ROOT}/golden/${DESIGN}/${WORKLOAD}/${SIM_LEVEL}/${RUN_NAME}"

# Optionally override the default mapped netlist from platform/cv32e40p/env.sh:
#   GOLDEN_NETLIST=/path/to/cv32e40p.mapped.v ./scripts/run_xrun_golden.sh ...
GOLDEN_NETLIST="${GOLDEN_NETLIST:-}"

# shellcheck disable=SC1091
source "${F2A_ROOT}/scripts/lib/xrun_common.sh"
f2a_run_xrun
