#!/usr/bin/env bash
set -euo pipefail

F2A_ROOT="${F2A_ROOT:-/raid/spring2026/fwu44/research/fault2assertion}"
FC="$F2A_ROOT/scripts/fault_characterization"
PHASE2_ROOT="$F2A_ROOT/runs/stage5_dev/phase2_v1"
G2_ROOT="$PHASE2_ROOT/g2_native_equivalence"
G3_ROOT="$PHASE2_ROOT/g3_observe"
G4_ROOT="$PHASE2_ROOT/g4_diagnostic_quarantine"
G5_ROOT="$PHASE2_ROOT/g5_oracle"

G2_REPORT="$G2_ROOT/phase2_g2_validation.json"
G3_REPORT="$G3_ROOT/phase2_g3_validation.json"
G4_REPORT="$G4_ROOT/phase2_g4_validation.json"

G2_RUN="$G2_ROOT/runs/fault_native_runtime"
G3_RUN="$G3_ROOT/runs/fault_observe_runtime"
G4_RUN="$G4_ROOT/runs/fault_diagnostic_quarantine_runtime"

ASSERTION_POLICY="$F2A_ROOT/platform/cv32e40p/stage5_assertion_policy_v1.json"
BUILDER="$FC/stage5_phase2_g5_build.py"
VALIDATOR="$FC/stage5_phase2_g5_validate.py"

log() {
    printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
    echo "ERROR: $*" >&2
    return 1
}

require_file() {
    local path="$1"
    local label="$2"
    [[ -s "$path" ]] || fail "$label not found or empty: $path"
}

main() {
    cd "$F2A_ROOT" || return 1

    log "Validate G2, G3, and G4 prerequisites"
    for file in \
        "$G2_REPORT" \
        "$G3_REPORT" \
        "$G4_REPORT" \
        "$G2_RUN/result.json" \
        "$G3_RUN/result.json" \
        "$G4_RUN/result.json" \
        "$G4_RUN/manifest.json" \
        "$ASSERTION_POLICY" \
        "$BUILDER" \
        "$VALIDATOR"
    do
        require_file "$file" "G5 input" || return 1
    done

    readarray -t META < <(
        python3 - "$G2_REPORT" "$G3_REPORT" "$G4_REPORT" "$G4_RUN/manifest.json" <<'PY'
import json
import sys
from pathlib import Path

reports = [json.loads(Path(item).read_text(encoding="utf-8")) for item in sys.argv[1:4]]
manifest = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8"))
for name, report in zip(("G2", "G3", "G4"), reports):
    if report.get("status") != "PASS":
        raise SystemExit(f"ERROR: {name} report is not PASS")
fault_ids = {str(report.get("fault_id", "")) for report in reports}
if len(fault_ids) != 1:
    raise SystemExit(f"ERROR: G2/G3/G4 fault IDs differ: {sorted(fault_ids)}")
fault_id = next(iter(fault_ids))
if manifest.get("fault_id") != fault_id:
    raise SystemExit("ERROR: G4 manifest fault ID mismatch")
fault_json = str(manifest.get("fault_json", ""))
if not fault_json:
    raise SystemExit("ERROR: G4 manifest has no fault_json")
print(fault_id)
print(fault_json)
PY
    )

    [[ "${#META[@]}" -eq 2 ]] || {
        fail "failed to resolve G5 fault metadata"
        return 1
    }

    local fault_id="${META[0]}"
    local fault_json="${META[1]}"
    require_file "$fault_json" "fault specification" || return 1

    case "$G5_ROOT" in
        "$F2A_ROOT"/runs/stage5_dev/phase2_v1/g5_oracle) ;;
        *)
            fail "unsafe G5 root: $G5_ROOT"
            return 1
            ;;
    esac
    if [[ -e "$G5_ROOT" ]]; then
        fail "G5 workspace already exists; preserve or move it first: $G5_ROOT"
        return 1
    fi

    mkdir -p \
        "$G5_ROOT/oracles" \
        "$G5_ROOT/prompt_context" \
        "$G5_ROOT/reports"

    local oracle="$G5_ROOT/oracles/${fault_id}.json"
    local prompt="$G5_ROOT/prompt_context/${fault_id}.json"
    local validation="$G5_ROOT/reports/${fault_id}_validation.json"

    log "Build the minimal multidimensional diagnostic oracle"
    python3 "$BUILDER" \
        --g2-report "$G2_REPORT" \
        --g3-report "$G3_REPORT" \
        --g4-report "$G4_REPORT" \
        --g2-run "$G2_RUN" \
        --g3-run "$G3_RUN" \
        --g4-run "$G4_RUN" \
        --fault-json "$fault_json" \
        --assertion-policy "$ASSERTION_POLICY" \
        --oracle "$oracle" \
        --prompt-context "$prompt" || return 1

    log "Independently validate the oracle and redacted prompt context"
    python3 "$VALIDATOR" \
        --oracle "$oracle" \
        --prompt-context "$prompt" \
        --g2-report "$G2_REPORT" \
        --g3-report "$G3_REPORT" \
        --g4-report "$G4_REPORT" \
        --g2-run "$G2_RUN" \
        --g3-run "$G3_RUN" \
        --g4-run "$G4_RUN" \
        --fault-json "$fault_json" \
        --g5-root "$G5_ROOT" \
        --report "$validation" || return 1

    log "Phase2-G5 completed successfully"
    python3 - "$oracle" "$prompt" "$validation" <<'PY'
import json
import sys
from pathlib import Path

oracle_path, prompt_path, validation_path = map(Path, sys.argv[1:])
oracle = json.loads(oracle_path.read_text(encoding="utf-8"))
validation = json.loads(validation_path.read_text(encoding="utf-8"))
conclusions = oracle["derived_conclusions"]
private = oracle["private_ground_truth"]

print()
print("=" * 70)
print("Phase2-G5 PASS: minimal multidimensional diagnostic oracle")
print("=" * 70)
print(f"Fault ID                         : {oracle['fault_id']}")
print("Natural architectural outcome    : " + conclusions["natural_execution"]["architectural_outcome"])
print("OBSERVE result                    : " + conclusions["observe_execution"]["runner_status"])
print("DIAGNOSTIC_QUARANTINE result      : " + conclusions["diagnostic_quarantine_execution"]["runner_status"])
print("Validated capability              : " + conclusions["continuation_capability"]["validated_capability"])
print("Capability scope                  : " + conclusions["continuation_capability"]["scope"])
print("Exact injection signal private    : YES")
print("Exact detector cycle private      : YES")
print("Earliest local divergence claimed : NO")
print("Prompt exact labels hidden        : YES")
print("Independent validation            : " + validation["status"])
print("SVA generated                     : NO")
print(f"Oracle                            : {oracle_path}")
print(f"Prompt context                    : {prompt_path}")
print(f"Validation report                 : {validation_path}")
PY
}

main "$@"
