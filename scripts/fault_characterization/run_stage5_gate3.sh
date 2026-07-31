#!/usr/bin/env bash
set -euo pipefail

F2A_ROOT="${F2A_ROOT:-/raid/spring2026/fwu44/research/fault2assertion}"
FC="$F2A_ROOT/scripts/fault_characterization"
MINI_ROOT="$F2A_ROOT/runs/stage5_dev/mini_smoke_v1"
MINI_CAMPAIGN="$MINI_ROOT/campaign/stage_05_campaign.json"
GOLDEN_MONITOR="$MINI_ROOT/monitors/stage5_golden_monitor.sv"
GOLDEN_MANIFEST="$MINI_ROOT/manifests/stage5_golden_monitor_manifest.json"
LOCK="$F2A_ROOT/runs/stage5_locks/mini_gate2_precompile_lock.json"
EXECUTION_INPUT_LOCK="$F2A_ROOT/runs/stage5_locks/mini_gate2_execution_inputs.json"
GATE2_REPORT="$MINI_ROOT/reports/gate2_compile_validation.json"
GATE3_ROOT="$MINI_ROOT/gate3_golden"
GOLDEN_RUN="$GATE3_ROOT/run"
SPLIT_DIR="$GATE3_ROOT/site_traces"
SPLIT_MANIFEST="$GATE3_ROOT/golden_split_manifest.json"
REPORT="$MINI_ROOT/reports/gate3_golden_validation.json"

STAGE5_TOOL="$FC/stage5_faults.py"
LOCK_VERIFY="$FC/stage5_lock_verify.py"
EXECUTION_INPUT_TOOL="$FC/stage5_execution_input_lock.py"
COMMON_VALIDATE="$FC/stage5_gate_validation_common.py"
GATE3_VALIDATE="$FC/stage5_gate3_validate.py"
GOLDEN_WRAPPER="$F2A_ROOT/scripts/run_xrun_stage5_golden.sh"

log() {
    printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

create_fresh_gate_root() {
    case "$GATE3_ROOT" in
        "$F2A_ROOT"/runs/stage5_dev/mini_smoke_v1/gate3_golden) ;;
        *) fail "unsafe Gate-3 root: $GATE3_ROOT" ;;
    esac
    if [[ -e "$GATE3_ROOT" ]]; then
        fail "Gate-3 workspace already exists. Preserve or archive it before an intentional rerun: $GATE3_ROOT"
    fi
    if [[ -e "$REPORT" ]]; then
        fail "Gate-3 report already exists. Preserve or archive it before an intentional rerun: $REPORT"
    fi
    mkdir -p "$GATE3_ROOT"
}

cd "$F2A_ROOT"

log "Verify Gate 2 passed and the precompile lock still matches"
python3 - "$GATE2_REPORT" <<'PY'
import json
import sys
from pathlib import Path
path = Path(sys.argv[1])
if not path.is_file():
    raise SystemExit(f"ERROR: Gate-2 report not found: {path}")
payload = json.loads(path.read_text(encoding="utf-8"))
if payload.get("status") != "PASS":
    raise SystemExit("ERROR: Gate-2 report is not PASS")
PY
python3 "$LOCK_VERIFY" --repo-root "$F2A_ROOT" --lock "$LOCK"
python3 "$EXECUTION_INPUT_TOOL" verify --lock "$EXECUTION_INPUT_LOCK"

readarray -t META < <(
python3 - "$MINI_CAMPAIGN" "$GOLDEN_MANIFEST" <<'PY'
import json
import sys
from pathlib import Path
campaign = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
manifest = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
print(Path(campaign["mapped_netlist"]["path"]).resolve())
print(Path(manifest["trace_output"]).resolve())
PY
)
[[ "${#META[@]}" -eq 2 ]] || fail "failed to resolve Gate-3 metadata"
GOLDEN_NETLIST="${META[0]}"
RAW_TRACE="${META[1]}"

for file in "$MINI_CAMPAIGN" "$GOLDEN_MONITOR" "$GOLDEN_NETLIST"; do
    [[ -s "$file" ]] || fail "missing Gate-3 input: $file"
done

log "Create a fresh Gate-3 workspace without deleting prior failure evidence"
create_fresh_gate_root
case "$RAW_TRACE" in
    "$MINI_ROOT"/traces/golden_all.trace.tsv) ;;
    *) fail "unexpected golden trace path: $RAW_TRACE" ;;
esac
[[ ! -e "$RAW_TRACE" ]] \
    || fail "golden raw trace already exists; preserve or remove it intentionally before rerun: $RAW_TRACE"

log "Gate 3: execute the golden netlist with strict CRC32 verdict"
STAGE5_PHASE=run \
STAGE5_TRACE_OUTPUT="$RAW_TRACE" \
GOLDEN_NETLIST="$GOLDEN_NETLIST" \
MAXCYCLES=2000000 \
VCD=0 \
KEEP_WORK=0 \
"$GOLDEN_WRAPPER" "$GOLDEN_MONITOR" "$GOLDEN_RUN"

[[ -s "$RAW_TRACE" ]] || fail "golden run did not create compact trace: $RAW_TRACE"
[[ "$(cat "$GOLDEN_RUN/result.txt")" == "PASS" ]] \
    || fail "golden strict verdict is not PASS"

log "Atomically split the four-site golden trace and delete only the raw source"
python3 "$STAGE5_TOOL" split-golden-trace \
    --trace "$RAW_TRACE" \
    --output-dir "$SPLIT_DIR" \
    --manifest "$SPLIT_MANIFEST" \
    --delete-source \
    --force

log "Fail-closed Gate-3 functional and trace-cache validation"
python3 "$GATE3_VALIDATE" \
    --common "$COMMON_VALIDATE" \
    --campaign "$MINI_CAMPAIGN" \
    --gate2-report "$GATE2_REPORT" \
    --run-dir "$GOLDEN_RUN" \
    --raw-trace "$RAW_TRACE" \
    --split-dir "$SPLIT_DIR" \
    --split-manifest "$SPLIT_MANIFEST" \
    --report "$REPORT"

log "Gate 3 completed successfully"
echo "Golden run         : $GOLDEN_RUN"
echo "Golden site traces : $SPLIT_DIR"
echo "Gate-3 report      : $REPORT"
echo "Raw all-site trace was deleted after atomic split."
