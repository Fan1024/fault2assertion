#!/usr/bin/env bash

# Shared Xcelium implementation for golden and fault simulations.
# This file must be sourced by run_xrun_golden.sh or run_xrun_fault.sh.

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    echo "ERROR: xrun_common.sh must be sourced, not executed."
    exit 1
fi

f2a_die() {
    echo "ERROR: $*" >&2
    return 1
}

f2a_require_file() {
    local path="$1"
    local label="$2"
    [[ -s "${path}" ]] || f2a_die "${label} not found or empty: ${path}"
}

f2a_write_command() {
    local output="$1"
    shift
    {
        printf 'xrun'
        printf ' %q' "$@"
        printf '\n'
    } > "${output}"
}

f2a_run_xrun() {
    : "${RUN_KIND:?RUN_KIND must be set to golden or fault}"
    : "${DESIGN:?DESIGN must be set}"
    : "${WORKLOAD:?WORKLOAD must be set}"
    : "${SIM_LEVEL:?SIM_LEVEL must be set}"
    : "${RUN_NAME:?RUN_NAME must be set}"
    : "${RUN_DIR:?RUN_DIR must be set}"
    : "${F2A_ROOT:?F2A_ROOT must be set}"

    local maxcycles="${MAXCYCLES:-5000000}"
    local vcd="${VCD:-0}"
    local verbose="${VERBOSE:-0}"
    local keep_work="${KEEP_WORK:-1}"

    case "${RUN_KIND}" in
        golden|fault) ;;
        *) f2a_die "unsupported RUN_KIND: ${RUN_KIND}"; return 1 ;;
    esac

    if [[ "${DESIGN}" != "cv32e40p" ]]; then
        f2a_die "unsupported design: ${DESIGN}"
        return 1
    fi

    case "${SIM_LEVEL}" in
        rtl|netlist) ;;
        *)
            f2a_die "unsupported simulation level: ${SIM_LEVEL}; use rtl or netlist"
            return 1
            ;;
    esac

    if [[ "${RUN_KIND}" == "fault" && "${SIM_LEVEL}" != "netlist" ]]; then
        f2a_die "fault simulation currently supports netlist level only"
        return 1
    fi

    local setup_script="${F2A_ROOT}/scripts/setup_env.sh"
    [[ -f "${setup_script}" ]] || {
        f2a_die "setup script not found: ${setup_script}"
        return 1
    }
    # shellcheck disable=SC1090
    source "${setup_script}"

    local f2a_home="${F2A_HOME:-${F2A_ROOT}}"
    local cv32e40p_home="${CV32E40P_HOME:-/raid/spring2026/fwu44/research/cv32e40p}"
    local rtl_dir="${cv32e40p_home}/rtl"
    local tb_dir="${cv32e40p_home}/verification/shared/tb"
    local rtl_manifest="${cv32e40p_home}/cv32e40p_manifest.flist"
    local build_dir="${f2a_home}/build/${DESIGN}/${WORKLOAD}"
    local firmware="${build_dir}/${WORKLOAD}.hex"
    local elf_file="${build_dir}/${WORKLOAD}.elf"
    local cell_model="${CV32E40P_CELL_MODEL:-}"

    command -v xrun >/dev/null 2>&1 || {
        f2a_die "xrun was not found in PATH"
        return 1
    }
    f2a_require_file "${firmware}" "firmware" || return 1
    f2a_require_file "${elf_file}" "ELF" || return 1
    [[ -d "${tb_dir}" ]] || {
        f2a_die "testbench directory not found: ${tb_dir}"
        return 1
    }
    if [[ -e "${RUN_DIR}" ]]; then
        f2a_die "run directory already exists: ${RUN_DIR}"
        return 1
    fi

    local work_dir="${RUN_DIR}/work"
    mkdir -p "${work_dir}"

    cp "${firmware}" "${work_dir}/firmware.hex"
    printf '%s\n' "${firmware}" > "${RUN_DIR}/firmware_source.txt"
    sha256sum "${firmware}" "${elf_file}" > "${RUN_DIR}/firmware.sha256"

    local tb_subsystem_source="${tb_dir}/cv32e40p_tb_subsystem.sv"
    if [[ "${SIM_LEVEL}" == "netlist" ]]; then
        tb_subsystem_source="${f2a_home}/platform/cv32e40p/tb/cv32e40p_tb_subsystem.sv"
    fi

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
    )
    local source_file
    for source_file in "${tb_sources[@]}"; do
        [[ -f "${source_file}" ]] || {
            f2a_die "testbench source not found: ${source_file}"
            return 1
        }
    done

    local design_sources_file="${work_dir}/design_sources.f"
    local raw_netlist=""
    local sim_netlist=""

    if [[ "${SIM_LEVEL}" == "rtl" ]]; then
        [[ -f "${rtl_manifest}" ]] || {
            f2a_die "RTL manifest not found: ${rtl_manifest}"
            return 1
        }
        sed "s|\${DESIGN_RTL_DIR}|${rtl_dir}|g" \
            "${rtl_manifest}" > "${design_sources_file}"
    else
        f2a_require_file "${cell_model}" "standard-cell model" || return 1

        if [[ "${RUN_KIND}" == "golden" ]]; then
            raw_netlist="${GOLDEN_NETLIST:-${CV32E40P_MAPPED_NETLIST:-}}"
            f2a_require_file "${raw_netlist}" "golden mapped netlist" || return 1
        else
            local fault_json="${FAULT_JSON:?FAULT_JSON must be set for fault simulation}"
            local injector="${f2a_home}/scripts/fault_injection/branch_fault.py"
            f2a_require_file "${fault_json}" "fault metadata" || return 1
            [[ -f "${injector}" ]] || {
                f2a_die "fault injector not found: ${injector}"
                return 1
            }

            raw_netlist="${work_dir}/fault_netlist.v"
            python3 "${injector}" apply \
                --fault-json "${fault_json}" \
                --output-netlist "${raw_netlist}"
            f2a_require_file "${raw_netlist}" "run-local fault netlist" || return 1

            cp "${fault_json}" "${RUN_DIR}/fault.json"
            if [[ -f "$(dirname -- "${fault_json}")/fault.patch" ]]; then
                cp "$(dirname -- "${fault_json}")/fault.patch" \
                    "${RUN_DIR}/fault.patch"
            fi
        fi

        local -a package_sources=(
            "${rtl_dir}/include/cv32e40p_apu_core_pkg.sv"
            "${rtl_dir}/include/cv32e40p_pkg.sv"
            "${rtl_dir}/include/cv32e40p_fpu_pkg.sv"
        )
        local package_file
        for package_file in "${package_sources[@]}"; do
            [[ -f "${package_file}" ]] || {
                f2a_die "required CV32E40P package not found: ${package_file}"
                return 1
            }
        done

        local netlist_prep_script="${f2a_home}/platform/cv32e40p/prepare_netlist.py"
        [[ -f "${netlist_prep_script}" ]] || {
            f2a_die "netlist preparation script not found: ${netlist_prep_script}"
            return 1
        }
        sim_netlist="${work_dir}/cv32e40p.mapped.sim.v"
        python3 "${netlist_prep_script}" "${raw_netlist}" "${sim_netlist}"
        f2a_require_file "${sim_netlist}" "run-local simulation netlist" || return 1

        {
            printf '%s\n' "${package_sources[@]}"
            printf '%s\n' "${cell_model}"
            printf '%s\n' "${sim_netlist}"
        } > "${design_sources_file}"

        sha256sum "${raw_netlist}" "${cell_model}" \
            > "${RUN_DIR}/netlist_sources.sha256"
        sha256sum "${sim_netlist}" \
            > "${RUN_DIR}/simulation_netlist.sha256"
        printf '%s\n' "${raw_netlist}" \
            > "${RUN_DIR}/mapped_netlist_source.txt"
        printf '%s\n' "${cell_model}" \
            > "${RUN_DIR}/cell_model_source.txt"
    fi

    git -C "${cv32e40p_home}" rev-parse HEAD \
        > "${RUN_DIR}/cv32e40p_commit.txt" 2>/dev/null || true
    git -C "${cv32e40p_home}" status --short \
        > "${RUN_DIR}/cv32e40p_status.txt" 2>/dev/null || true
    git -C "${f2a_home}" rev-parse HEAD \
        > "${RUN_DIR}/fault2assertion_commit.txt" 2>/dev/null || true
    git -C "${f2a_home}" status --short \
        > "${RUN_DIR}/fault2assertion_status.txt" 2>/dev/null || true

    cat > "${RUN_DIR}/manifest.txt" <<MANIFEST
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
golden_netlist=${GOLDEN_NETLIST:-}
fault_id=${FAULT_ID:-}
fault_json=${FAULT_JSON:-}
raw_simulation_netlist=${raw_netlist}
cell_model=${cell_model}
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
        [[ -d "${include_dir}" ]] && include_args+=("+incdir+${include_dir}")
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
    )

    if [[ "${SIM_LEVEL}" == "netlist" ]]; then
        xrun_args+=(
            +define+TETRAMAX
            -delay_mode zero
            -notimingchecks
        )
    fi
    [[ "${vcd}" == "1" ]] && xrun_args+=(+vcd)
    [[ "${verbose}" == "1" ]] && xrun_args+=(+verbose)

    f2a_write_command "${RUN_DIR}/command.txt" "${xrun_args[@]}"

    echo
    echo "======================================================================"
    echo "fault2assertion ${RUN_KIND} simulation"
    echo "======================================================================"
    echo "Design         : ${DESIGN}"
    echo "Workload       : ${WORKLOAD}"
    echo "Simulation     : ${SIM_LEVEL}"
    [[ -n "${FAULT_ID:-}" ]] && echo "Fault ID       : ${FAULT_ID}"
    echo "Firmware       : ${firmware}"
    [[ -n "${raw_netlist}" ]] && echo "Input netlist  : ${raw_netlist}"
    [[ "${SIM_LEVEL}" == "netlist" ]] && echo "Cell model     : ${cell_model}"
    echo "Run directory  : ${RUN_DIR}"
    echo "Work directory : ${work_dir}"
    echo "Maximum cycles : ${maxcycles}"
    echo "VCD enabled    : ${vcd}"
    echo "Xcelium        : $(command -v xrun)"
    echo "======================================================================"

    cd "${work_dir}"
    set +e
    xrun "${xrun_args[@]}"
    local xrun_status=$?
    set -e

    local result_file="${RUN_DIR}/result.txt"
    local log_file="${RUN_DIR}/xrun.log"
    local final_status=""
    local exit_status=0

    if grep -q "Simulation aborted due to maximum cycle limit" "${log_file}"; then
        final_status="TIMEOUT"
        exit_status=2
    elif grep -Eqi "CRC32 FAIL|EXIT FAILURE|TEST\(S\) FAILED" "${log_file}"; then
        final_status="OUTPUT_MISMATCH"
        exit_status=2
    elif [[ ${xrun_status} -ne 0 ]]; then
        final_status="ERROR"
        exit_status=${xrun_status}
    elif [[ "${WORKLOAD}" == "crc32" ]] && \
         grep -Eqi "CRC32 PASS:.*vector=cbf43926.*signature=2d6352b3" "${log_file}" && \
         grep -q "EXIT SUCCESS" "${log_file}"; then
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

    # KEEP_WORK=0 is intended for later use after VCD feature extraction is added.
    # For now, refuse to delete a work directory containing a VCD so data is not lost.
    if [[ "${keep_work}" == "0" ]]; then
        if [[ -f "${work_dir}/riscy_tb.vcd" ]]; then
            echo "WARNING: KEEP_WORK=0 ignored because VCD feature extraction is not connected yet."
        else
            rm -rf "${work_dir}"
            echo "Removed work directory: ${work_dir}"
        fi
    fi

    return "${exit_status}"
}
