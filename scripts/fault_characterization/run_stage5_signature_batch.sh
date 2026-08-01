#!/usr/bin/env bash
set -euo pipefail

F2A_ROOT="${F2A_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)}"
SITE_COUNT="${SITE_COUNT:-4}"
ACTION="${ACTION:-classify}"
MAXCYCLES="${MAXCYCLES:-2000000}"

BATCH_ROOT="${BATCH_ROOT:-${F2A_ROOT}/runs/stage5_campaign_v2/cv32e40p/crc32/sites_${SITE_COUNT}}"
DRIVER="${F2A_ROOT}/scripts/fault_characterization/stage5_signature_batch_driver.py"
MICRO_SCRIPT="${F2A_ROOT}/scripts/fault_characterization/run_stage5_4site_micro_batch.sh"
POLICY="${F2A_ROOT}/platform/cv32e40p/stage5_assertion_policy_v1.json"

[[ -f "${DRIVER}" ]] || { echo "ERROR: missing driver: ${DRIVER}" >&2; exit 1; }
[[ -f "${POLICY}" ]] || { echo "ERROR: missing policy: ${POLICY}" >&2; exit 1; }
[[ -d "${BATCH_ROOT}" ]] || { echo "ERROR: missing batch root: ${BATCH_ROOT}" >&2; exit 1; }

COMMON=(
  python3 "${DRIVER}"
  --root "${F2A_ROOT}"
  --batch-root "${BATCH_ROOT}"
  --policy "${POLICY}"
  --maxcycles "${MAXCYCLES}"
)

case "${ACTION}" in
  classify)
    "${COMMON[@]}" classify-existing
    ;;
  recover)
    "${COMMON[@]}" recover-existing
    ;;
  run)
    "${COMMON[@]}" run-batch
    ;;
  verify)
    "${COMMON[@]}" verify
    ;;
  validate)
    [[ "${SITE_COUNT}" == "4" ]] || {
      echo "ERROR: validate action currently targets the frozen 4-site gate only" >&2
      exit 1
    }
    F2A_ROOT="${F2A_ROOT}" ACTION=validate bash "${MICRO_SCRIPT}"
    ;;
  freeze)
    [[ "${SITE_COUNT}" == "4" ]] || {
      echo "ERROR: freeze action currently targets the frozen 4-site gate only" >&2
      exit 1
    }
    F2A_ROOT="${F2A_ROOT}" ACTION=freeze bash "${MICRO_SCRIPT}"
    ;;
  *)
    echo "ERROR: ACTION must be classify|recover|run|verify|validate|freeze" >&2
    exit 1
    ;;
esac
