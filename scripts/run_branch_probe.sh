#!/usr/bin/env bash

set -euo pipefail

# One-command local diagnosis for a selected branch stuck-at fault.
#
# Usage:
#   ./scripts/run_branch_probe.sh BF0001_SA0 [tag]
#
# Useful overrides:
#   MAXCYCLES=2000000 KEEP_PROBE_VCD=1 KEEP_PROBE_WORK=1 \
#     ./scripts/run_branch_probe.sh BF0001_SA0 debug1
#
# The script performs:
#   1. Safe recovery of missing fault.json/fault.patch without deleting results.
#   2. Golden simulation with the fault-local observation probe.
#   3. Fault simulation with the same observation probe.
#   4. VCD-header validation before comparison.
#   5. Activation, injection, and local-propagation comparison.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
F2A_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

FAULT_ID="${1:-}"
TAG="${2:-$(date +%Y%m%d_%H%M%S)}"
MAXCYCLES="${MAXCYCLES:-2000000}"
KEEP_PROBE_VCD="${KEEP_PROBE_VCD:-0}"
KEEP_PROBE_WORK="${KEEP_PROBE_WORK:-0}"
EXPECTED_SELECTION_SEED="${EXPECTED_SELECTION_SEED:-20260729}"

DESIGN="cv32e40p"
WORKLOAD="crc32"
FAULT_TYPE="branchfault"
CAMPAIGN_ROOT="${F2A_ROOT}/faults/${DESIGN}/${FAULT_TYPE}"
POPULATION_JSON="${CAMPAIGN_ROOT}/population.json"
SELECTION_JSON="${CAMPAIGN_ROOT}/selection.json"
FAULT_DIR="${CAMPAIGN_ROOT}/${FAULT_ID}"
FAULT_JSON="${FAULT_DIR}/fault.json"
FAULT_PATCH="${FAULT_DIR}/fault.patch"
INJECTOR="${F2A_ROOT}/scripts/fault_injection/branch_fault.py"
COMPARE_TOOL="${F2A_ROOT}/scripts/fault_injection/compare_local_probe.py"
GOLDEN_RUNNER="${F2A_ROOT}/scripts/run_xrun_golden.sh"
FAULT_RUNNER="${F2A_ROOT}/scripts/run_xrun_fault.sh"

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

require_nonempty_file() {
    local path="$1"
    local label="$2"
    [[ -s "${path}" ]] || fail "${label} not found or empty: ${path}"
}

validate_flag() {
    local name="$1"
    local value="${!name}"
    if [[ "${value}" != "0" && "${value}" != "1" ]]; then
        fail "${name} must be 0 or 1; got ${value}"
    fi
}

validate_positive_integer() {
    local name="$1"
    local value="${!name}"
    [[ "${value}" =~ ^[1-9][0-9]*$ ]] \
        || fail "${name} must be a positive integer; got ${value}"
}

recover_fault_metadata() {
    if [[ -s "${FAULT_JSON}" && -s "${FAULT_PATCH}" ]]; then
        return 0
    fi

    echo "Recovering ${FAULT_ID} metadata without deleting existing results ..."

    require_nonempty_file "${POPULATION_JSON}" "population metadata"
    require_nonempty_file "${SELECTION_JSON}" "selection metadata"

    local regen_root
    regen_root="$(mktemp -d "${CAMPAIGN_ROOT}/.regen_${FAULT_ID}_XXXXXX")"

    cleanup_regen() {
        rm -rf -- "${regen_root}"
    }

    cp -- "${POPULATION_JSON}" "${regen_root}/population.json"
    cp -- "${SELECTION_JSON}" "${regen_root}/selection.json"

    if ! python3 "${INJECTOR}" materialize \
        --output-root "${regen_root}" \
        --fault-id "${FAULT_ID}"
    then
        cleanup_regen
        fail "failed to regenerate metadata for ${FAULT_ID}"
    fi

    if [[ ! -s "${regen_root}/${FAULT_ID}/fault.json" ]]; then
        cleanup_regen
        fail "regenerated fault JSON is missing for ${FAULT_ID}"
    fi
    if [[ ! -s "${regen_root}/${FAULT_ID}/fault.patch" ]]; then
        cleanup_regen
        fail "regenerated fault patch is missing for ${FAULT_ID}"
    fi

    mkdir -p -- "${FAULT_DIR}"
    cp -- "${regen_root}/${FAULT_ID}/fault.json" "${FAULT_JSON}"
    cp -- "${regen_root}/${FAULT_ID}/fault.patch" "${FAULT_PATCH}"

    cleanup_regen

    echo "Recovered: ${FAULT_JSON}"
    echo "Recovered: ${FAULT_PATCH}"
}

validate_campaign_and_fault() {
    python3 - \
        "${POPULATION_JSON}" \
        "${SELECTION_JSON}" \
        "${FAULT_JSON}" \
        "${FAULT_ID}" \
        "${EXPECTED_SELECTION_SEED}" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

population_path = Path(sys.argv[1])
selection_path = Path(sys.argv[2])
fault_path = Path(sys.argv[3])
expected_fault_id = sys.argv[4]
expected_seed = int(sys.argv[5])

population = json.loads(population_path.read_text(encoding="utf-8"))
selection = json.loads(selection_path.read_text(encoding="utf-8"))
fault = json.loads(fault_path.read_text(encoding="utf-8"))

selection_seed = int(selection["sampling_policy"]["random_seed"])
fault_seed = int(fault["sampling"]["random_seed"])

if selection_seed != expected_seed:
    raise SystemExit(
        f"ERROR: selection seed is {selection_seed}; expected {expected_seed}"
    )
if fault_seed != expected_seed:
    raise SystemExit(
        f"ERROR: fault seed is {fault_seed}; expected {expected_seed}"
    )
if fault["fault_id"] != expected_fault_id:
    raise SystemExit(
        f"ERROR: fault_id mismatch: {fault['fault_id']} != {expected_fault_id}"
    )

population_sha = str(population["source_netlist_sha256"])
selection_sha = str(selection["source_netlist_sha256"])
fault_sha = str(fault["source_netlist_sha256"])

if len({population_sha, selection_sha, fault_sha}) != 1:
    raise SystemExit(
        "ERROR: population.json, selection.json, and fault.json do not "
        "reference the same golden netlist SHA-256"
    )

source_netlist = Path(str(fault["source_netlist"]))
if not source_netlist.is_file():
    raise SystemExit(f"ERROR: golden source netlist not found: {source_netlist}")

actual_sha = hashlib.sha256(source_netlist.read_bytes()).hexdigest()
if actual_sha != fault_sha:
    raise SystemExit(
        "ERROR: golden netlist SHA-256 changed\n"
        f"  expected: {fault_sha}\n"
        f"  actual:   {actual_sha}"
    )

print("Campaign/fault validation: PASS")
print(f"Fault ID           : {fault['fault_id']}")
print(f"Selection seed     : {selection_seed}")
print(f"Golden netlist     : {source_netlist}")
print(f"Functional region  : {fault['site']['functional_region']}")
print(f"Source net         : {fault['site']['source_net']}")
print(
    "Receiver           : "
    f"{fault['site']['sink_instance']}/{fault['site']['sink_pin']}"
)
PY
}

check_probe_log() {
    local log_path="$1"
    local label="$2"

    require_nonempty_file "${log_path}" "${label} Xcelium log"

    grep -Fq "[F2A_PROBE] active fault=${FAULT_ID}" "${log_path}" \
        || fail "${label} probe was not elaborated; marker missing in ${log_path}"

    grep -Fq "[F2A_PROBE] requested VCD variables:" "${log_path}" \
        || fail "${label} probe did not request VCD variables: ${log_path}"

    grep -Fq "[F2A_PROBE] VCD registration completed at time" "${log_path}" \
        || fail "${label} probe did not register VCD variables: ${log_path}"

    echo "${label} probe log check: PASS"
}

check_probe_vcd() {
    local vcd_path="$1"
    local label="$2"

    require_nonempty_file "${vcd_path}" "${label} local-probe VCD"

    python3 - "${vcd_path}" "${label}" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
label = sys.argv[2]
required = {"f2a_source_original", "f2a_branch_observed"}
found: set[str] = set()

with path.open("r", encoding="utf-8", errors="replace") as stream:
    for raw_line in stream:
        line = raw_line.strip()
        if line.startswith("$var"):
            fields = line.split()
            if len(fields) >= 6:
                reference = " ".join(fields[4:-1])
                leaf = re.sub(r"\s*\[[^\]]+\]\s*$", "", reference)
                leaf = leaf.rsplit(".", 1)[-1]
                if leaf.startswith("f2a_"):
                    found.add(leaf)
        if line.startswith("$enddefinitions"):
            break

missing = sorted(required - found)
if missing:
    raise SystemExit(
        f"ERROR: {label} VCD header is missing {missing}: {path}"
    )

outputs = sorted(name for name in found if name.startswith("f2a_output_"))
if not outputs:
    raise SystemExit(
        f"ERROR: {label} VCD has no f2a_output_* receiver outputs: {path}"
    )

print(f"{label} VCD-header check: PASS")
print(f"  f2a variables: {len(found)}")
print(f"  receiver outputs: {', '.join(outputs)}")
PY
}

if [[ ! "${FAULT_ID}" =~ ^BF[0-9]{4}_SA[01]$ ]]; then
    fail "expected fault ID such as BF0001_SA0 or BF0001_SA1"
fi

if [[ ! "${TAG}" =~ ^[A-Za-z0-9_.-]+$ ]]; then
    fail "tag may contain only letters, digits, dot, underscore, and dash"
fi

validate_positive_integer MAXCYCLES
validate_positive_integer EXPECTED_SELECTION_SEED
validate_flag KEEP_PROBE_VCD
validate_flag KEEP_PROBE_WORK

require_nonempty_file "${INJECTOR}" "branch-fault injector"
require_nonempty_file "${COMPARE_TOOL}" "local-probe comparator"
require_nonempty_file "${GOLDEN_RUNNER}" "golden runner"
require_nonempty_file "${FAULT_RUNNER}" "fault runner"
require_nonempty_file "${POPULATION_JSON}" "population metadata"
require_nonempty_file "${SELECTION_JSON}" "selection metadata"

recover_fault_metadata
require_nonempty_file "${FAULT_JSON}" "fault metadata"
require_nonempty_file "${FAULT_PATCH}" "fault patch metadata"
validate_campaign_and_fault

GOLDEN_RUN_NAME="probe_${FAULT_ID}_${TAG}_golden"
FAULT_RUN_NAME="probe_${TAG}_fault"
GOLDEN_RUN_DIR="${F2A_ROOT}/golden/${DESIGN}/${WORKLOAD}/netlist/${GOLDEN_RUN_NAME}"
FAULT_RUN_DIR="${FAULT_DIR}/results/${WORKLOAD}/${FAULT_RUN_NAME}"
GOLDEN_VCD="${GOLDEN_RUN_DIR}/work/riscy_tb.vcd"
FAULT_VCD="${FAULT_RUN_DIR}/work/riscy_tb.vcd"
REPORT_JSON="${FAULT_RUN_DIR}/local_probe_compare.json"
REPORT_TEXT="${FAULT_RUN_DIR}/local_probe_compare.txt"

if [[ -e "${GOLDEN_RUN_DIR}" || -e "${FAULT_RUN_DIR}" ]]; then
    echo "ERROR: run directory already exists; choose a different tag" >&2
    echo "  golden: ${GOLDEN_RUN_DIR}" >&2
    echo "  fault : ${FAULT_RUN_DIR}" >&2
    exit 1
fi

echo
echo "======================================================================"
echo "1/3 Golden local-probe simulation"
echo "======================================================================"
LOCAL_PROBE=1 \
PROBE_FAULT_JSON="${FAULT_JSON}" \
VCD=1 \
KEEP_WORK=1 \
MAXCYCLES="${MAXCYCLES}" \
"${GOLDEN_RUNNER}" \
    "${DESIGN}" "${WORKLOAD}" netlist "${GOLDEN_RUN_NAME}"

check_probe_log "${GOLDEN_RUN_DIR}/xrun.log" "Golden"
check_probe_vcd "${GOLDEN_VCD}" "Golden"

echo
echo "======================================================================"
echo "2/3 Fault local-probe simulation"
echo "======================================================================"
set +e
LOCAL_PROBE=1 \
VCD=1 \
KEEP_WORK=1 \
MAXCYCLES="${MAXCYCLES}" \
"${FAULT_RUNNER}" \
    "${DESIGN}" "${WORKLOAD}" "${FAULT_TYPE}" "${FAULT_ID}" "${FAULT_RUN_NAME}"
FAULT_RUN_STATUS=$?
set -e

check_probe_log "${FAULT_RUN_DIR}/xrun.log" "Fault"
check_probe_vcd "${FAULT_VCD}" "Fault"

echo
echo "======================================================================"
echo "3/3 Activation and local-propagation comparison"
echo "======================================================================"
COMPARE_ARGS=(
    --golden-vcd "${GOLDEN_VCD}"
    --fault-vcd "${FAULT_VCD}"
    --fault-json "${FAULT_JSON}"
    --json-output "${REPORT_JSON}"
    --text-output "${REPORT_TEXT}"
)

if [[ "${KEEP_PROBE_VCD}" == "0" ]]; then
    COMPARE_ARGS+=(--delete-vcds)
fi

python3 "${COMPARE_TOOL}" "${COMPARE_ARGS[@]}"

if [[ "${KEEP_PROBE_WORK}" == "0" && "${KEEP_PROBE_VCD}" == "0" ]]; then
    rm -rf -- "${GOLDEN_RUN_DIR}/work" "${FAULT_RUN_DIR}/work"
    echo "Removed local Xcelium work directories after successful comparison."
fi

echo
echo "======================================================================"
echo "Local probe completed"
echo "======================================================================"
cat "${REPORT_TEXT}"
echo "Golden result     : ${GOLDEN_RUN_DIR}/result.txt"
echo "Fault result      : ${FAULT_RUN_DIR}/result.txt"
echo "Probe report      : ${REPORT_TEXT}"
echo "Probe JSON        : ${REPORT_JSON}"
echo "Fault runner exit : ${FAULT_RUN_STATUS}"
