#!/usr/bin/env bash

#
# fault2assertion project environment
#
# Usage:
#   source scripts/setup_env.sh
#

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    echo "ERROR: This script must be sourced, not executed."
    echo "Use:"
    echo "  source scripts/setup_env.sh"
    exit 1
fi

# ----------------------------------------------------------------------
# Project repositories
# ----------------------------------------------------------------------

export F2A_HOME=/raid/spring2026/fwu44/research/fault2assertion
export RESEARCH_HOME=/raid/spring2026/fwu44/research

export CV32E40P_HOME="${RESEARCH_HOME}/cv32e40p"
export IBEX_HOME="${RESEARCH_HOME}/ibex"
export PICORV32_HOME="${RESEARCH_HOME}/picorv32"
export VEER_EH2_HOME="${RESEARCH_HOME}/Cores-VeeR-EH2"

export ASIC_FLOW_HOME="${RESEARCH_HOME}/ASIC-Flow"

# ----------------------------------------------------------------------
# RISC-V toolchain
# ----------------------------------------------------------------------

export RISCV_TOOLCHAIN_HOME=/raid/spring2026/fwu44/tools/riscv32-none-elf-current

export CROSS_COMPILE="${RISCV_TOOLCHAIN_HOME}/bin/riscv32-none-elf-"

case ":${PATH}:" in
    *":${RISCV_TOOLCHAIN_HOME}/bin:"*)
        ;;
    *)
        export PATH="${RISCV_TOOLCHAIN_HOME}/bin:${PATH}"
        ;;
esac

# ----------------------------------------------------------------------
# License
# ----------------------------------------------------------------------

export LM_LICENSE_FILE="${LM_LICENSE_FILE:-27000@mimic.ece.jhu.edu}"

# ----------------------------------------------------------------------
# Optional Cadence environment
# ----------------------------------------------------------------------

CADENCE_ENV="${ASIC_FLOW_HOME}/common/env/cadence_env.sh"

if [[ -f "${CADENCE_ENV}" ]]; then
    source "${CADENCE_ENV}"
else
    echo "WARNING: Cadence environment script not found:"
    echo "  ${CADENCE_ENV}"
    echo "RISC-V compilation can continue, but xrun may not be available."
fi

# ----------------------------------------------------------------------
# Load selected design configuration
# ----------------------------------------------------------------------

DESIGN_ENV="${F2A_HOME}/platform/cv32e40p/env.sh"

if [[ -f "${DESIGN_ENV}" ]]; then
    source "${DESIGN_ENV}"
else
    echo "WARNING: CV32E40P design environment not found:"
    echo "  ${DESIGN_ENV}"
fi

hash -r

echo
echo "fault2assertion environment loaded"
echo "----------------------------------"
echo "F2A_HOME          = ${F2A_HOME}"
echo "CV32E40P_HOME     = ${CV32E40P_HOME}"
echo "RISCV_TOOLCHAIN   = ${RISCV_TOOLCHAIN_HOME}"
echo "CROSS_COMPILE     = ${CROSS_COMPILE}"
echo "LM_LICENSE_FILE   = ${LM_LICENSE_FILE}"

if command -v "${CROSS_COMPILE}gcc" >/dev/null 2>&1 || \
   [[ -x "${CROSS_COMPILE}gcc" ]]; then
    echo "RISC-V GCC        = $("${CROSS_COMPILE}gcc" --version | head -1)"
else
    echo "RISC-V GCC        = NOT FOUND"
fi

if command -v xrun >/dev/null 2>&1; then
    echo "Xcelium xrun      = $(command -v xrun)"
else
    echo "Xcelium xrun      = NOT FOUND"
fi
