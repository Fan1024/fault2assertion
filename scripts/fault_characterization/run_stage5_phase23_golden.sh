#!/usr/bin/env bash
set -euo pipefail
F2A_ROOT="${F2A_ROOT:-/raid/spring2026/fwu44/research/fault2assertion}"
FC="$F2A_ROOT/scripts/fault_characterization"
MINI_ROOT="$F2A_ROOT/runs/stage5_dev/mini_smoke_v1"
PHASE_ROOT="$F2A_ROOT/runs/stage5_dev/phase23_smoke_v1"
CAMPAIGN="$MINI_ROOT/campaign/stage_05_campaign.json"
LOCK="$F2A_ROOT/runs/stage5_locks/phase23_smoke_execution_inputs.json"
META="$PHASE_ROOT/provenance/smoke_metadata.json"
COMPILE_REPORT="$PHASE_ROOT/reports/phase23_compile_validation.json"
GOLDEN_ROOT="$PHASE_ROOT/golden"
RUN_DIR="$GOLDEN_ROOT/run"
RAW_TRACE="$PHASE_ROOT/traces/golden_all.trace.tsv"
SPLIT_DIR="$GOLDEN_ROOT/site_traces"
SPLIT_MANIFEST="$GOLDEN_ROOT/golden_split_manifest.json"
REPORT="$PHASE_ROOT/reports/phase23_golden_validation.json"
MONITOR="$PHASE_ROOT/monitors/stage5_golden_monitor.sv"
WRAPPER="$F2A_ROOT/scripts/run_xrun_stage5_golden.sh"
LOCK_TOOL="$FC/stage5_execution_input_lock.py"
VALIDATOR="$FC/stage5_phase23_golden_validate.py"
COMMON="$FC/stage5_gate_validation_common.py"
STAGE5_TOOL="$FC/stage5_faults.py"
fail(){ echo "ERROR: $*" >&2; exit 1; }
log(){ printf '\n[%s] %s\n' "$(date '+%F %T')" "$*"; }
cd "$F2A_ROOT"
[[ ! -e "$GOLDEN_ROOT" ]] || fail "golden workspace exists: $GOLDEN_ROOT"
[[ ! -e "$REPORT" ]] || fail "golden report exists: $REPORT"
python3 - "$COMPILE_REPORT" <<'PY'
import json,sys
from pathlib import Path
assert json.loads(Path(sys.argv[1]).read_text()).get("status")=="PASS"
PY
python3 "$LOCK_TOOL" verify --lock "$LOCK"
GOLDEN_NETLIST="$(python3 - "$META" <<'PY'
import json,sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text())["mapped_netlist"])
PY
)"
mkdir -p "$GOLDEN_ROOT"
log "Run strict golden regression with mode-aware mm_ram in native mode"
STAGE5_PHASE=run STAGE5_RUN_PURPOSE=NATIVE_CHARACTERIZATION \
STAGE5_TRACE_OUTPUT="$RAW_TRACE" GOLDEN_NETLIST="$GOLDEN_NETLIST" \
MAXCYCLES=2000000 VCD=0 KEEP_WORK=0 \
"$WRAPPER" "$MONITOR" "$RUN_DIR"
[[ "$(cat "$RUN_DIR/result.txt")" == "PASS" ]] || fail "strict golden verdict is not PASS"
log "Split comprehensive golden trace into per-site gzip traces"
python3 "$STAGE5_TOOL" split-golden-trace \
  --trace "$RAW_TRACE" \
  --output-dir "$SPLIT_DIR" \
  --manifest "$SPLIT_MANIFEST" \
  --delete-source --force
log "Validate golden workload, zero detector events, and split cache"
python3 "$VALIDATOR" \
  --common "$COMMON" \
  --campaign "$CAMPAIGN" \
  --compile-report "$COMPILE_REPORT" \
  --run-dir "$RUN_DIR" \
  --raw-trace "$RAW_TRACE" \
  --split-dir "$SPLIT_DIR" \
  --split-manifest "$SPLIT_MANIFEST" \
  --report "$REPORT"
printf '\nPhase-2 golden regression: PASS\nReport: %s\n' "$REPORT"
