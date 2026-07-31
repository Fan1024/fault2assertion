#!/usr/bin/env bash
set -euo pipefail

F2A_ROOT="${F2A_ROOT:-/raid/spring2026/fwu44/research/fault2assertion}"

STAGE5_TOOL="$F2A_ROOT/scripts/fault_characterization/stage5_faults.py"
VERSION_GUARD="$F2A_ROOT/scripts/fault_characterization/stage5_version_guard.py"
LOCK_VERIFY="$F2A_ROOT/scripts/fault_characterization/stage5_lock_verify.py"
GUARD_SELFTEST="$F2A_ROOT/scripts/fault_characterization/stage5_guard_selftest.py"
MINI_SELECTOR="$F2A_ROOT/scripts/fault_characterization/make_stage5_mini_selection.py"
GATE1_VALIDATE="$F2A_ROOT/scripts/fault_characterization/stage5_gate1_validate.py"
SITE_CATALOG_TOOL="$F2A_ROOT/scripts/fault_sites/site_catalog.py"
SITE_POLICY="$F2A_ROOT/platform/cv32e40p/fault_site_policy.json"

CANDIDATES="$F2A_ROOT/faults/cv32e40p/site_catalog/stage_04_candidates.json"
PARENT_SELECTION="$F2A_ROOT/faults/cv32e40p/site_catalog/stage_04_selection.json"
FULL_STAGE5_ROOT="$F2A_ROOT/faults/cv32e40p/stage5"
FULL_CAMPAIGN="$FULL_STAGE5_ROOT/stage_05_campaign.json"

LOCK_ROOT="$F2A_ROOT/runs/stage5_locks"
LOCK_ARCHIVE_ROOT="$LOCK_ROOT/archive"
RUN_STAMP="$(date +%Y%m%d_%H%M%S)"
STAGE0_ROOT="$F2A_ROOT/runs/stage5/cv32e40p/crc32/golden"
STAGE0_MONITOR="$STAGE0_ROOT/stage5_golden_monitor.sv"
STAGE0_MANIFEST="$STAGE0_ROOT/stage5_golden_monitor_manifest.json"
STAGE0_TRACE="$STAGE0_ROOT/golden_all.trace.tsv"
STAGE0_AUDIT="$STAGE0_ROOT/stage5_version_audit.json"
STAGE0_LOCK="$LOCK_ROOT/stage0_baseline_lock.json"

MINI_ROOT="$F2A_ROOT/runs/stage5_dev/mini_smoke_v1"
MINI_INPUT_ROOT="$MINI_ROOT/input"
MINI_CAMPAIGN_ROOT="$MINI_ROOT/campaign"
MINI_MONITOR_ROOT="$MINI_ROOT/monitors"
MINI_FAULT_MONITOR_ROOT="$MINI_MONITOR_ROOT/faults"
MINI_MANIFEST_ROOT="$MINI_ROOT/manifests"
MINI_FAULT_MANIFEST_ROOT="$MINI_MANIFEST_ROOT/faults"
MINI_TRACE_ROOT="$MINI_ROOT/traces"
MINI_REPORT_ROOT="$MINI_ROOT/reports"
MINI_PROVENANCE_ROOT="$MINI_ROOT/provenance"

MINI_SELECTION="$MINI_INPUT_ROOT/stage_04_mini_selection.json"
MINI_CAMPAIGN="$MINI_CAMPAIGN_ROOT/stage_05_campaign.json"
MINI_GOLDEN_MONITOR="$MINI_MONITOR_ROOT/stage5_golden_monitor.sv"
MINI_GOLDEN_MANIFEST="$MINI_MANIFEST_ROOT/stage5_golden_monitor_manifest.json"
MINI_GOLDEN_TRACE="$MINI_TRACE_ROOT/golden_all.trace.tsv"
MINI_GATE1_REPORT="$MINI_REPORT_ROOT/gate1_static_validation.json"
MINI_AUDIT="$MINI_PROVENANCE_ROOT/version_audit.json"
MINI_LOCK="$LOCK_ROOT/mini_gate1_lock.json"

log() {
  printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

require_file() {
  [[ -s "$1" ]] || fail "missing or empty required file: $1"
}

safe_reset_mini_root() {
  case "$MINI_ROOT" in
    "$F2A_ROOT"/runs/stage5_dev/*)
      rm -rf "$MINI_ROOT"
      ;;
    *)
      fail "refusing to delete unsafe MINI_ROOT: $MINI_ROOT"
      ;;
  esac
}

cd "$F2A_ROOT"

log "Verify repository identity and remote synchronization"
git fetch origin
CURRENT_BRANCH="$(git branch --show-current)"
LOCAL_HEAD="$(git rev-parse HEAD)"
REMOTE_HEAD="$(git rev-parse origin/main)"
[[ "$CURRENT_BRANCH" == "main" ]] || fail "expected branch main, got $CURRENT_BRANCH"
[[ "$LOCAL_HEAD" == "$REMOTE_HEAD" ]] || fail \
  "local HEAD differs from origin/main; local=$LOCAL_HEAD remote=$REMOTE_HEAD"
echo "Repository HEAD: $LOCAL_HEAD"
echo "Working-tree state before Stage 0/Gate 1:"
git status --short

log "Verify all required source artifacts"
for file in \
  "$STAGE5_TOOL" \
  "$VERSION_GUARD" \
  "$LOCK_VERIFY" \
  "$GUARD_SELFTEST" \
  "$MINI_SELECTOR" \
  "$GATE1_VALIDATE" \
  "$SITE_CATALOG_TOOL" \
  "$SITE_POLICY" \
  "$CANDIDATES" \
  "$PARENT_SELECTION" \
  "$FULL_CAMPAIGN"
do
  require_file "$file"
done

mkdir -p "$LOCK_ROOT" "$LOCK_ARCHIVE_ROOT" "$STAGE0_ROOT"

log "Compile Python tools before using any generated artifact"
python3 -m py_compile \
  "$STAGE5_TOOL" \
  "$VERSION_GUARD" \
  "$LOCK_VERIFY" \
  "$GUARD_SELFTEST" \
  "$MINI_SELECTOR" \
  "$GATE1_VALIDATE" \
  "$SITE_CATALOG_TOOL"

log "Run independent digest self-tests"
python3 "$GUARD_SELFTEST" \
  --stage5-tool "$STAGE5_TOOL" \
  --version-guard "$VERSION_GUARD"

log "Stage 0: regenerate the full static golden monitor with the current generator"
rm -f "$STAGE0_TRACE"
python3 "$STAGE5_TOOL" make-golden-monitor \
  --campaign "$FULL_CAMPAIGN" \
  --trace-output "$STAGE0_TRACE" \
  --output "$STAGE0_MONITOR" \
  --manifest "$STAGE0_MANIFEST" \
  --force
[[ ! -e "$STAGE0_TRACE" ]] || fail "a trace was generated during static monitor creation"

log "Stage 0: audit all 1056 full-campaign specs/patches and write baseline lock"
python3 "$VERSION_GUARD" \
  --repo-root "$F2A_ROOT" \
  --tool "$STAGE5_TOOL" \
  --campaign "$FULL_CAMPAIGN" \
  --monitor "$STAGE0_MONITOR" \
  --manifest "$STAGE0_MANIFEST" \
  --report "$STAGE0_AUDIT" \
  --write-lock "$STAGE0_LOCK"

STAGE0_LOCK_ARCHIVE="$LOCK_ARCHIVE_ROOT/stage0_baseline_${LOCAL_HEAD:0:12}_${RUN_STAMP}.json"
cp "$STAGE0_LOCK" "$STAGE0_LOCK_ARCHIVE"

log "Stage 0: immediately verify the saved baseline lock"
python3 "$LOCK_VERIFY" \
  --repo-root "$F2A_ROOT" \
  --lock "$STAGE0_LOCK"

log "Gate 1: create a clean, isolated mini-smoke workspace"
safe_reset_mini_root
mkdir -p \
  "$MINI_INPUT_ROOT" \
  "$MINI_CAMPAIGN_ROOT" \
  "$MINI_MONITOR_ROOT" \
  "$MINI_FAULT_MONITOR_ROOT" \
  "$MINI_MANIFEST_ROOT" \
  "$MINI_FAULT_MANIFEST_ROOT" \
  "$MINI_TRACE_ROOT" \
  "$MINI_REPORT_ROOT" \
  "$MINI_PROVENANCE_ROOT"

log "Gate 1: derive one safe dual-polarity site from each Stage-4 class"
python3 "$MINI_SELECTOR" \
  --candidates "$CANDIDATES" \
  --parent-selection "$PARENT_SELECTION" \
  --parent-campaign "$FULL_CAMPAIGN" \
  --output "$MINI_SELECTION" \
  --max-receivers 32 \
  --force

log "Gate 1: independently materialize the 4-site/8-fault mini campaign"
python3 "$STAGE5_TOOL" prepare \
  --candidates "$CANDIDATES" \
  --selection "$MINI_SELECTION" \
  --site-catalog-tool "$SITE_CATALOG_TOOL" \
  --policy "$SITE_POLICY" \
  --output-root "$MINI_CAMPAIGN_ROOT" \
  --force

log "Gate 1: run the Stage-5 built-in metadata validation"
python3 "$STAGE5_TOOL" validate \
  --campaign "$MINI_CAMPAIGN"

log "Gate 1: generate the mini comprehensive golden monitor"
python3 "$STAGE5_TOOL" make-golden-monitor \
  --campaign "$MINI_CAMPAIGN" \
  --trace-output "$MINI_GOLDEN_TRACE" \
  --output "$MINI_GOLDEN_MONITOR" \
  --manifest "$MINI_GOLDEN_MANIFEST" \
  --force

log "Gate 1: generate one fault monitor for each of the 8 mini faults"
while IFS=$'\t' read -r fault_id fault_spec
 do
  [[ -n "$fault_id" && -n "$fault_spec" ]] || fail "invalid campaign fault row"
  python3 "$STAGE5_TOOL" make-fault-monitor \
    --fault-json "$fault_spec" \
    --trace-output "$MINI_TRACE_ROOT/${fault_id}.trace.tsv" \
    --output "$MINI_FAULT_MONITOR_ROOT/${fault_id}.sv" \
    --manifest "$MINI_FAULT_MANIFEST_ROOT/${fault_id}.json" \
    --force
 done < <(
  python3 - "$MINI_CAMPAIGN" <<'PY'
import json
import sys
from pathlib import Path

campaign = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for record in sorted(campaign["faults"], key=lambda item: item["fault_id"]):
    print(f"{record['fault_id']}\t{record['fault_spec']}")
PY
)

log "Gate 1: run the dedicated fail-closed static validator"
python3 "$GATE1_VALIDATE" \
  --stage5-tool "$STAGE5_TOOL" \
  --candidates "$CANDIDATES" \
  --parent-selection "$PARENT_SELECTION" \
  --parent-campaign "$FULL_CAMPAIGN" \
  --mini-selection "$MINI_SELECTION" \
  --mini-campaign "$MINI_CAMPAIGN" \
  --golden-monitor "$MINI_GOLDEN_MONITOR" \
  --golden-manifest "$MINI_GOLDEN_MANIFEST" \
  --fault-monitor-dir "$MINI_FAULT_MONITOR_ROOT" \
  --fault-manifest-dir "$MINI_FAULT_MANIFEST_ROOT" \
  --trace-dir "$MINI_TRACE_ROOT" \
  --mini-root "$MINI_ROOT" \
  --report "$MINI_GATE1_REPORT" \
  --max-receivers 32

log "Gate 1: audit versions/digests for all mini artifacts and write mini lock"
monitor_args=(--monitor "$MINI_GOLDEN_MONITOR")
manifest_args=(--manifest "$MINI_GOLDEN_MANIFEST")
for fault_monitor in "$MINI_FAULT_MONITOR_ROOT"/*.sv
 do
  monitor_args+=(--monitor "$fault_monitor")
 done
for fault_manifest in "$MINI_FAULT_MANIFEST_ROOT"/*.json
 do
  manifest_args+=(--manifest "$fault_manifest")
 done

python3 "$VERSION_GUARD" \
  --repo-root "$F2A_ROOT" \
  --tool "$STAGE5_TOOL" \
  --campaign "$MINI_CAMPAIGN" \
  "${monitor_args[@]}" \
  "${manifest_args[@]}" \
  --report "$MINI_AUDIT" \
  --write-lock "$MINI_LOCK"

MINI_LOCK_ARCHIVE="$LOCK_ARCHIVE_ROOT/mini_gate1_${LOCAL_HEAD:0:12}_${RUN_STAMP}.json"
cp "$MINI_LOCK" "$MINI_LOCK_ARCHIVE"

log "Gate 1: immediately verify the mini lock"
python3 "$LOCK_VERIFY" \
  --repo-root "$F2A_ROOT" \
  --lock "$MINI_LOCK"

log "Gate 1: final no-simulation/no-permanent-netlist assertions"
[[ ! -e "$MINI_GOLDEN_TRACE" ]] || fail "golden trace exists unexpectedly"
if find "$MINI_TRACE_ROOT" -type f -name '*.tsv' -print -quit | grep -q .; then
  fail "fault trace exists unexpectedly"
fi
if find "$MINI_ROOT" -type f \
  \( -name '*.vcd' -o -name 'fault_netlist.v' -o -name '*.mapped.sim.v' \) \
  -print -quit | grep -q .; then
  fail "forbidden simulation/netlist artifact exists in mini workspace"
fi

log "Stage 0 and Gate 1 completed successfully"
echo "Stage-0 lock : $STAGE0_LOCK"
echo "Stage-0 archive: $STAGE0_LOCK_ARCHIVE"
echo "Mini lock    : $MINI_LOCK"
echo "Mini archive : $MINI_LOCK_ARCHIVE"
echo "Gate-1 report: $MINI_GATE1_REPORT"
echo "Mini campaign: $MINI_CAMPAIGN"
echo
echo "No git commit or push was performed."
echo "Current working tree:"
git status --short
