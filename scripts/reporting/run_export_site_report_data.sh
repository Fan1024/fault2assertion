#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
F2A_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

STAMP="$(date +%Y%m%d_%H%M%S)"
OUTPUT_DIR="${F2A_ROOT}/data/site_report_export/export_${STAMP}"
CAMPAIGN_ROOT="${1:-}"

ARGS=(
  --repo-root "${F2A_ROOT}"
  --output-dir "${OUTPUT_DIR}"
  --include-all-candidates
  --candidate-chunk-size 5000
)

if [[ -n "${CAMPAIGN_ROOT}" ]]; then
  ARGS+=(--campaign-root "${CAMPAIGN_ROOT}")
fi

python3 "${SCRIPT_DIR}/export_site_report_data.py" "${ARGS[@]}"

echo
echo "Export completed: ${OUTPUT_DIR}"
echo "Review first:"
echo "  ${OUTPUT_DIR}/10_integrity_report.txt"
echo "  ${OUTPUT_DIR}/00_export_manifest.json"
