#!/usr/bin/env bash

#
# CV32E40P-specific configuration
#
# This file is normally sourced by scripts/setup_env.sh.
#

export CV32E40P_ARCH=rv32imc_zicsr_zifencei
export CV32E40P_ABI=ilp32

export CV32E40P_RUNTIME_DIR="${CV32E40P_HOME}/example_tb/core/custom"

export CV32E40P_RTL_SIM_HOME="${CV32E40P_HOME}/verification/rtl_sim"
export CV32E40P_RTL_WRAPPER="${CV32E40P_RTL_SIM_HOME}/run_xrun.sh"

export CV32E40P_SHARED_HOME="${CV32E40P_HOME}/verification/shared"
export CV32E40P_TB_HOME="${CV32E40P_SHARED_HOME}/tb"

export CV32E40P_DEFAULT_MAXCYCLES=2000000

export CRC32_BUILD_DIR="${F2A_HOME}/build/cv32e40p/crc32"
export CRC32_ELF="${CRC32_BUILD_DIR}/crc32.elf"
export CRC32_HEX="${CRC32_BUILD_DIR}/crc32.hex"

# ----------------------------------------------------------------------
# Post-synthesis gate-level simulation
# ----------------------------------------------------------------------
export CV32E40P_MAPPED_NETLIST="${CV32E40P_MAPPED_NETLIST:-${CV32E40P_HOME}/syn/runs/run_003/results/cv32e40p.SYN/cv32e40p.mapped.v}"
export CV32E40P_CELL_MODEL="${CV32E40P_CELL_MODEL:-/raid/spring2026/fwu44/pdks/NanGate_45nm/pdk_v1.3_v2010_12/NangateOpenCellLibrary_PDKv1_3_v2010_12/Front_End/Verilog/NangateOpenCellLibrary.v}"
