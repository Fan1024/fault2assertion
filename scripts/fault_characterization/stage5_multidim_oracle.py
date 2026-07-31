#!/usr/bin/env python3
"""Build a Stage-5 multidimensional fault oracle from three execution modes.

The oracle keeps immutable observations separate from derived dimensions.  The
native run defines the natural completion boundary.  Observe and quarantine
runs are counterfactual diagnostic continuations and may add propagation facts,
but they never replace the native architectural outcome.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

PROGRAM_VERSION = "1.0.0"
SCHEMA_VERSION = "3.0"
ORACLE_STAGE = "stage_05_multidimensional_fault_oracle_v3"
FAULT_STAGE = "stage_05_fault_materialization"
FAULT_RE = re.compile(r"^TF\d{6}_SA[01]$")
SELECTION_RE = re.compile(r"^TS\d{6}$")


class OracleError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OracleError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise OracleError(f"invalid {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise OracleError(f"{label} must be an object")
    return value


def atomic_write(path: Path, text: str, force: bool) -> None:
    path = path.resolve()
    if path.exists() and not force:
        raise OracleError(f"refusing to overwrite without --force: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def open_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def normalize_scope(scope: str) -> str:
    value = scope.strip()
    if "." in value:
        value = value.rsplit(".", 1)[0]
    return value


@dataclass
class Series:
    rows: list[tuple[int, int, tuple[str, ...]]] = field(default_factory=list)

    def add(self, cycle: int, time_value: int, values: Sequence[str]) -> None:
        normalized = tuple(str(v).strip().lower() for v in values)
        if self.rows and self.rows[-1][2] == normalized:
            return
        self.rows.append((cycle, time_value, normalized))

    def known_values(self, column: int) -> set[str]:
        result: set[str] = set()
        for _, _, values in self.rows:
            if column >= len(values):
                continue
            value = values[column]
            if value and not any(c in value for c in "xz"):
                result.add(value)
        return result

    def last_cycle(self) -> int | None:
        return self.rows[-1][0] if self.rows else None


@dataclass
class Trace:
    valid: bool
    errors: list[str]
    path: Path
    sha256: str | None
    header_count: int
    sample_count: int
    activity_count: int
    series: dict[str, Series]
    activity: dict[str, dict[str, bool]]


def parse_golden(path: Path, selection_id: str) -> Trace:
    errors: list[str] = []
    series: dict[str, Series] = defaultdict(Series)
    activity: dict[str, dict[str, bool]] = {}
    if not path.is_file() or path.stat().st_size == 0:
        return Trace(False, [f"golden trace missing or empty: {path}"], path, None, 0, 0, 0, {}, {})
    header = samples = activities = 0

    def mark(scope: str, value: str) -> None:
        item = activity.setdefault(scope, {"seen0": False, "seen1": False})
        if value == "0":
            item["seen0"] = True
        elif value == "1":
            item["seen1"] = True

    with open_text(path) as stream:
        for line_number, raw in enumerate(stream, start=1):
            fields = raw.rstrip("\n").split("\t")
            if not fields or fields == [""]:
                continue
            try:
                if fields[0] == "H":
                    header += 1
                    if len(fields) < 2 or fields[1] != "GOLDEN":
                        errors.append(f"line {line_number}: invalid golden header {fields!r}")
                elif fields[0] == "G" and len(fields) == 7:
                    if fields[1] != selection_id:
                        errors.append(f"line {line_number}: wrong selection ID")
                        continue
                    cycle, time_value = int(fields[2]), int(fields[3])
                    scope = normalize_scope(fields[4])
                    source, receivers = fields[5].lower(), fields[6].lower()
                    series[scope].add(cycle, time_value, (source, receivers))
                    mark(scope, source)
                    samples += 1
                elif fields[0] == "GA" and len(fields) == 7:
                    if fields[1] != selection_id or fields[5] != "SRC":
                        errors.append(f"line {line_number}: invalid golden activity row")
                        continue
                    mark(normalize_scope(fields[4]), fields[6].lower())
                    activities += 1
                elif fields[0] == "GS" and len(fields) == 5:
                    if fields[1] != selection_id:
                        errors.append(f"line {line_number}: wrong selection ID")
                        continue
                    item = activity.setdefault(normalize_scope(fields[2]), {"seen0": False, "seen1": False})
                    item["seen0"] |= fields[3] == "1"
                    item["seen1"] |= fields[4] == "1"
                    activities += 1
                else:
                    errors.append(f"line {line_number}: malformed golden row {fields[:8]!r}")
            except (ValueError, IndexError) as exc:
                errors.append(f"line {line_number}: {exc}")
    if header > 1:
        errors.append(f"multiple golden headers: {header}")
    if samples == 0 or not series:
        errors.append("golden trace contains no samples")
    return Trace(not errors, errors, path, sha256_file(path), header, samples, activities, dict(series), activity)


def parse_fault(path: Path, fault_id: str) -> Trace:
    errors: list[str] = []
    series: dict[str, Series] = defaultdict(Series)
    activity: dict[str, dict[str, bool]] = {}
    if not path.is_file() or path.stat().st_size == 0:
        return Trace(False, [f"fault trace missing or empty: {path}"], path, None, 0, 0, 0, {}, {})
    header = samples = activities = 0

    def entry(scope: str) -> dict[str, bool]:
        return activity.setdefault(
            scope,
            {"pre_seen0": False, "pre_seen1": False, "observed_seen0": False, "observed_seen1": False},
        )

    def mark(scope: str, channel: str, value: str) -> None:
        item = entry(scope)
        key = {
            ("PRE", "0"): "pre_seen0",
            ("PRE", "1"): "pre_seen1",
            ("OBS", "0"): "observed_seen0",
            ("OBS", "1"): "observed_seen1",
        }.get((channel, value))
        if key:
            item[key] = True
        elif channel not in {"PRE", "OBS"}:
            raise ValueError(f"invalid channel {channel}")

    with open_text(path) as stream:
        for line_number, raw in enumerate(stream, start=1):
            fields = raw.rstrip("\n").split("\t")
            if not fields or fields == [""]:
                continue
            try:
                if fields[0] == "H":
                    header += 1
                    if fields != ["H", "FAULT", fault_id]:
                        errors.append(f"line {line_number}: invalid fault header {fields!r}")
                elif fields[0] == "F" and len(fields) == 8:
                    if fields[1] != fault_id:
                        errors.append(f"line {line_number}: wrong fault ID")
                        continue
                    cycle, time_value = int(fields[2]), int(fields[3])
                    scope = normalize_scope(fields[4])
                    pre, observed, receivers = fields[5].lower(), fields[6].lower(), fields[7].lower()
                    series[scope].add(cycle, time_value, (pre, observed, receivers))
                    mark(scope, "PRE", pre)
                    mark(scope, "OBS", observed)
                    samples += 1
                elif fields[0] == "FA" and len(fields) == 7:
                    if fields[1] != fault_id:
                        errors.append(f"line {line_number}: wrong fault ID")
                        continue
                    mark(normalize_scope(fields[4]), fields[5], fields[6].lower())
                    activities += 1
                elif fields[0] == "FS" and len(fields) == 7:
                    if fields[1] != fault_id:
                        errors.append(f"line {line_number}: wrong fault ID")
                        continue
                    item = entry(normalize_scope(fields[2]))
                    item["pre_seen0"] |= fields[3] == "1"
                    item["pre_seen1"] |= fields[4] == "1"
                    item["observed_seen0"] |= fields[5] == "1"
                    item["observed_seen1"] |= fields[6] == "1"
                    activities += 1
                else:
                    errors.append(f"line {line_number}: malformed fault row {fields[:9]!r}")
            except (ValueError, IndexError) as exc:
                errors.append(f"line {line_number}: {exc}")
    if header != 1:
        errors.append(f"fault trace must contain one header; found {header}")
    if samples == 0 or not series:
        errors.append("fault trace contains no samples")
    return Trace(not errors, errors, path, sha256_file(path), header, samples, activities, dict(series), activity)


def carried(series: Series) -> dict[int, tuple[int, tuple[str, ...]]]:
    return {cycle: (time_value, values) for cycle, time_value, values in series.rows}


def compare_columns(golden: Series, fault: Series, golden_column: int, fault_column: int) -> list[dict[str, Any]]:
    g_changes, f_changes = carried(golden), carried(fault)
    cycles = sorted(set(g_changes) | set(f_changes))
    g_values = f_values = None
    g_time = f_time = 0
    differences: list[dict[str, Any]] = []
    for cycle in cycles:
        if cycle in g_changes:
            g_time, g_values = g_changes[cycle]
        if cycle in f_changes:
            f_time, f_values = f_changes[cycle]
        if g_values is None or f_values is None:
            continue
        if golden_column >= len(g_values) or fault_column >= len(f_values):
            continue
        gv, fv = g_values[golden_column], f_values[fault_column]
        if gv != fv:
            differences.append({"cycle": cycle, "time": max(g_time, f_time), "golden": gv, "fault": fv})
    return differences


def compare_mode_prefix(reference: Trace, other: Trace) -> list[str]:
    errors: list[str] = []
    for scope in sorted(set(reference.series) & set(other.series)):
        ref = carried(reference.series[scope])
        oth = carried(other.series[scope])
        common_cycles = sorted(set(ref) & set(oth))
        for cycle in common_cycles:
            if ref[cycle][1] != oth[cycle][1]:
                errors.append(f"scope={scope} cycle={cycle}: native/diagnostic local trace mismatch")
                if len(errors) >= 20:
                    return errors
    return errors


def validate_fault_spec(spec: Mapping[str, Any]) -> None:
    if spec.get("stage") != FAULT_STAGE:
        raise OracleError("not a Stage-5 fault spec")
    if not FAULT_RE.fullmatch(str(spec.get("fault_id", ""))):
        raise OracleError("invalid fault ID")
    if not SELECTION_RE.fullmatch(str(spec.get("selection_id", ""))):
        raise OracleError("invalid selection ID")
    if int(spec.get("stuck_at", -1)) not in {0, 1}:
        raise OracleError("invalid stuck-at value")


def load_mode(run_dir: Path, expected_purpose: str, expected_mode: str) -> dict[str, Any]:
    result_path = run_dir / "result.json"
    result = load_json(result_path, f"{expected_mode} result")
    if result.get("phase") != "run" or result.get("run_kind") != "fault":
        raise OracleError(f"{expected_mode} result is not a fault run")
    if result.get("run_purpose") != expected_purpose or result.get("assertion_mode") != expected_mode:
        raise OracleError(f"{expected_mode} purpose/mode mismatch")
    raw = result.get("raw_facts")
    if not isinstance(raw, dict) or raw.get("tool", {}).get("status") != "OK":
        raise OracleError(f"{expected_mode} execution is not tool-valid")
    events = raw.get("existing_detector_baseline", {}).get("events")
    if not isinstance(events, list):
        raise OracleError(f"{expected_mode} structured events missing")
    event_path = run_dir / "assertion_events.tsv"
    log_path = run_dir / "xrun.log"
    if not event_path.is_file() or not log_path.is_file():
        raise OracleError(f"{expected_mode} evidence files missing")
    return {
        "result": result,
        "result_path": result_path,
        "result_sha256": sha256_file(result_path),
        "log_path": log_path,
        "log_sha256": sha256_file(log_path),
        "event_path": event_path,
        "event_sha256": sha256_file(event_path),
        "events": events,
    }


def first_event_signature(mode: dict[str, Any]) -> dict[str, Any] | None:
    events = mode["events"]
    if not events:
        return None
    event = events[0]
    return {
        key: event.get(key)
        for key in (
            "cycle",
            "simulation_time",
            "detector_origin",
            "assertion_leaf_name",
            "detector_reported_effect_hint",
            "address",
            "write_data",
            "byte_enable",
        )
    }


def contains_unknown(value: Any) -> bool:
    return isinstance(value, str) and any(char in value.lower() for char in "xz")


def build_oracle(args: argparse.Namespace) -> dict[str, Any]:
    fault_path = args.fault_json.resolve()
    spec = load_json(fault_path, "fault spec")
    validate_fault_spec(spec)
    policy_path = args.assertion_policy.resolve()
    policy = load_json(policy_path, "assertion policy")
    if policy.get("schema_version") != "1.0":
        raise OracleError("unsupported assertion policy schema")
    detectors = policy.get("detectors")
    if not isinstance(detectors, list) or len(detectors) != 1:
        raise OracleError("smoke oracle requires one registered detector")
    detector_policy = detectors[0]
    if detector_policy.get("detector_id") != "cv32e40p.mm_ram.out_of_bounds_write":
        raise OracleError("unexpected detector registry entry")
    fault_id = str(spec["fault_id"])
    selection_id = str(spec["selection_id"])
    stuck_at = int(spec["stuck_at"])
    required_value = str(1 - stuck_at)
    receivers = spec.get("receiver_signals")
    if not isinstance(receivers, list) or not receivers:
        raise OracleError("fault spec has no receiver signals")

    mode_defs = {
        "native": (args.native_run.resolve(), args.native_trace.resolve(), "NATIVE_CHARACTERIZATION"),
        "observe": (args.observe_run.resolve(), args.observe_trace.resolve(), "DIAGNOSTIC_OBSERVE"),
        "diagnostic_quarantine": (
            args.quarantine_run.resolve(),
            args.quarantine_trace.resolve(),
            "DIAGNOSTIC_QUARANTINE",
        ),
    }
    modes: dict[str, dict[str, Any]] = {}
    traces: dict[str, Trace] = {}
    for mode_name, (run_dir, trace_path, purpose) in mode_defs.items():
        modes[mode_name] = load_mode(run_dir, purpose, mode_name)
        traces[mode_name] = parse_fault(trace_path, fault_id)
        if not traces[mode_name].valid:
            raise OracleError(f"invalid {mode_name} trace: {traces[mode_name].errors}")

    golden_path = args.golden_trace.resolve()
    golden = parse_golden(golden_path, selection_id)
    if not golden.valid:
        raise OracleError(f"invalid golden trace: {golden.errors}")

    event_signatures = {name: first_event_signature(mode) for name, mode in modes.items()}
    if event_signatures["native"] is None:
        raise OracleError("smoke oracle requires a native existing-detector event")
    for name, signature in event_signatures.items():
        if signature != event_signatures["native"]:
            raise OracleError(
                f"first detector event mismatch between native and {name}: "
                f"native={event_signatures['native']}, {name}={signature}"
            )

    prefix_errors = compare_mode_prefix(traces["native"], traces["observe"])
    prefix_errors += compare_mode_prefix(traces["native"], traces["diagnostic_quarantine"])
    if prefix_errors:
        raise OracleError("mode-local trace prefix inconsistency: " + "; ".join(prefix_errors[:5]))

    common_scopes = sorted(set(golden.series) & set(traces["native"].series))
    if not common_scopes:
        raise OracleError("golden/native traces have no common scopes")

    scope_facts: list[dict[str, Any]] = []
    diagnostic_candidates: list[dict[str, Any]] = []
    any_activated = False
    all_injection_effective = True
    any_site_diverged = False
    any_receiver_diverged = False

    for scope in common_scopes:
        g_series = golden.series[scope]
        f_series = traces["native"].series[scope]
        g_activity = golden.activity.get(scope, {})
        f_activity = traces["native"].activity.get(scope, {})
        activated = bool(g_activity.get(f"seen{required_value}", False)) or required_value in g_series.known_values(0)
        any_activated |= activated
        observed_known = f_series.known_values(1)
        if f_activity.get("observed_seen0", False):
            observed_known.add("0")
        if f_activity.get("observed_seen1", False):
            observed_known.add("1")
        injection_effective = bool(observed_known) and observed_known <= {str(stuck_at)}
        all_injection_effective &= injection_effective

        site_diff = compare_columns(g_series, f_series, 0, 1)
        receiver_diff = compare_columns(g_series, f_series, 1, 2)
        any_site_diverged |= bool(site_diff)
        any_receiver_diverged |= bool(receiver_diff)
        if site_diff:
            first = site_diff[0]
            diagnostic_candidates.append(
                {
                    **first,
                    "scope": scope,
                    "signal_role": "injected_site",
                    "expression": spec["site"]["source_net"],
                    "receiver_index": None,
                }
            )
        if receiver_diff:
            first = receiver_diff[0]
            diagnostic_candidates.append(
                {
                    **first,
                    "scope": scope,
                    "signal_role": "direct_receiver_vector",
                    "expression": None,
                    "receiver_index": None,
                }
            )
        scope_facts.append(
            {
                "scope": scope,
                "activated": activated,
                "injection_effective": injection_effective,
                "golden_source_known_values": sorted(g_series.known_values(0)),
                "fault_observed_known_values": sorted(observed_known),
                "site_divergence_count": len(site_diff),
                "receiver_divergence_count": len(receiver_diff),
                "earliest_site_divergence": site_diff[0] if site_diff else None,
                "earliest_receiver_divergence": receiver_diff[0] if receiver_diff else None,
            }
        )

    diagnostic_candidates.sort(key=lambda x: (x["cycle"], x["time"], x["scope"], x["signal_role"]))
    earliest = diagnostic_candidates[0] if diagnostic_candidates else None
    native_event = event_signatures["native"]
    if native_event is not None:
        if native_event.get("assertion_leaf_name") != detector_policy.get("assertion_leaf_name"):
            raise OracleError("native detector name does not match assertion policy")
        if native_event.get("detector_reported_effect_hint") != detector_policy.get("effect_hint"):
            raise OracleError("native detector effect does not match assertion policy")
    effect_classes: list[str] = []
    if any_site_diverged:
        effect_classes.append("SITE_DIVERGENCE")
    if any_receiver_diverged:
        effect_classes.append("DIRECT_RECEIVER_DIVERGENCE")
    if native_event:
        effect_classes.append(str(detector_policy["effect_hint"]))
    if native_event and contains_unknown(native_event.get("address")):
        effect_classes.append("UNKNOWN_ADDRESS_AT_MEMORY_INTERFACE")
    effect_classes = sorted(set(effect_classes))

    if native_event:
        propagation_class = "ARCHITECTURAL_INTERFACE_REACHED"
    elif any_receiver_diverged:
        propagation_class = "DIRECT_RECEIVER_REACHED"
    elif any_site_diverged:
        propagation_class = "SITE_ONLY"
    else:
        propagation_class = "NO_OBSERVED_DIVERGENCE"

    native_result = modes["native"]["result"]
    native_raw = native_result["raw_facts"]
    native_completion = native_raw["execution"]["completion"]
    native_arch = native_raw["workload"]["architectural_outcome"]
    activation_class = "ACTIVATED" if any_activated else "NOT_ACTIVATED"
    injection_class = "EFFECTIVE" if all_injection_effective else "INEFFECTIVE_OR_UNPROVEN"
    confidence = "high" if any_activated and all_injection_effective and native_event and (any_site_diverged or any_receiver_diverged) else "medium"

    provenance_modes: dict[str, Any] = {}
    execution_modes: dict[str, Any] = {}
    for name, mode in modes.items():
        trace = traces[name]
        result = mode["result"]
        raw = result["raw_facts"]
        provenance_modes[name] = {
            "run_directory": str(mode_defs[name][0]),
            "runner_result": str(mode["result_path"]),
            "runner_result_sha256": mode["result_sha256"],
            "xrun_log": str(mode["log_path"]),
            "xrun_log_sha256": mode["log_sha256"],
            "assertion_events": str(mode["event_path"]),
            "assertion_events_sha256": mode["event_sha256"],
            "trace": str(trace.path),
            "trace_sha256": trace.sha256,
        }
        execution_modes[name] = {
            "status": result["status"],
            "completion": raw["execution"]["completion"],
            "workload_outcome": raw["workload"]["outcome"],
            "architectural_outcome": raw["workload"]["architectural_outcome"],
            "detector_event_count": len(mode["events"]),
            "intervention": raw["intervention"],
            "counterfactual_after_first_event": name != "native",
        }

    oracle: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "generated_at_utc": utc_now(),
        "stage": ORACLE_STAGE,
        "identity": {
            "fault_id": fault_id,
            "selection_id": selection_id,
            "site_id": spec["site_id"],
            "design": spec["design"],
            "workload": spec["workload"],
            "fault_class": spec["fault_class"],
            "polarity": spec["polarity"],
            "stuck_at": stuck_at,
        },
        "provenance": {
            "fault_spec": str(fault_path),
            "fault_spec_sha256": sha256_file(fault_path),
            "fault_spec_digest_sha256": spec.get("fault_spec_digest_sha256"),
            "assertion_policy": str(policy_path),
            "assertion_policy_sha256": sha256_file(policy_path),
            "registered_detector": detector_policy,
            "golden_trace": str(golden_path),
            "golden_trace_sha256": golden.sha256,
            "execution_modes": provenance_modes,
            "analyzer": str(Path(__file__).resolve()),
            "analyzer_sha256": sha256_file(Path(__file__).resolve()),
        },
        "raw_facts": {
            "execution_modes": execution_modes,
            "trace_validity": {
                "golden": {"valid": golden.valid, "sample_count": golden.sample_count},
                **{
                    name: {"valid": trace.valid, "sample_count": trace.sample_count}
                    for name, trace in traces.items()
                },
            },
            "mode_consistency": {
                "first_detector_event_equal": True,
                "local_trace_common_rows_equal": True,
            },
            "activation": {
                "activated": any_activated,
                "required_golden_value": required_value,
                "scope_facts": scope_facts,
            },
            "injection": {
                "effective": all_injection_effective,
                "expected_observed_value": str(stuck_at),
            },
            "divergence": {
                "site_diverged": any_site_diverged,
                "receiver_diverged": any_receiver_diverged,
                "earliest_diagnostic_candidate": earliest,
                "candidate_count": len(diagnostic_candidates),
                "candidate_preview": diagnostic_candidates[:20],
            },
            "existing_detector_baseline": {
                "triggered": native_event is not None,
                "first_event": native_event,
                "native_status": native_result["status"],
                "detector_registry_entry": detector_policy,
            },
            "effect_evidence": {
                "registered_detector_predicate_violated": native_event is not None,
                "detector_effect_class": detector_policy.get("effect_hint") if native_event else None,
                "unknown_address_observed": bool(native_event and contains_unknown(native_event.get("address"))),
            },
        },
        "dimensions": {
            "execution_validity": "VALID",
            "activation_class": activation_class,
            "injection_class": injection_class,
            "propagation_class": propagation_class,
            "effect_classes": effect_classes,
            "detection_classes": ["DETECTED_BY_PREEXISTING_TB_ASSERTION"] if native_event else [],
            "native_completion_class": native_completion,
            "native_architectural_outcome": native_arch,
            "diagnostic_continuation_available": True,
            "oracle_confidence": confidence,
        },
        "diagnostic_continuation": {
            "observe": execution_modes["observe"],
            "diagnostic_quarantine": execution_modes["diagnostic_quarantine"],
            "interpretation": (
                "Both diagnostic modes are counterfactual after the first detector event. "
                "Observe suppresses fatal termination only. Diagnostic quarantine also "
                "acknowledges and drops the unsafe write. Neither outcome replaces the "
                "native architectural outcome."
            ),
        },
        "guardrails": {
            "native_run_defines_natural_completion_boundary": True,
            "existing_detector_is_baseline_not_ai_oracle": True,
            "quarantine_outcome_is_not_natural_architectural_outcome": True,
            "single_primary_fault_label_intentionally_omitted": True,
            "sva_seed_generated": False,
            "train_test_generation_out_of_scope": True,
        },
    }
    oracle["oracle_digest_sha256"] = canonical_digest(
        {k: v for k, v in oracle.items() if k not in {"generated_at_utc", "oracle_digest_sha256"}}
    )
    return oracle


def render_report(oracle: Mapping[str, Any]) -> str:
    identity = oracle["identity"]
    dimensions = oracle["dimensions"]
    event = oracle["raw_facts"]["existing_detector_baseline"]["first_event"]
    earliest = oracle["raw_facts"]["divergence"]["earliest_diagnostic_candidate"]
    lines = [
        "Fault2Assertion Stage-5 Multidimensional Fault Oracle v3",
        "=" * 80,
        f"Fault ID                    : {identity['fault_id']}",
        f"Fault class                 : {identity['fault_class']}",
        f"Polarity                    : {identity['polarity']}",
        f"Activation                  : {dimensions['activation_class']}",
        f"Injection                   : {dimensions['injection_class']}",
        f"Propagation                 : {dimensions['propagation_class']}",
        f"Effects                     : {', '.join(dimensions['effect_classes'])}",
        f"Native completion           : {dimensions['native_completion_class']}",
        f"Native architectural outcome: {dimensions['native_architectural_outcome']}",
        f"Confidence                  : {dimensions['oracle_confidence']}",
        "",
        "Existing detector baseline",
        "-" * 80,
        f"Detector                    : {event.get('assertion_leaf_name') if event else None}",
        f"Cycle                       : {event.get('cycle') if event else None}",
        f"Time                        : {event.get('simulation_time') if event else None}",
        f"Address                     : {event.get('address') if event else None}",
        "",
        "Earliest local diagnostic candidate",
        "-" * 80,
        f"Candidate                   : {earliest}",
        "",
        "Interpretation boundary",
        "-" * 80,
        oracle["diagnostic_continuation"]["interpretation"],
        "",
        "No SVA seed was generated in the characterization/oracle stage.",
    ]
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fault-json", type=Path, required=True)
    parser.add_argument("--assertion-policy", type=Path, required=True)
    parser.add_argument("--golden-trace", type=Path, required=True)
    parser.add_argument("--native-run", type=Path, required=True)
    parser.add_argument("--native-trace", type=Path, required=True)
    parser.add_argument("--observe-run", type=Path, required=True)
    parser.add_argument("--observe-trace", type=Path, required=True)
    parser.add_argument("--quarantine-run", type=Path, required=True)
    parser.add_argument("--quarantine-trace", type=Path, required=True)
    parser.add_argument("--oracle-output", type=Path, required=True)
    parser.add_argument("--report-output", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        oracle = build_oracle(args)
        atomic_write(args.oracle_output, json.dumps(oracle, indent=2) + "\n", args.force)
        atomic_write(args.report_output, render_report(oracle), args.force)
    except OracleError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"Fault ID        : {oracle['identity']['fault_id']}")
    print(f"Effects         : {oracle['dimensions']['effect_classes']}")
    print(f"Propagation     : {oracle['dimensions']['propagation_class']}")
    print(f"Native outcome  : {oracle['dimensions']['native_architectural_outcome']}")
    print(f"Oracle JSON     : {args.oracle_output.resolve()}")
    print("Oracle result   : PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
