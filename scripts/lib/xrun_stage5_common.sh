#!/usr/bin/env bash

# Dedicated Xcelium implementation for Fault2Assertion Stage 5.
#
# This file is intentionally independent from scripts/lib/xrun_common.sh so the
# existing golden, BF branch-fault, local-probe, and Stage-3 flows are not
# modified.  It must be sourced by run_xrun_stage5_golden.sh or
# run_xrun_stage5_fault.sh, not executed directly.

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    echo "ERROR: xrun_stage5_common.sh must be sourced, not executed." >&2
    exit 1
fi

f2a_stage5_die() {
    echo "ERROR: $*" >&2
    return 1
}

f2a_stage5_require_file() {
    local path="$1"
    local label="$2"
    [[ -s "${path}" ]] || f2a_stage5_die "${label} not found or empty: ${path}"
}

f2a_stage5_validate_flag() {
    local name="$1"
    local value="$2"
    case "${value}" in
        0|1) ;;
        *)
            f2a_stage5_die "${name} must be 0 or 1; got ${value}"
            return 1
            ;;
    esac
}

f2a_stage5_write_command() {
    local output="$1"
    shift
    {
        printf 'xrun'
        printf ' %q' "$@"
        printf '\n'
    } > "${output}"
}

f2a_stage5_run_xrun() {
    : "${RUN_KIND:?RUN_KIND must be golden or fault}"
    : "${DESIGN:?DESIGN must be set}"
    : "${WORKLOAD:?WORKLOAD must be set}"
    : "${SIM_LEVEL:?SIM_LEVEL must be set}"
    : "${RUN_NAME:?RUN_NAME must be set}"
    : "${RUN_DIR:?RUN_DIR must be set}"
    : "${F2A_ROOT:?F2A_ROOT must be set}"
    : "${EXTRA_SV_SOURCE:?EXTRA_SV_SOURCE must be the Stage-5 monitor}"

    local maxcycles="${MAXCYCLES:-2000000}"
    local vcd="${VCD:-0}"
    local verbose="${VERBOSE:-0}"
    local keep_work="${KEEP_WORK:-0}"

    case "${RUN_KIND}" in
        golden|fault) ;;
        *)
            f2a_stage5_die "unsupported RUN_KIND: ${RUN_KIND}"
            return 1
            ;;
    esac

    if [[ "${DESIGN}" != "cv32e40p" ]]; then
        f2a_stage5_die "Stage 5 currently supports only cv32e40p; got ${DESIGN}"
        return 1
    fi
    if [[ "${WORKLOAD}" != "crc32" ]]; then
        f2a_stage5_die "Stage 5 currently supports only crc32; got ${WORKLOAD}"
        return 1
    fi
    if [[ "${SIM_LEVEL}" != "netlist" ]]; then
        f2a_stage5_die "Stage 5 requires netlist simulation; got ${SIM_LEVEL}"
        return 1
    fi
    if [[ "${RUN_DIR}" != /* ]]; then
        f2a_stage5_die "RUN_DIR must be absolute: ${RUN_DIR}"
        return 1
    fi
    if [[ ! "${maxcycles}" =~ ^[1-9][0-9]*$ ]]; then
        f2a_stage5_die "MAXCYCLES must be a positive integer; got ${maxcycles}"
        return 1
    fi

    f2a_stage5_validate_flag VCD "${vcd}" || return 1
    f2a_stage5_validate_flag VERBOSE "${verbose}" || return 1
    f2a_stage5_validate_flag KEEP_WORK "${keep_work}" || return 1

    if [[ -e "${RUN_DIR}" ]]; then
        f2a_stage5_die "run directory already exists: ${RUN_DIR}"
        return 1
    fi

    local setup_script="${F2A_ROOT}/scripts/setup_env.sh"
    [[ -f "${setup_script}" ]] || {
        f2a_stage5_die "setup script not found: ${setup_script}"
        return 1
    }

    # shellcheck disable=SC1090
    source "${setup_script}"

    local f2a_home="${F2A_HOME:-${F2A_ROOT}}"
    local cv32e40p_home="${CV32E40P_HOME:-/raid/spring2026/fwu44/research/cv32e40p}"
    local rtl_dir="${cv32e40p_home}/rtl"
    local tb_dir="${cv32e40p_home}/verification/shared/tb"
    local build_dir="${f2a_home}/build/${DESIGN}/${WORKLOAD}"
    local firmware="${build_dir}/${WORKLOAD}.hex"
    local elf_file="${build_dir}/${WORKLOAD}.elf"
    local cell_model="${CV32E40P_CELL_MODEL:-}"
    local monitor_source
    monitor_source="$(readlink -f -- "${EXTRA_SV_SOURCE}")"

    command -v xrun >/dev/null 2>&1 || {
        f2a_stage5_die "xrun was not found in PATH"
        return 1
    }
    command -v python3 >/dev/null 2>&1 || {
        f2a_stage5_die "python3 was not found in PATH"
        return 1
    }

    f2a_stage5_require_file "${firmware}" "firmware" || return 1
    f2a_stage5_require_file "${elf_file}" "ELF" || return 1
    f2a_stage5_require_file "${cell_model}" "standard-cell model" || return 1
    f2a_stage5_require_file "${monitor_source}" "Stage-5 monitor" || return 1

    [[ -d "${tb_dir}" ]] || {
        f2a_stage5_die "testbench directory not found: ${tb_dir}"
        return 1
    }

    local tb_subsystem_source="${f2a_home}/platform/cv32e40p/tb/cv32e40p_tb_subsystem.sv"
    local -a tb_sources=(
        "${tb_dir}/include/perturbation_pkg.sv"
        "${tb_dir}/amo_shim.sv"
        "${tb_dir}/cv32e40p_random_interrupt_generator.sv"
        "${tb_dir}/dp_ram.sv"
        "${tb_dir}/riscv_gnt_stall.sv"
        "${tb_dir}/riscv_rvalid_stall.sv"
        "${tb_dir}/mm_ram.sv"
        "${tb_subsystem_source}"
        "${tb_dir}/tb_top.sv"
        "${monitor_source}"
    )

    local source_file
    for source_file in "${tb_sources[@]}"; do
        [[ -f "${source_file}" ]] || {
            f2a_stage5_die "simulation source not found: ${source_file}"
            return 1
        }
    done

    local -a package_sources=(
        "${rtl_dir}/include/cv32e40p_apu_core_pkg.sv"
        "${rtl_dir}/include/cv32e40p_pkg.sv"
        "${rtl_dir}/include/cv32e40p_fpu_pkg.sv"
    )
    for source_file in "${package_sources[@]}"; do
        f2a_stage5_require_file "${source_file}" "CV32E40P package" || return 1
    done

    local netlist_prep_script="${f2a_home}/platform/cv32e40p/prepare_netlist.py"
    f2a_stage5_require_file "${netlist_prep_script}" "netlist preparation script" \
        || return 1

    mkdir -p "${RUN_DIR}"
    local work_dir="${RUN_DIR}/work"
    mkdir -p "${work_dir}"

    cp -- "${firmware}" "${work_dir}/firmware.hex"
    cp -- "${monitor_source}" "${RUN_DIR}/stage5_monitor.sv"
    printf '%s\n' "${firmware}" > "${RUN_DIR}/firmware_source.txt"
    sha256sum "${firmware}" "${elf_file}" > "${RUN_DIR}/firmware.sha256"
    sha256sum "${monitor_source}" > "${RUN_DIR}/stage5_monitor.sha256"

    local raw_netlist=""
    if [[ "${RUN_KIND}" == "golden" ]]; then
        raw_netlist="${GOLDEN_NETLIST:-${CV32E40P_MAPPED_NETLIST:-}}"
        f2a_stage5_require_file "${raw_netlist}" "golden mapped netlist" || return 1
    else
        local fault_json="${FAULT_JSON:?FAULT_JSON must be set for a fault run}"
        local fault_applier="${STAGE5_FAULT_APPLIER:-${f2a_home}/scripts/fault_characterization/stage5_faults.py}"

        f2a_stage5_require_file "${fault_json}" "Stage-5 fault spec" || return 1
        f2a_stage5_require_file "${fault_applier}" "Stage-5 fault materializer" \
            || return 1

        raw_netlist="${work_dir}/fault_netlist.v"
        python3 "${fault_applier}" apply \
            --fault-json "${fault_json}" \
            --output-netlist "${raw_netlist}"
        f2a_stage5_require_file "${raw_netlist}" "run-local fault netlist" || return 1
        cp -- "${fault_json}" "${RUN_DIR}/fault.json"
    fi

    local sim_netlist="${work_dir}/cv32e40p.mapped.sim.v"
    python3 "${netlist_prep_script}" "${raw_netlist}" "${sim_netlist}"
    f2a_stage5_require_file "${sim_netlist}" "run-local simulation netlist" \
        || return 1

    local design_sources_file="${work_dir}/design_sources.f"
    {
        printf '%s\n' "${package_sources[@]}"
        printf '%s\n' "${cell_model}"
        printf '%s\n' "${sim_netlist}"
    } > "${design_sources_file}"

    sha256sum "${raw_netlist}" "${cell_model}" \
        > "${RUN_DIR}/netlist_sources.sha256"
    sha256sum "${sim_netlist}" > "${RUN_DIR}/simulation_netlist.sha256"
    printf '%s\n' "${raw_netlist}" > "${RUN_DIR}/mapped_netlist_source.txt"
    printf '%s\n' "${cell_model}" > "${RUN_DIR}/cell_model_source.txt"

    git -C "${cv32e40p_home}" rev-parse HEAD \
        > "${RUN_DIR}/cv32e40p_commit.txt" 2>/dev/null || true
    git -C "${f2a_home}" rev-parse HEAD \
        > "${RUN_DIR}/fault2assertion_commit.txt" 2>/dev/null || true

    cat > "${RUN_DIR}/manifest.txt" <<MANIFEST
stage=5
run_kind=${RUN_KIND}
design=${DESIGN}
workload=${WORKLOAD}
simulation_level=${SIM_LEVEL}
run_name=${RUN_NAME}
run_time=$(date --iso-8601=seconds)
run_directory=${RUN_DIR}
work_directory=${work_dir}
firmware=${firmware}
elf=${elf_file}
cv32e40p_home=${cv32e40p_home}
raw_simulation_netlist=${raw_netlist}
cell_model=${cell_model}
fault_id=${FAULT_ID:-}
fault_json=${FAULT_JSON:-}
stage5_monitor=${monitor_source}
maxcycles=${maxcycles}
vcd=${vcd}
verbose=${verbose}
keep_work=${keep_work}
expected_crc32_vector=0xCBF43926
expected_crc32_signature=0x2D6352B3
MANIFEST

    local -a include_dirs=(
        "${rtl_dir}/include"
        "${cv32e40p_home}/bhv"
        "${cv32e40p_home}/bhv/include"
        "${cv32e40p_home}/sva"
        "${tb_dir}/include"
    )
    local -a include_args=()
    local include_dir
    for include_dir in "${include_dirs[@]}"; do
        if [[ -d "${include_dir}" ]]; then
            include_args+=("+incdir+${include_dir}")
        fi
    done

    local -a xrun_args=(
        -64bit
        -licqueue
        -clean
        -sv
        -timescale 1ns/1ps
        -access +rwc
        -top tb_top
        -f "${design_sources_file}"
        "${include_args[@]}"
        "${tb_sources[@]}"
        "+firmware=${work_dir}/firmware.hex"
        "+maxcycles=${maxcycles}"
        -l "${RUN_DIR}/xrun.log"
        +define+TETRAMAX
        -delay_mode zero
        -notimingchecks
    )

    if [[ "${vcd}" == "1" ]]; then
        xrun_args+=(+vcd)
    fi
    if [[ "${verbose}" == "1" ]]; then
        xrun_args+=(+verbose)
    fi

    f2a_stage5_write_command "${RUN_DIR}/command.txt" "${xrun_args[@]}"

    echo
    echo "======================================================================"
    echo "Fault2Assertion Stage-5 ${RUN_KIND} simulation"
    echo "======================================================================"
    echo "Design         : ${DESIGN}"
    echo "Workload       : ${WORKLOAD}"
    echo "Simulation     : ${SIM_LEVEL}"
    echo "Fault ID       : ${FAULT_ID:-none}"
    echo "Firmware       : ${firmware}"
    echo "Input netlist  : ${raw_netlist}"
    echo "Cell model     : ${cell_model}"
    echo "Monitor        : ${monitor_source}"
    echo "Run directory  : ${RUN_DIR}"
    echo "Work directory : ${work_dir}"
    echo "Maximum cycles : ${maxcycles}"
    echo "VCD enabled    : ${vcd}"
    echo "Xcelium        : $(command -v xrun)"
    echo "======================================================================"

    local original_pwd="${PWD}"
    cd "${work_dir}"
    set +e
    xrun "${xrun_args[@]}"
    local xrun_status=$?
    set -e
    cd "${original_pwd}"

    local result_file="${RUN_DIR}/result.txt"
    local log_file="${RUN_DIR}/xrun.log"
    local final_status=""
    local exit_status=0

    if [[ ! -f "${log_file}" ]]; then
        final_status="ERROR"
        exit_status="${xrun_status}"
        if [[ "${exit_status}" -eq 0 ]]; then
            exit_status=4
        fi
    elif grep -q "Simulation aborted due to maximum cycle limit" "${log_file}"; then
        final_status="TIMEOUT"
        exit_status=2
    elif grep -Eqi "CRC32 FAIL|EXIT FAILURE|TEST\(S\) FAILED" "${log_file}"; then
        final_status="OUTPUT_MISMATCH"
        exit_status=2
    elif [[ "${xrun_status}" -ne 0 ]]; then
        final_status="ERROR"
        exit_status="${xrun_status}"
    elif grep -Eqi \
        "CRC32 PASS:.*vector=cbf43926.*signature=2d6352b3" \
        "${log_file}" \
        && grep -q "EXIT SUCCESS" "${log_file}"
    then
        if [[ "${RUN_KIND}" == "golden" ]]; then
            final_status="PASS"
        else
            final_status="OUTPUT_MATCH"
        fi
        grep -Ei "CRC32 PASS|EXIT SUCCESS" "${log_file}" \
            > "${RUN_DIR}/signature.txt" || true
    elif grep -q "EXIT SUCCESS" "${log_file}"; then
        if [[ "${RUN_KIND}" == "golden" ]]; then
            final_status="PASS"
        else
            final_status="OUTPUT_MATCH"
        fi
    else
        final_status="UNKNOWN"
        exit_status=3
    fi

    printf '%s\n' "${final_status}" > "${result_file}"
    printf 'xrun_exit_status=%s\nresult=%s\n' \
        "${xrun_status}" "${final_status}" > "${RUN_DIR}/result.env"

    echo
    echo "======================================================================"
    echo "Simulation result: ${final_status}"
    echo "======================================================================"
    echo "Log : ${log_file}"
    if [[ -f "${work_dir}/riscy_tb.vcd" ]]; then
        echo "VCD : ${work_dir}/riscy_tb.vcd"
    fi

    # Stage 5 normally uses compact TSV monitors and VCD=0.  The monitor trace
    # is deliberately outside work_dir, so deleting work does not delete the
    # data needed by the oracle analyzer.
    if [[ "${keep_work}" == "0" ]]; then
        if [[ -f "${work_dir}/riscy_tb.vcd" ]]; then
            echo "WARNING: KEEP_WORK=0 ignored because a VCD exists." >&2
        else
            rm -rf -- "${work_dir}"
            echo "Removed Stage-5 work directory: ${work_dir}"
        fi
    fi

    return "${exit_status}"
}
