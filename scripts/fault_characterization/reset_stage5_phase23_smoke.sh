#!/usr/bin/env bash
set -euo pipefail
F2A_ROOT="${F2A_ROOT:-/raid/spring2026/fwu44/research/fault2assertion}"
PHASE_ROOT="$F2A_ROOT/runs/stage5_dev/phase23_smoke_v1"
EXEC_LOCK="$F2A_ROOT/runs/stage5_locks/phase23_smoke_execution_inputs.json"
ORACLE_LOCK="$F2A_ROOT/runs/stage5_locks/phase23_smoke_multidim_oracle.json"
case "$PHASE_ROOT" in
  "$F2A_ROOT"/runs/stage5_dev/phase23_smoke_v1) ;;
  *) echo "ERROR: unsafe Phase-2/3 root: $PHASE_ROOT" >&2; exit 1 ;;
esac
rm -rf -- "$PHASE_ROOT"
rm -f -- "$EXEC_LOCK" "$ORACLE_LOCK"
echo "Removed Phase-2/3 smoke workspace and locks."
echo "Phase-1 mini campaign/specs/patches/monitors/results were not modified."
