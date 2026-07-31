#!/usr/bin/env bash

# Hardened Xcelium implementation for Fault2Assertion Stage 5.
# Source this file only from run_xrun_stage5_golden.sh or
# run_xrun_stage5_fault.sh.

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    echo "ERROR: xrun_stage5_common.sh must be sourced, not executed." >&2
    exit 1
fi

F2A_STAGE5_RUNNER_VERSION="5.0.0"

f2a_stage5_die() {
    echo "ERROR: $*" >&2
    return 1
}

f2a_stage5_require_file() {
    local path="$1"
    local label="$2"
    [[ -n "${path}" && -s "${path}" ]] \
        || f2a_stage5_die "${label} not found or empty: ${path:-<empty>}"
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

f2a_stage5_sha256_or_missing() {
    local path="$1"
    if [[ -f "${path}" ]]; then
        sha256sum "${path}"
    else
        printf 'MISSING  %s\n' "${path}"
    fi
}

f2a_stage5_write_retention_json() {
    local output="$1"
    local status="$2"
    local work_retained="$3"
    local reason="$4"
    local bundle_created="$5"
    python3 - "${output}" "${status}" "${work_retained}" "${reason}" "${bundle_created}" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "schema_version": "1.0",
    "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    "status": sys.argv[2],
    "work_directory_retained": sys.argv[3] == "1",
    "retention_reason": sys.argv[4],
    "reproduction_bundle_created": sys.argv[5] == "1",
}
path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY
}

f2a_stage5_manifest_to_json() {
    local source="$1"
    local output="$2"
    python3 - "${source}" "${output}" <<'PY'
import json
import sys
from pathlib import Path

source = Path(sys.argv[1])
output = Path(sys.argv[2])
payload = {}
for raw in source.read_text(encoding="utf-8").splitlines():
    if raw and "=" in raw:
        key, value = raw.split("=", 1)
        payload[key] = value
output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY
}

f2a_stage5_finalize_preflight_failure() {
    local run_dir="$1"
    local work_dir="$2"
    local phase="$3"
    local run_kind="$4"
    local trace_output="$5"
    local verdict_tool="$6"
    local bundle_tool="$7"
    local failure_reason="$8"
    local synthetic_status="${9:-90}"
    local run_purpose="${10:-${STAGE5_RUN_PURPOSE:-NATIVE_CHARACTERIZATION}}"
    local mm_ram_profile="${11:-${STAGE5_MM_RAM_PROFILE:-native}}"
    local log_file="${run_dir}/xrun.log"

    {
        echo "F2A_RUNNER_ERROR: ${failure_reason}"
        if [[ -s "${run_dir}/preflight_failure.txt" ]]; then
            cat "${run_dir}/preflight_failure.txt"
        fi
    } > "${log_file}"

    if [[ ! -s "${run_dir}/command.txt" ]]; then
        printf '%s\n' \
            "PRE_XRUN_FAILURE: xrun was not invoked; rerun the recorded wrapper command after fixing ${failure_reason}." \
            > "${run_dir}/command.txt"
    fi
    if [[ ! -s "${run_dir}/wrapper_command.txt" ]]; then
        printf '%s\n' "${WRAPPER_COMMAND:-not_recorded}" \
            > "${run_dir}/wrapper_command.txt"
    fi
    if [[ ! -s "${run_dir}/manifest.txt" ]]; then
        cat > "${run_dir}/manifest.txt" <<MANIFEST
schema_version=1.0
runner_version=${F2A_STAGE5_RUNNER_VERSION}
stage=5
phase=${phase}
run_kind=${run_kind}
run_purpose=${run_purpose}
mm_ram_profile=${mm_ram_profile}
run_directory=${run_dir}
work_directory=${work_dir}
trace_output=${trace_output}
preflight_failure=${failure_reason}
MANIFEST
    fi
    f2a_stage5_manifest_to_json \
        "${run_dir}/manifest.txt" \
        "${run_dir}/manifest.json"

    python3 "${verdict_tool}" \
        --phase "${phase}" \
        --run-kind "${run_kind}" \
        --run-purpose "${run_purpose}" \
        --xrun-status "${synthetic_status}" \
        --log "${log_file}" \
        --result-json "${run_dir}/result.json" \
        --result-text "${run_dir}/result.txt" \
        --result-env "${run_dir}/result.env" >/dev/null || return 5

    # shellcheck disable=SC1090
    source "${run_dir}/result.env"
    python3 "${bundle_tool}" \
        --run-dir "${run_dir}" \
        --status "${result}" \
        --trace "${trace_output}" \
        --output "${run_dir}/reproduction_bundle.tar.gz" \
        --manifest "${run_dir}/reproduction_bundle_manifest.json" \
        || return 5

    f2a_stage5_write_retention_json \
        "${run_dir}/retention.json" \
        "${result}" \
        1 \
        "preflight_failure_retention" \
        1

    echo "ERROR: Stage-5 preflight failed: ${failure_reason}" >&2
    echo "Retained work directory: ${work_dir}" >&2
    echo "Reproduction bundle: ${run_dir}/reproduction_bundle.tar.gz" >&2
    return "${recommended_exit_code}"
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
    : "${STAGE5_TRACE_OUTPUT:?STAGE5_TRACE_OUTPUT must be the monitor trace path}"

    local phase="${STAGE5_PHASE:-run}"
    local run_purpose="${STAGE5_RUN_PURPOSE:-}"
    if [[ -z "${run_purpose}" ]]; then
        if [[ "${phase}" == "compile" ]]; then
            run_purpose="COMPILE_CHECK"
        else
            run_purpose="NATIVE_CHARACTERIZATION"
        fi
    fi

    local mm_ram_profile="${STAGE5_MM_RAM_PROFILE:-}"
    if [[ -z "${mm_ram_profile}" ]]; then
        case "${run_purpose}" in
            COMPILE_CHECK|NATIVE_CHARACTERIZATION)
                mm_ram_profile="native"
                ;;
            DIAGNOSTIC_OBSERVE|DIAGNOSTIC_QUARANTINE)
                mm_ram_profile="diagnostic"
                ;;
        esac
    fi

    local assertion_mode="native"
    case "${run_purpose}" in
        COMPILE_CHECK|NATIVE_CHARACTERIZATION)
            assertion_mode="native"
            ;;
        DIAGNOSTIC_OBSERVE)
            assertion_mode="observe"
            ;;
        DIAGNOSTIC_QUARANTINE)
            assertion_mode="diagnostic_quarantine"
            ;;
    esac

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
    case "${phase}" in
        compile|run) ;;
        *)
            f2a_stage5_die "STAGE5_PHASE must be compile or run; got ${phase}"
            return 1
            ;;
    esac
    case "${run_purpose}" in
        COMPILE_CHECK|NATIVE_CHARACTERIZATION|DIAGNOSTIC_OBSERVE|DIAGNOSTIC_QUARANTINE) ;;
        *)
            f2a_stage5_die "unsupported STAGE5_RUN_PURPOSE: ${run_purpose}"
            return 1
            ;;
    esac
    case "${mm_ram_profile}" in
        native|diagnostic) ;;
        *)
            f2a_stage5_die \
                "STAGE5_MM_RAM_PROFILE must be native or diagnostic; got ${mm_ram_profile}"
            return 1
            ;;
    esac

    if [[ "${phase}" == "compile" && "${run_purpose}" != "COMPILE_CHECK" ]]; then
        f2a_stage5_die "compile phase requires STAGE5_RUN_PURPOSE=COMPILE_CHECK"
        return 1
    fi
    if [[ "${run_purpose}" == "NATIVE_CHARACTERIZATION" && "${mm_ram_profile}" != "native" ]]; then
        f2a_stage5_die "native characterization must use STAGE5_MM_RAM_PROFILE=native"
        return 1
    fi
    if [[ "${run_purpose}" == "DIAGNOSTIC_OBSERVE" && "${mm_ram_profile}" != "diagnostic" ]]; then
        f2a_stage5_die "diagnostic observe must use STAGE5_MM_RAM_PROFILE=diagnostic"
        return 1
    fi
    if [[ "${run_purpose}" == "DIAGNOSTIC_QUARANTINE" && "${mm_ram_profile}" != "diagnostic" ]]; then
        f2a_stage5_die "diagnostic quarantine must use STAGE5_MM_RAM_PROFILE=diagnostic"
        return 1
    fi
    if [[ "${RUN_KIND}" == "golden" && "${mm_ram_profile}" != "native" ]]; then
        f2a_stage5_die "golden execution supports the native mm_ram profile only"
        return 1
    fi

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
    if [[ "${STAGE5_TRACE_OUTPUT}" != /* ]]; then
        f2a_stage5_die \
            "STAGE5_TRACE_OUTPUT must be absolute: ${STAGE5_TRACE_OUTPUT}"
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
    if [[ -e "${STAGE5_TRACE_OUTPUT}" ]]; then
        f2a_stage5_die \
            "refusing to overwrite an existing Stage-5 trace: ${STAGE5_TRACE_OUTPUT}"
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
    local trace_output
    trace_output="$(readlink -m -- "${STAGE5_TRACE_OUTPUT}")"

    local verdict_tool="${f2a_home}/scripts/fault_characterization/stage5_verdict.py"
    local bundle_tool="${f2a_home}/scripts/fault_characterization/stage5_reproduction_bundle.py"

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
    f2a_stage5_require_file "${verdict_tool}" "Stage-5 verdict tool" || return 1
    f2a_stage5_require_file "${bundle_tool}" "Stage-5 reproduction-bundle tool" \
        || return 1

    python3 - "${monitor_source}" "${trace_output}" <<'PY'
import sys
from pathlib import Path

monitor = Path(sys.argv[1])
trace = str(Path(sys.argv[2]).resolve())
text = monitor.read_text(encoding="utf-8", errors="strict")
if text.count(trace) != 1:
    raise SystemExit(
        "ERROR: monitor must contain the exact STAGE5_TRACE_OUTPUT path once\n"
        f"  monitor: {monitor}\n"
        f"  trace:   {trace}\n"
        f"  count:   {text.count(trace)}"
    )
PY

    [[ -d "${tb_dir}" ]] || {
        f2a_stage5_die "testbench directory not found: ${tb_dir}"
        return 1
    }

    local tb_subsystem_source="${f2a_home}/platform/cv32e40p/tb/cv32e40p_tb_subsystem.sv"
    local original_mm_ram_source="${tb_dir}/mm_ram.sv"
    f2a_stage5_require_file "${original_mm_ram_source}" "original mm_ram source" || return 1

    mkdir -p "${RUN_DIR}" "$(dirname -- "${trace_output}")"
    local work_dir="${RUN_DIR}/work"
    mkdir -p "${work_dir}"

    local selected_mm_ram_source="${original_mm_ram_source}"
    local prepared_mm_ram_source=""
    local assertion_policy=""
    local assertion_prep=""
    local assertion_prep_impl=""
    local mm_ram_preparation_report=""
    local mm_ram_ownership_report=""

    if [[ "${mm_ram_profile}" == "diagnostic" ]]; then
        assertion_policy="${f2a_home}/platform/cv32e40p/stage5_assertion_policy_v1.json"
        assertion_prep="${f2a_home}/platform/cv32e40p/prepare_stage5_mm_ram.py"
        assertion_prep_impl="${f2a_home}/platform/cv32e40p/prepare_stage5_mm_ram_impl.py"
        prepared_mm_ram_source="${work_dir}/mm_ram.stage5.sv"
        mm_ram_preparation_report="${RUN_DIR}/mm_ram_preparation.json"
        mm_ram_ownership_report="${RUN_DIR}/mm_ram_ownership.json"

        f2a_stage5_require_file "${assertion_policy}" "Stage-5 assertion policy" || return 1
        f2a_stage5_require_file "${assertion_prep}" "Stage-5 mm_ram wrapper" || return 1
        f2a_stage5_require_file "${assertion_prep_impl}" "Stage-5 mm_ram implementation" || return 1

        set +e
        python3 "${assertion_prep}" \
            "${original_mm_ram_source}" \
            "${prepared_mm_ram_source}" \
            --policy "${assertion_policy}" \
            --report "${mm_ram_preparation_report}" \
            > "${RUN_DIR}/mm_ram_preparation.log" 2>&1
        local mm_ram_prepare_status=$?
        set -e
        cat "${RUN_DIR}/mm_ram_preparation.log"

        if [[ "${mm_ram_prepare_status}" -ne 0 \
              || ! -s "${prepared_mm_ram_source}" \
              || ! -s "${mm_ram_preparation_report}" \
              || ! -s "${mm_ram_ownership_report}" ]]; then
            cp -- "${RUN_DIR}/mm_ram_preparation.log" "${RUN_DIR}/preflight_failure.txt"
            f2a_stage5_finalize_preflight_failure \
                "${RUN_DIR}" "${work_dir}" "${phase}" "${RUN_KIND}" \
                "${trace_output}" "${verdict_tool}" "${bundle_tool}" \
                "stage5_mm_ram_preparation_failed" 93 \
                "${run_purpose}" "${mm_ram_profile}"
            return $?
        fi

        selected_mm_ram_source="${prepared_mm_ram_source}"
        sha256sum \
            "${original_mm_ram_source}" \
            "${prepared_mm_ram_source}" \
            "${assertion_policy}" \
            "${assertion_prep}" \
            "${assertion_prep_impl}" \
            > "${RUN_DIR}/stage5_assertion_adapter.sha256"
    else
        sha256sum "${original_mm_ram_source}" \
            > "${RUN_DIR}/stage5_assertion_adapter.sha256"
    fi

    local assertion_event_output="${RUN_DIR}/assertion_events.tsv"
    local assertion_event_origin="none_compile_only"
    if [[ "${phase}" == "run" && "${mm_ram_profile}" == "native" ]]; then
        printf 'H\tF2A_ASSERT_EVENTS\t1\tnative\n' > "${assertion_event_output}"
        assertion_event_origin="runner_native_header_only"
    elif [[ "${phase}" == "run" ]]; then
        assertion_event_origin="diagnostic_overlay"
    fi

    local -a tb_sources=(
        "${tb_dir}/include/perturbation_pkg.sv"
        "${tb_dir}/amo_shim.sv"
        "${tb_dir}/cv32e40p_random_interrupt_generator.sv"
        "${tb_dir}/dp_ram.sv"
        "${tb_dir}/riscv_gnt_stall.sv"
        "${tb_dir}/riscv_rvalid_stall.sv"
        "${selected_mm_ram_source}"
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

    cp -- "${firmware}" "${work_dir}/firmware.hex"
    cp -- "${monitor_source}" "${RUN_DIR}/stage5_monitor.sv"
    printf '%s\n' "${firmware}" > "${RUN_DIR}/firmware_source.txt"
    sha256sum "${firmware}" "${elf_file}" > "${RUN_DIR}/firmware.sha256"
    sha256sum "${monitor_source}" > "${RUN_DIR}/stage5_monitor.sha256"
    printf '%s\n' "${cell_model}" > "${RUN_DIR}/cell_model_source.txt"

    {
        echo "runner_version=${F2A_STAGE5_RUNNER_VERSION}"
        echo "hostname=$(hostname -f 2>/dev/null || hostname)"
        echo "uname=$(uname -a)"
        echo "date=$(date --iso-8601=seconds)"
        echo "xrun=$(command -v xrun)"
        echo "python3=$(command -v python3)"
        echo "F2A_ROOT=${F2A_ROOT}"
        echo "F2A_HOME=${f2a_home}"
        echo "CV32E40P_HOME=${cv32e40p_home}"
        echo "CV32E40P_CELL_MODEL=${cell_model}"
        echo "STAGE5_PHASE=${phase}"
        echo "STAGE5_RUN_PURPOSE=${run_purpose}"
        echo "STAGE5_MM_RAM_PROFILE=${mm_ram_profile}"
        echo "STAGE5_ASSERTION_MODE=${assertion_mode}"
        echo "STAGE5_ASSERTION_EVENT_OUTPUT=${assertion_event_output}"
        echo "STAGE5_ASSERTION_EVENT_ORIGIN=${assertion_event_origin}"
        echo "STAGE5_ASSERTION_POLICY=${assertion_policy}"
        echo "STAGE5_TRACE_OUTPUT=${trace_output}"
        echo "MAXCYCLES=${maxcycles}"
        echo "VCD=${vcd}"
        echo "VERBOSE=${verbose}"
        echo "KEEP_WORK=${keep_work}"
    } > "${RUN_DIR}/environment.txt"

    xrun -version > "${RUN_DIR}/xrun_version.txt" 2>&1 || true
    printf '%s\n' "${WRAPPER_COMMAND:-not_recorded}" \
        > "${RUN_DIR}/wrapper_command.txt"

    local raw_netlist=""
    if [[ "${RUN_KIND}" == "golden" ]]; then
        raw_netlist="${GOLDEN_NETLIST:-${CV32E40P_MAPPED_NETLIST:-}}"
        f2a_stage5_require_file "${raw_netlist}" "golden mapped netlist" || return 1
        raw_netlist="$(readlink -f -- "${raw_netlist}")"
    else
        local fault_json="${FAULT_JSON:?FAULT_JSON must be set for a fault run}"
        local fault_applier="${STAGE5_FAULT_APPLIER:-${f2a_home}/scripts/fault_characterization/stage5_faults.py}"

        f2a_stage5_require_file "${fault_json}" "Stage-5 fault spec" || return 1
        f2a_stage5_require_file "${fault_applier}" "Stage-5 fault materializer" \
            || return 1

        raw_netlist="${work_dir}/fault_netlist.v"
        cp -- "${fault_json}" "${RUN_DIR}/fault.json"
        set +e
        python3 "${fault_applier}" apply \
            --fault-json "${fault_json}" \
            --output-netlist "${raw_netlist}" \
            > "${RUN_DIR}/materialize.log" 2>&1
        local materialize_status=$?
        set -e
        cat "${RUN_DIR}/materialize.log"
        if [[ "${materialize_status}" -ne 0 || ! -s "${raw_netlist}" ]]; then
            cp -- "${RUN_DIR}/materialize.log" "${RUN_DIR}/preflight_failure.txt"
            f2a_stage5_finalize_preflight_failure \
                "${RUN_DIR}" "${work_dir}" "${phase}" "${RUN_KIND}" \
                "${trace_output}" "${verdict_tool}" "${bundle_tool}" \
                "fault_materialization_failed" 91 \
                "${run_purpose}" "${mm_ram_profile}"
            return $?
        fi
    fi

    printf '%s\n' "${raw_netlist}" > "${RUN_DIR}/mapped_netlist_source.txt"
    f2a_stage5_sha256_or_missing "${raw_netlist}" \
        > "${RUN_DIR}/netlist_sources.sha256"
    sha256sum "${cell_model}" >> "${RUN_DIR}/netlist_sources.sha256"

    local sim_netlist="${work_dir}/cv32e40p.mapped.sim.v"
    set +e
    python3 "${netlist_prep_script}" "${raw_netlist}" "${sim_netlist}" \
        > "${RUN_DIR}/prepare_netlist.log" 2>&1
    local prepare_status=$?
    set -e
    cat "${RUN_DIR}/prepare_netlist.log"
    if [[ "${prepare_status}" -ne 0 || ! -s "${sim_netlist}" ]]; then
        cp -- "${RUN_DIR}/prepare_netlist.log" "${RUN_DIR}/preflight_failure.txt"
        f2a_stage5_finalize_preflight_failure \
            "${RUN_DIR}" "${work_dir}" "${phase}" "${RUN_KIND}" \
            "${trace_output}" "${verdict_tool}" "${bundle_tool}" \
            "simulation_netlist_preparation_failed" 92 \
            "${run_purpose}" "${mm_ram_profile}"
        return $?
    fi

    local design_sources_file="${work_dir}/design_sources.f"
    {
        printf '%s\n' "${package_sources[@]}"
        printf '%s\n' "${cell_model}"
        printf '%s\n' "${sim_netlist}"
    } > "${design_sources_file}"

    sha256sum "${sim_netlist}" > "${RUN_DIR}/simulation_netlist.sha256"

    git -C "${cv32e40p_home}" rev-parse HEAD \
        > "${RUN_DIR}/cv32e40p_commit.txt" 2>/dev/null || true
    git -C "${f2a_home}" rev-parse HEAD \
        > "${RUN_DIR}/fault2assertion_commit.txt" 2>/dev/null || true

    cat > "${RUN_DIR}/manifest.txt" <<MANIFEST
schema_version=1.0
runner_version=${F2A_STAGE5_RUNNER_VERSION}
stage=5
phase=${phase}
run_kind=${RUN_KIND}
run_purpose=${run_purpose}
mm_ram_profile=${mm_ram_profile}
assertion_mode=${assertion_mode}
assertion_event_output=${assertion_event_output}
assertion_event_origin=${assertion_event_origin}
assertion_policy=${assertion_policy}
original_mm_ram_source=${original_mm_ram_source}
prepared_mm_ram_source=${prepared_mm_ram_source}
selected_mm_ram_source=${selected_mm_ram_source}
mm_ram_preparation_report=${mm_ram_preparation_report}
mm_ram_ownership_report=${mm_ram_ownership_report}
design=${DESIGN}
workload=${WORKLOAD}
simulation_level=${SIM_LEVEL}
run_name=${RUN_NAME}
run_time=$(date --iso-8601=seconds)
run_directory=${RUN_DIR}
work_directory=${work_dir}
trace_output=${trace_output}
firmware=${firmware}
elf=${elf_file}
cv32e40p_home=${cv32e40p_home}
raw_simulation_netlist=${raw_netlist}
prepared_simulation_netlist=${sim_netlist}
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
expected_crc32_last=0x5650AC83
MANIFEST

    f2a_stage5_manifest_to_json \
        "${RUN_DIR}/manifest.txt" \
        "${RUN_DIR}/manifest.json"

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

    if [[ "${mm_ram_profile}" == "diagnostic" ]]; then
        xrun_args+=(
            "+f2a_assert_mode=${assertion_mode}"
            "+f2a_assert_event_file=${assertion_event_output}"
        )
    fi

    if [[ "${phase}" == "compile" ]]; then
        xrun_args+=(-elaborate)
    fi
    if [[ "${vcd}" == "1" ]]; then
        xrun_args+=(+vcd)
    fi
    if [[ "${verbose}" == "1" ]]; then
        xrun_args+=(+verbose)
    fi

    f2a_stage5_write_command "${RUN_DIR}/command.txt" "${xrun_args[@]}"

    echo
    echo "======================================================================"
    echo "Fault2Assertion Stage-5 ${RUN_KIND} ${phase}"
    echo "======================================================================"
    echo "Runner version : ${F2A_STAGE5_RUNNER_VERSION}"
    echo "Design         : ${DESIGN}"
    echo "Workload       : ${WORKLOAD}"
    echo "Simulation     : ${SIM_LEVEL}"
    echo "Phase          : ${phase}"
    echo "Run purpose    : ${run_purpose}"
    echo "mm_ram profile : ${mm_ram_profile}"
    echo "Assertion mode : ${assertion_mode}"
    echo "mm_ram source  : ${selected_mm_ram_source}"
    echo "Fault ID       : ${FAULT_ID:-none}"
    echo "Firmware       : ${firmware}"
    echo "Input netlist  : ${raw_netlist}"
    echo "Cell model     : ${cell_model}"
    echo "Monitor        : ${monitor_source}"
    echo "Trace          : ${trace_output}"
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

    local log_file="${RUN_DIR}/xrun.log"

    if [[ "${phase}" == "compile" && -e "${trace_output}" ]]; then
        echo "F2A_RUNNER_ERROR: compile-only phase unexpectedly created a trace" \
            >> "${log_file}"
        xrun_status=97
    fi
    if [[ "${phase}" == "compile" && -e "${assertion_event_output}" ]]; then
        echo "F2A_RUNNER_ERROR: compile-only phase unexpectedly created assertion events" \
            >> "${log_file}"
        xrun_status=96
    fi
    if [[ "${phase}" == "run" && ! -s "${trace_output}" ]]; then
        echo "F2A_RUNNER_ERROR: run phase did not create a non-empty compact trace" \
            >> "${log_file}"
        xrun_status=98
    fi
    if [[ "${phase}" == "run" && ! -s "${assertion_event_output}" ]]; then
        echo "F2A_RUNNER_ERROR: run phase did not create a non-empty assertion event file" \
            >> "${log_file}"
        xrun_status=99
    fi

    local result_file="${RUN_DIR}/result.txt"
    local result_json="${RUN_DIR}/result.json"
    local result_env="${RUN_DIR}/result.env"
    python3 "${verdict_tool}" \
        --phase "${phase}" \
        --run-kind "${RUN_KIND}" \
        --run-purpose "${run_purpose}" \
        --xrun-status "${xrun_status}" \
        --log "${log_file}" \
        --assert-events "${assertion_event_output}" \
        --result-json "${result_json}" \
        --result-text "${result_file}" \
        --result-env "${result_env}" >/dev/null || return 1

    # shellcheck disable=SC1090
    source "${result_env}"

    if [[ ! -f "${log_file}" ]]; then
        printf '%s\n' 'Original xrun.log was missing after the xrun invocation.' \
            > "${log_file}"
    fi

    local final_status="${result}"
    local exit_status="${recommended_exit_code}"

    if [[ "${final_status}" == "PASS" \
          || "${final_status}" == "OUTPUT_MATCH" \
          || "${final_status}" == "DIAGNOSTIC_OUTPUT_MATCH" ]]; then
        grep -Ei \
            "CRC32 PASS:.*vector=(0x)?cbf43926.*signature=(0x)?2d6352b3.*last=(0x)?5650ac83|EXIT SUCCESS" \
            "${log_file}" > "${RUN_DIR}/signature.txt" || true
    fi

    local bundle_created=0
    case "${final_status}" in
        COMPILE_ERROR|GOLDEN_INVALID|ERROR|UNKNOWN|TIMEOUT|OUTPUT_MISMATCH|EXISTING_ASSERTION_DETECTED|DIAGNOSTIC_TIMEOUT|DIAGNOSTIC_OUTPUT_MISMATCH)
            set +e
            python3 "${bundle_tool}" \
                --run-dir "${RUN_DIR}" \
                --status "${final_status}" \
                --trace "${trace_output}" \
                --output "${RUN_DIR}/reproduction_bundle.tar.gz" \
                --manifest "${RUN_DIR}/reproduction_bundle_manifest.json"
            local bundle_status=$?
            set -e
            if [[ "${bundle_status}" -ne 0 ]]; then
                echo "ERROR: failed to create Stage-5 reproduction bundle" >&2
                return 5
            fi
            bundle_created=1
            ;;
    esac

    echo
    echo "======================================================================"
    echo "Stage-5 result: ${final_status}"
    echo "======================================================================"
    echo "Reason : ${reason}"
    echo "Log    : ${log_file}"
    echo "Result : ${result_json}"
    if [[ -f "${trace_output}" ]]; then
        echo "Trace  : ${trace_output}"
    fi
    if [[ -f "${assertion_event_output}" ]]; then
        echo "Events : ${assertion_event_output}"
    fi
    if [[ "${bundle_created}" == "1" ]]; then
        echo "Bundle : ${RUN_DIR}/reproduction_bundle.tar.gz"
    fi

    local retain_work=0
    local retention_reason="successful_or_scientifically_valid_run_cleanup"
    if [[ "${keep_work}" == "1" ]]; then
        retain_work=1
        retention_reason="KEEP_WORK_requested"
    elif [[ -f "${work_dir}/riscy_tb.vcd" || "${vcd}" == "1" ]]; then
        retain_work=1
        retention_reason="VCD_present_or_requested"
    else
        case "${final_status}" in
            COMPILE_ERROR|GOLDEN_INVALID|ERROR|UNKNOWN|TIMEOUT|EXISTING_ASSERTION_DETECTED|DIAGNOSTIC_TIMEOUT)
                retain_work=1
                if [[ "${final_status}" == "EXISTING_ASSERTION_DETECTED" ]]; then
                    retention_reason="native_assertion_termination_retained_for_raw_fact_and_oracle_analysis"
                else
                    retention_reason="infrastructure_invalid_or_censored_execution_retention"
                fi
                ;;
            *)
                retain_work=0
                ;;
        esac
    fi

    if [[ "${retain_work}" == "0" ]]; then
        rm -rf -- "${work_dir}"
        echo "Removed Stage-5 work directory: ${work_dir}"
    else
        echo "Retained Stage-5 work directory: ${work_dir}"
    fi

    f2a_stage5_write_retention_json \
        "${RUN_DIR}/retention.json" \
        "${final_status}" \
        "${retain_work}" \
        "${retention_reason}" \
        "${bundle_created}"

    return "${exit_status}"
}
