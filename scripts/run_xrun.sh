#!/usr/bin/env bash

set -euo pipefail

# ============================================================================
# fault2assertion Xcelium launcher
#
# Usage:
#   ./scripts/run_xrun.sh <design> <workload> <sim_level> [run_name]
#
# Supported:
#   cv32e40p crc32 rtl
#   cv32e40p crc32 netlist
# ============================================================================

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
F2A_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

SETUP_SCRIPT="${F2A_ROOT}/scripts/setup_env.sh"

if [[ ! -f "${SETUP_SCRIPT}" ]]; then
    echo "ERROR: setup script not found:"
    echo "  ${SETUP_SCRIPT}"
    exit 1
fi

source "${SETUP_SCRIPT}"

F2A_HOME="${F2A_HOME:-${F2A_ROOT}}"

DESIGN="${1:-cv32e40p}"
WORKLOAD="${2:-crc32}"
SIM_LEVEL="${3:-rtl}"
RUN_NAME="${4:-run_$(date +%Y%m%d_%H%M%S)}"

MAXCYCLES="${MAXCYCLES:-5000000}"
VCD="${VCD:-0}"
VERBOSE="${VERBOSE:-0}"

if [[ "${DESIGN}" != "cv32e40p" ]]; then
    echo "ERROR: unsupported design: ${DESIGN}"
    exit 1
fi

case "${SIM_LEVEL}" in
    rtl|netlist)
        ;;
    *)
        echo "ERROR: unsupported simulation level: ${SIM_LEVEL}"
        echo "Supported levels: rtl, netlist"
        exit 1
        ;;
esac

CV32E40P_HOME="${CV32E40P_HOME:-/raid/spring2026/fwu44/research/cv32e40p}"

RTL_DIR="${CV32E40P_HOME}/rtl"
TB_DIR="${CV32E40P_HOME}/verification/shared/tb"
RTL_MANIFEST="${CV32E40P_HOME}/cv32e40p_manifest.flist"

BUILD_DIR="${F2A_HOME}/build/${DESIGN}/${WORKLOAD}"
FIRMWARE="${BUILD_DIR}/${WORKLOAD}.hex"
ELF_FILE="${BUILD_DIR}/${WORKLOAD}.elf"

GOLDEN_ROOT="${F2A_HOME}/golden/${DESIGN}/${WORKLOAD}/${SIM_LEVEL}"
RUN_DIR="${GOLDEN_ROOT}/${RUN_NAME}"

CV32E40P_MAPPED_NETLIST="${CV32E40P_MAPPED_NETLIST:-}"
CV32E40P_CELL_MODEL="${CV32E40P_CELL_MODEL:-}"

if ! command -v xrun >/dev/null 2>&1; then
    echo "ERROR: xrun was not found in PATH."
    exit 1
fi

if [[ ! -s "${FIRMWARE}" ]]; then
    echo "ERROR: firmware not found or empty:"
    echo "  ${FIRMWARE}"
    exit 1
fi

if [[ ! -s "${ELF_FILE}" ]]; then
    echo "ERROR: ELF not found or empty:"
    echo "  ${ELF_FILE}"
    exit 1
fi

if [[ ! -d "${TB_DIR}" ]]; then
    echo "ERROR: testbench directory not found:"
    echo "  ${TB_DIR}"
    exit 1
fi

if [[ -e "${RUN_DIR}" ]]; then
    echo "ERROR: run directory already exists:"
    echo "  ${RUN_DIR}"
    exit 1
fi

TB_SUBSYSTEM_SOURCE="${TB_DIR}/cv32e40p_tb_subsystem.sv"

if [[ "${SIM_LEVEL}" == "netlist" ]]; then
    TB_SUBSYSTEM_SOURCE="${F2A_HOME}/platform/cv32e40p/tb/cv32e40p_tb_subsystem.sv"
fi

TB_SOURCES=(
    "${TB_DIR}/include/perturbation_pkg.sv"
    "${TB_DIR}/amo_shim.sv"
    "${TB_DIR}/cv32e40p_random_interrupt_generator.sv"
    "${TB_DIR}/dp_ram.sv"
    "${TB_DIR}/riscv_gnt_stall.sv"
    "${TB_DIR}/riscv_rvalid_stall.sv"
    "${TB_DIR}/mm_ram.sv"
    "${TB_SUBSYSTEM_SOURCE}"
    "${TB_DIR}/tb_top.sv"
)

for source_file in "${TB_SOURCES[@]}"; do
    if [[ ! -f "${source_file}" ]]; then
        echo "ERROR: testbench source not found:"
        echo "  ${source_file}"
        exit 1
    fi
done

mkdir -p "${RUN_DIR}"

cp "${FIRMWARE}" "${RUN_DIR}/firmware.hex"

printf '%s\n' "${FIRMWARE}" \
    > "${RUN_DIR}/firmware_source.txt"

sha256sum "${FIRMWARE}" "${ELF_FILE}" \
    > "${RUN_DIR}/firmware.sha256"

DESIGN_SOURCES_FILE="${RUN_DIR}/design_sources.f"

if [[ "${SIM_LEVEL}" == "rtl" ]]; then
    if [[ ! -f "${RTL_MANIFEST}" ]]; then
        echo "ERROR: RTL manifest not found:"
        echo "  ${RTL_MANIFEST}"
        exit 1
    fi

    sed \
        "s|\${DESIGN_RTL_DIR}|${RTL_DIR}|g" \
        "${RTL_MANIFEST}" \
        > "${DESIGN_SOURCES_FILE}"
else
    if [[ ! -s "${CV32E40P_MAPPED_NETLIST}" ]]; then
        echo "ERROR: mapped netlist not configured or empty:"
        echo "  ${CV32E40P_MAPPED_NETLIST}"
        exit 1
    fi

    if [[ ! -s "${CV32E40P_CELL_MODEL}" ]]; then
        echo "ERROR: standard-cell model not configured or empty:"
        echo "  ${CV32E40P_CELL_MODEL}"
        exit 1
    fi

    PACKAGE_SOURCES=(
        "${RTL_DIR}/include/cv32e40p_apu_core_pkg.sv"
        "${RTL_DIR}/include/cv32e40p_pkg.sv"
        "${RTL_DIR}/include/cv32e40p_fpu_pkg.sv"
    )

    for package_file in "${PACKAGE_SOURCES[@]}"; do
        if [[ ! -f "${package_file}" ]]; then
            echo "ERROR: required CV32E40P package not found:"
            echo "  ${package_file}"
            exit 1
        fi
    done

    NETLIST_PREP_SCRIPT="${F2A_HOME}/platform/cv32e40p/prepare_netlist.py"
    SIM_NETLIST="${RUN_DIR}/cv32e40p.mapped.sim.v"

    if [[ ! -f "${NETLIST_PREP_SCRIPT}" ]]; then
        echo "ERROR: netlist preparation script not found:"
        echo "  ${NETLIST_PREP_SCRIPT}"
        exit 1
    fi

    python3 \
        "${NETLIST_PREP_SCRIPT}" \
        "${CV32E40P_MAPPED_NETLIST}" \
        "${SIM_NETLIST}"

    if [[ ! -s "${SIM_NETLIST}" ]]; then
        echo "ERROR: run-local simulation netlist was not generated:"
        echo "  ${SIM_NETLIST}"
        exit 1
    fi

    {
        printf '%s\n' "${PACKAGE_SOURCES[@]}"
        printf '%s\n' "${CV32E40P_CELL_MODEL}"
        printf '%s\n' "${SIM_NETLIST}"
    } > "${DESIGN_SOURCES_FILE}"

    sha256sum \
        "${CV32E40P_MAPPED_NETLIST}" \
        "${CV32E40P_CELL_MODEL}" \
        > "${RUN_DIR}/netlist_sources.sha256"

    sha256sum "${SIM_NETLIST}" \
        > "${RUN_DIR}/simulation_netlist.sha256"

    printf '%s\n' "${CV32E40P_MAPPED_NETLIST}" \
        > "${RUN_DIR}/mapped_netlist_source.txt"

    printf '%s\n' "${CV32E40P_CELL_MODEL}" \
        > "${RUN_DIR}/cell_model_source.txt"
fi

git -C "${CV32E40P_HOME}" rev-parse HEAD \
    > "${RUN_DIR}/cv32e40p_commit.txt" 2>/dev/null || true

git -C "${CV32E40P_HOME}" status --short \
    > "${RUN_DIR}/cv32e40p_status.txt" 2>/dev/null || true

git -C "${F2A_HOME}" rev-parse HEAD \
    > "${RUN_DIR}/fault2assertion_commit.txt" 2>/dev/null || true

git -C "${F2A_HOME}" status --short \
    > "${RUN_DIR}/fault2assertion_status.txt" 2>/dev/null || true

cat > "${RUN_DIR}/manifest.txt" <<MANIFEST
design=${DESIGN}
workload=${WORKLOAD}
simulation_level=${SIM_LEVEL}
run_name=${RUN_NAME}
run_time=$(date --iso-8601=seconds)
firmware=${FIRMWARE}
elf=${ELF_FILE}
cv32e40p_home=${CV32E40P_HOME}
mapped_netlist=${CV32E40P_MAPPED_NETLIST}
cell_model=${CV32E40P_CELL_MODEL}
maxcycles=${MAXCYCLES}
vcd=${VCD}
verbose=${VERBOSE}
expected_crc32_vector=0xCBF43926
expected_crc32_signature=0x2D6352B3
MANIFEST

INCLUDE_DIRS=(
    "${RTL_DIR}/include"
    "${CV32E40P_HOME}/bhv"
    "${CV32E40P_HOME}/bhv/include"
    "${CV32E40P_HOME}/sva"
    "${TB_DIR}/include"
)

INCLUDE_ARGS=()

for include_dir in "${INCLUDE_DIRS[@]}"; do
    if [[ -d "${include_dir}" ]]; then
        INCLUDE_ARGS+=("+incdir+${include_dir}")
    fi
done

XRUN_ARGS=(
    -64bit
    -licqueue
    -clean
    -sv
    -timescale 1ns/1ps
    -access +rwc
    -top tb_top

    -f "${DESIGN_SOURCES_FILE}"

    "${INCLUDE_ARGS[@]}"
    "${TB_SOURCES[@]}"

    "+firmware=${RUN_DIR}/firmware.hex"
    "+maxcycles=${MAXCYCLES}"

    -l xrun.log
)

# First gate-level test is functional/zero-delay.
# SDF will be added only after this run passes.
if [[ "${SIM_LEVEL}" == "netlist" ]]; then
    XRUN_ARGS+=(
        +define+TETRAMAX
    -delay_mode zero
        -notimingchecks
    )
fi

if [[ "${VCD}" == "1" ]]; then
    XRUN_ARGS+=(+vcd)
fi

if [[ "${VERBOSE}" == "1" ]]; then
    XRUN_ARGS+=(+verbose)
fi

{
    printf 'xrun'
    printf ' %q' "${XRUN_ARGS[@]}"
    printf '\n'
} > "${RUN_DIR}/command.txt"

echo
echo "======================================================================"
echo "fault2assertion simulation"
echo "======================================================================"
echo "Design         : ${DESIGN}"
echo "Workload       : ${WORKLOAD}"
echo "Simulation     : ${SIM_LEVEL}"
echo "Firmware       : ${FIRMWARE}"

if [[ "${SIM_LEVEL}" == "netlist" ]]; then
    echo "Mapped netlist : ${CV32E40P_MAPPED_NETLIST}"
    echo "Cell model     : ${CV32E40P_CELL_MODEL}"
    echo "Delay mode     : zero"
fi

echo "Run directory  : ${RUN_DIR}"
echo "Maximum cycles : ${MAXCYCLES}"
echo "VCD enabled    : ${VCD}"
echo "Xcelium        : $(command -v xrun)"
echo "======================================================================"

cd "${RUN_DIR}"

set +e
xrun "${XRUN_ARGS[@]}"
XRUN_STATUS=$?
set -e

echo
echo "======================================================================"
echo "Simulation result"
echo "======================================================================"

if grep -q \
    "Simulation aborted due to maximum cycle limit" \
    xrun.log; then

    echo "TIMEOUT" > result.txt
    echo "TIMEOUT: firmware did not finish within ${MAXCYCLES} cycles."
    echo "Log:"
    echo "  ${RUN_DIR}/xrun.log"
    exit 2
fi

if [[ ${XRUN_STATUS} -ne 0 ]]; then
    echo "ERROR" > result.txt
    echo "ERROR: Xcelium returned status ${XRUN_STATUS}."
    echo "Inspect:"
    echo "  ${RUN_DIR}/xrun.log"
    exit "${XRUN_STATUS}"
fi

if grep -Eqi \
    "EXIT FAILURE|TEST\(S\) FAILED|Simulation aborted due to maximum cycle limit" \
    xrun.log; then

    echo "FAIL" > result.txt
    echo "FAIL: firmware execution failed."
    echo "Log:"
    echo "  ${RUN_DIR}/xrun.log"
    exit 2
fi

if grep -qi "CRC32 FAIL" xrun.log; then
    echo "FAIL" > result.txt
    echo "FAIL: CRC32 workload reported an incorrect result."
    exit 2
fi

if [[ "${WORKLOAD}" == "crc32" ]]; then
    if grep -Eqi \
        "CRC32 PASS:.*vector=cbf43926.*signature=2d6352b3" \
        xrun.log &&
       grep -q "EXIT SUCCESS" xrun.log; then

        echo "PASS" > result.txt

        grep -Ei \
            "CRC32 PASS|EXIT SUCCESS" \
            xrun.log \
            > golden_signature.txt || true

        echo "PASS: CRC32 ${SIM_LEVEL} golden simulation completed successfully."
        echo "Log : ${RUN_DIR}/xrun.log"

        if [[ -f riscy_tb.vcd ]]; then
            echo "VCD : ${RUN_DIR}/riscy_tb.vcd"
        fi

        exit 0
    fi

    echo "UNKNOWN" > result.txt
    echo "ERROR: CRC32 golden signature was not found."
    echo "Expected vector    : 0xCBF43926"
    echo "Expected signature : 0x2D6352B3"
    echo "Inspect:"
    echo "  ${RUN_DIR}/xrun.log"
    exit 3
fi

if grep -q "EXIT SUCCESS" xrun.log; then
    echo "PASS" > result.txt
    echo "PASS: ${SIM_LEVEL} simulation completed successfully."
    exit 0
fi

echo "UNKNOWN" > result.txt
echo "ERROR: simulation ended without an explicit result."
echo "Inspect:"
echo "  ${RUN_DIR}/xrun.log"
exit 3
