#!/usr/bin/env python3
"""Generate a Stage-5 v2 diagnostic oracle from immutable raw facts.

Raw observations are collected first and stored under ``raw_facts``.  The
frozen semantic policy is then applied by stage5_oracle_semantics.py and stored
separately under ``semantic_classification``.  Changing a label never changes
the underlying observations.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import json
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

PROGRAM_VERSION = "2.0.0"
SCHEMA_VERSION = "2.0"
ORACLE_STAGE = "stage_05_diagnostic_oracle_v2"
FAULT_STAGE = "stage_05_fault_materialization"
FAULT_RE = re.compile(r"^TF\d{6}_SA[01]$")
SELECTION_RE = re.compile(r"^TS\d{6}$")


class OracleError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OracleError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise OracleError(f"invalid {label} JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise OracleError(f"{label} must contain one JSON object: {path}")
    return payload


def atomic_write(path: Path, text: str, force: bool) -> None:
    path = path.resolve()
    if path.exists() and not force:
        raise OracleError(f"refusing to overwrite without --force: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def import_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path.resolve())
    if spec is None or spec.loader is None:
        raise OracleError(f"cannot import Python module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def open_text_auto(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def normalize_scope(scope: str) -> str:
    value = scope.strip()
    if "." in value:
        value = value.rsplit(".", 1)[0]
    return value


@dataclass
class SampleSeries:
    rows: list[tuple[int, int, tuple[str, ...]]] = field(default_factory=list)

    def add(self, cycle: int, time_value: int, values: Sequence[str]) -> None:
        normalized = tuple(str(value).strip().lower() for value in values)
        if self.rows and self.rows[-1][2] == normalized:
            return
        self.rows.append((cycle, time_value, normalized))

    def known_values(self, column: int) -> set[str]:
        values: set[str] = set()
        for _, _, row in self.rows:
            if column >= len(row):
                continue
            value = row[column]
            if value and not any(char in value for char in "xz"):
                values.add(value)
        return values


@dataclass
class ParsedTrace:
    valid: bool
    errors: list[str]
    file_exists: bool
    file_size_bytes: int | None
    file_sha256: str | None
    header_count: int
    sample_row_count: int
    activity_row_count: int
    series: dict[str, SampleSeries]
    activity: dict[str, dict[str, bool]]


def _trace_file_metadata(path: Path) -> tuple[bool, int | None, str | None]:
    if not path.is_file():
        return False, None, None
    return True, path.stat().st_size, sha256_file(path)


def parse_golden_trace(path: Path, selection_id: str) -> ParsedTrace:
    errors: list[str] = []
    series: dict[str, SampleSeries] = defaultdict(SampleSeries)
    activity: dict[str, dict[str, bool]] = {}
    exists, size, digest = _trace_file_metadata(path)
    if not exists or size == 0:
        return ParsedTrace(
            False,
            [f"golden trace missing or empty: {path}"],
            exists,
            size,
            digest,
            0,
            0,
            0,
            {},
            {},
        )

    header_count = 0
    sample_count = 0
    activity_count = 0

    def mark(scope: str, value: str) -> None:
        entry = activity.setdefault(scope, {"seen0": False, "seen1": False})
        if value == "0":
            entry["seen0"] = True
        elif value == "1":
            entry["seen1"] = True

    try:
        with open_text_auto(path) as stream:
            for line_number, raw in enumerate(stream, start=1):
                fields = raw.rstrip("\n").split("\t")
                if not fields or fields == [""]:
                    continue
                kind = fields[0]
                try:
                    if kind == "H":
                        header_count += 1
                        if (
                            len(fields) != 3
                            or fields[1] != "FAULT"
                            or fields[2] != fault_id
                        ):
                            errors.append(
                                f"line {line_number}: invalid fault header {fields!r}"
                            )
                        if len(fields) < 2 or fields[1] != "GOLDEN":
                            errors.append(
                                f"line {line_number}: invalid golden header {fields!r}"
                            )
                    elif kind == "G" and len(fields) == 7:
                        if fields[1] != selection_id:
                            errors.append(
                                f"line {line_number}: wrong selection ID {fields[1]!r}"
                            )
                            continue
                        cycle = int(fields[2])
                        time_value = int(fields[3])
                        scope = normalize_scope(fields[4])
                        source = fields[5].lower()
                        receivers = fields[6].lower()
                        series[scope].add(cycle, time_value, (source, receivers))
                        mark(scope, source)
                        sample_count += 1
                    elif kind == "GA" and len(fields) == 7:
                        if fields[1] != selection_id:
                            errors.append(
                                f"line {line_number}: wrong selection ID {fields[1]!r}"
                            )
                            continue
                        if fields[5] != "SRC":
                            errors.append(
                                f"line {line_number}: invalid golden activity channel {fields[5]!r}"
                            )
                            continue
                        mark(normalize_scope(fields[4]), fields[6].lower())
                        activity_count += 1
                    elif kind == "GS" and len(fields) == 5:
                        if fields[1] != selection_id:
                            errors.append(
                                f"line {line_number}: wrong selection ID {fields[1]!r}"
                            )
                            continue
                        scope = normalize_scope(fields[2])
                        entry = activity.setdefault(
                            scope, {"seen0": False, "seen1": False}
                        )
                        entry["seen0"] = entry["seen0"] or fields[3] == "1"
                        entry["seen1"] = entry["seen1"] or fields[4] == "1"
                        activity_count += 1
                    else:
                        errors.append(
                            f"line {line_number}: malformed golden row {fields[:8]!r}"
                        )
                except (ValueError, IndexError) as exc:
                    errors.append(f"line {line_number}: {exc}")
    except OSError as exc:
        errors.append(f"failed to read golden trace: {exc}")

    if header_count > 1:
        errors.append(
            f"golden trace contains multiple headers: {header_count}"
        )
    if sample_count == 0 or not series:
        errors.append("golden trace contains no usable G cycle samples")
    return ParsedTrace(
        valid=not errors,
        errors=errors,
        file_exists=True,
        file_size_bytes=size,
        file_sha256=digest,
        header_count=header_count,
        sample_row_count=sample_count,
        activity_row_count=activity_count,
        series=dict(series),
        activity=activity,
    )


def parse_fault_trace(path: Path, fault_id: str) -> ParsedTrace:
    errors: list[str] = []
    series: dict[str, SampleSeries] = defaultdict(SampleSeries)
    activity: dict[str, dict[str, bool]] = {}
    exists, size, digest = _trace_file_metadata(path)
    if not exists or size == 0:
        return ParsedTrace(
            False,
            [f"fault trace missing or empty: {path}"],
            exists,
            size,
            digest,
            0,
            0,
            0,
            {},
            {},
        )

    header_count = 0
    sample_count = 0
    activity_count = 0

    def entry(scope: str) -> dict[str, bool]:
        return activity.setdefault(
            scope,
            {
                "pre_seen0": False,
                "pre_seen1": False,
                "observed_seen0": False,
                "observed_seen1": False,
            },
        )

    def mark(scope: str, channel: str, value: str) -> None:
        item = entry(scope)
        key = {
            ("PRE", "0"): "pre_seen0",
            ("PRE", "1"): "pre_seen1",
            ("OBS", "0"): "observed_seen0",
            ("OBS", "1"): "observed_seen1",
        }.get((channel, value))
        if key is not None:
            item[key] = True
        elif channel not in {"PRE", "OBS"}:
            raise ValueError(f"invalid fault activity channel {channel!r}")

    try:
        with open_text_auto(path) as stream:
            for line_number, raw in enumerate(stream, start=1):
                fields = raw.rstrip("\n").split("\t")
                if not fields or fields == [""]:
                    continue
                kind = fields[0]
                try:
                    if kind == "H":
                        header_count += 1
                    elif kind == "F" and len(fields) == 8:
                        if fields[1] != fault_id:
                            errors.append(
                                f"line {line_number}: wrong fault ID {fields[1]!r}"
                            )
                            continue
                        cycle = int(fields[2])
                        time_value = int(fields[3])
                        scope = normalize_scope(fields[4])
                        pre = fields[5].lower()
                        observed = fields[6].lower()
                        receivers = fields[7].lower()
                        series[scope].add(
                            cycle, time_value, (pre, observed, receivers)
                        )
                        mark(scope, "PRE", pre)
                        mark(scope, "OBS", observed)
                        sample_count += 1
                    elif kind == "FA" and len(fields) == 7:
                        if fields[1] != fault_id:
                            errors.append(
                                f"line {line_number}: wrong fault ID {fields[1]!r}"
                            )
                            continue
                        mark(
                            normalize_scope(fields[4]),
                            fields[5],
                            fields[6].lower(),
                        )
                        activity_count += 1
                    elif kind == "FS" and len(fields) == 7:
                        if fields[1] != fault_id:
                            errors.append(
                                f"line {line_number}: wrong fault ID {fields[1]!r}"
                            )
                            continue
                        scope = normalize_scope(fields[2])
                        item = entry(scope)
                        item["pre_seen0"] = item["pre_seen0"] or fields[3] == "1"
                        item["pre_seen1"] = item["pre_seen1"] or fields[4] == "1"
                        item["observed_seen0"] = (
                            item["observed_seen0"] or fields[5] == "1"
                        )
                        item["observed_seen1"] = (
                            item["observed_seen1"] or fields[6] == "1"
                        )
                        activity_count += 1
                    else:
                        errors.append(
                            f"line {line_number}: malformed fault row {fields[:9]!r}"
                        )
                except (ValueError, IndexError) as exc:
                    errors.append(f"line {line_number}: {exc}")
    except OSError as exc:
        errors.append(f"failed to read fault trace: {exc}")

    if header_count != 1:
        errors.append(
            f"fault trace must contain exactly one valid header; found {header_count}"
        )
    if sample_count == 0 or not series:
        errors.append("fault trace contains no usable F cycle samples")
    return ParsedTrace(
        valid=not errors,
        errors=errors,
        file_exists=True,
        file_size_bytes=size,
        file_sha256=digest,
        header_count=header_count,
        sample_row_count=sample_count,
        activity_row_count=activity_count,
        series=dict(series),
        activity=activity,
    )


def carry_forward(series: SampleSeries) -> dict[int, tuple[int, tuple[str, ...]]]:
    return {cycle: (time_value, values) for cycle, time_value, values in series.rows}


def compare_columns(
    golden: SampleSeries,
    fault: SampleSeries,
    golden_column: int,
    fault_column: int,
) -> list[tuple[int, int, str, str]]:
    g_changes = carry_forward(golden)
    f_changes = carry_forward(fault)
    cycles = sorted(set(g_changes) | set(f_changes))
    g_values: tuple[str, ...] | None = None
    f_values: tuple[str, ...] | None = None
    g_time = 0
    f_time = 0
    differences: list[tuple[int, int, str, str]] = []
    for cycle in cycles:
        if cycle in g_changes:
            g_time, g_values = g_changes[cycle]
        if cycle in f_changes:
            f_time, f_values = f_changes[cycle]
        if g_values is None or f_values is None:
            continue
        if golden_column >= len(g_values) or fault_column >= len(f_values):
            continue
        g_value = g_values[golden_column]
        f_value = f_values[fault_column]
        if g_value != f_value:
            differences.append((cycle, max(g_time, f_time), g_value, f_value))
    return differences


def pad_bits(value: str, width: int) -> str:
    value = value.lower()
    if len(value) >= width:
        return value[-width:]
    pad = value[0] if value and value[0] in "xz" else "0"
    return pad * (width - len(value)) + value


def receiver_bit_differences(
    golden_value: str,
    fault_value: str,
    width: int,
) -> list[dict[str, Any]]:
    golden_bits = pad_bits(golden_value, width)
    fault_bits = pad_bits(fault_value, width)
    return [
        {
            "receiver_index": index,
            "golden_value": golden_bit,
            "fault_value": fault_bit,
        }
        for index, (golden_bit, fault_bit) in enumerate(
            zip(golden_bits, fault_bits)
        )
        if golden_bit != fault_bit
    ]


def validate_fault_spec(spec: Mapping[str, Any]) -> None:
    if spec.get("stage") != FAULT_STAGE:
        raise OracleError("fault JSON is not a Stage-5 fault specification")
    fault_id = str(spec.get("fault_id", ""))
    selection_id = str(spec.get("selection_id", ""))
    if not FAULT_RE.fullmatch(fault_id):
        raise OracleError(f"invalid fault ID: {fault_id!r}")
    if not SELECTION_RE.fullmatch(selection_id):
        raise OracleError(f"invalid selection ID: {selection_id!r}")
    stored = spec.get("fault_spec_digest_sha256")
    rebuilt = canonical_json_digest(
        {
            key: value
            for key, value in spec.items()
            if key not in {"generated_at_utc", "fault_spec_digest_sha256"}
        }
    )
    if stored != rebuilt:
        raise OracleError(
            f"fault-spec digest mismatch: expected={stored}, actual={rebuilt}"
        )


def load_runner_result(path: Path) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    try:
        result = load_json(path, "runner result")
    except OracleError as exc:
        return {}, [str(exc)]
    if result.get("phase") != "run":
        errors.append(f"runner phase is not run: {result.get('phase')!r}")
    if result.get("run_kind") != "fault":
        errors.append(f"runner kind is not fault: {result.get('run_kind')!r}")
    return result, errors


def extract_log_evidence(path: Path) -> list[str]:
    if not path.is_file():
        return []
    pattern = re.compile(
        r"CRC32 PASS|CRC32 FAIL|EXIT SUCCESS|EXIT FAILURE|maximum cycle|"
        r"F2A_RUNNER_ERROR|(?:xmvlog|xmelab|xmsim|xrun):\s*\*[EF],",
        re.IGNORECASE,
    )
    evidence: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if pattern.search(raw):
            evidence.append(raw[:1000])
            if len(evidence) >= 40:
                break
    return evidence


def render_report(oracle: Mapping[str, Any]) -> str:
    raw = oracle["raw_facts"]
    semantic = oracle["semantic_classification"]
    diagnostic = oracle["diagnostic_oracle"]
    return "\n".join(
        [
            "Fault2Assertion Stage 5 Diagnostic Oracle v2",
            "=" * 80,
            "",
            f"Fault ID             : {oracle['fault_id']}",
            f"Selection ID         : {oracle['selection_id']}",
            f"Fault class          : {oracle['fault_class']}",
            f"Polarity             : {oracle['polarity']}",
            f"Runner status        : {raw['runner']['status']}",
            f"Semantic class       : {semantic['primary_class']}",
            f"Classification reason: {semantic['reason']}",
            f"Policy version       : {semantic['semantics_version']}",
            "",
            "Raw facts",
            "-" * 80,
            f"Golden trace valid   : {raw['trace_validity']['golden_valid']}",
            f"Fault trace valid    : {raw['trace_validity']['fault_valid']}",
            f"Common scopes        : {raw['scope_alignment']['common_scope_count']}",
            f"Activated            : {raw['activation']['activated']}",
            f"Injection effective  : {raw['injection']['effective']}",
            f"Site diverged        : {raw['divergence']['site_diverged']}",
            f"Receiver diverged    : {raw['divergence']['receiver_diverged']}",
            "",
            "Earliest diagnostic candidate",
            "-" * 80,
            f"Cycle                : {diagnostic.get('earliest_cycle')}",
            f"Simulation time      : {diagnostic.get('earliest_time')}",
            f"Scope                : {diagnostic.get('scope')}",
            f"Role                 : {diagnostic.get('signal_role')}",
            f"Expression           : {diagnostic.get('expression')}",
            f"Golden value         : {diagnostic.get('golden_value')}",
            f"Fault value          : {diagnostic.get('fault_value')}",
            "",
            "Interpretation boundary",
            "-" * 80,
            oracle["interpretation_boundary"],
            "",
        ]
    )


def render_sva_seed(oracle: Mapping[str, Any]) -> str:
    diagnostic = oracle["diagnostic_oracle"]
    fault_id = str(oracle["fault_id"])
    expression = diagnostic.get("expression")
    cycle = diagnostic.get("earliest_cycle")
    expected = diagnostic.get("golden_value")
    if expression is None or cycle is None or expected not in {"0", "1"}:
        return (
            f"// {fault_id}: no cycle-local binary diagnostic target was available.\n"
            "// The v2 oracle raw_facts remain the ground-truth record.\n"
        )
    return f"""// Auto-generated Stage-5 v2 SVA seed for {fault_id}.
// Template only; not formally validated.
// Resolve CLK, RESET_N, CYCLE_COUNTER, TARGET, and bound scope separately.

property p_{fault_id.lower()};
  @(posedge CLK) disable iff (!RESET_N)
    (CYCLE_COUNTER == {cycle}) |-> (TARGET === 1'b{expected});
endproperty

assert property (p_{fault_id.lower()});

// Suggested target in scope {diagnostic.get('scope')}:
//   {expression}
"""


def analyze(args: argparse.Namespace) -> dict[str, Any]:
    fault_path = args.fault_json.resolve()
    golden_path = args.golden_trace.resolve()
    fault_trace_path = args.fault_trace.resolve()
    result_path = args.result_json.resolve()
    log_path = args.xrun_log.resolve()
    policy_path = args.policy.resolve()
    semantics_path = args.semantics.resolve()

    spec = load_json(fault_path, "fault spec")
    validate_fault_spec(spec)
    fault_id = str(spec["fault_id"])
    selection_id = str(spec["selection_id"])
    stuck_at = int(spec["stuck_at"])
    required = str(1 - stuck_at)
    receivers = spec.get("receiver_signals")
    if not isinstance(receivers, list) or not receivers:
        raise OracleError("fault spec contains no receiver signals")

    semantics_module = import_module(semantics_path, "f2a_stage5_oracle_semantics_runtime")
    policy = semantics_module.load_policy(policy_path)
    runner_result, runner_errors = load_runner_result(result_path)
    status = str(runner_result.get("status", "MISSING_RESULT"))
    accepted = set(policy["runner_contract"]["accepted_fault_results"])
    markers = runner_result.get("markers", {})
    strict_signature_valid = bool(
        status == "OUTPUT_MATCH"
        and isinstance(markers, Mapping)
        and int(markers.get("exact_signature_count", 0)) >= 1
        and int(markers.get("exit_success_count", 0)) >= 1
        and int(runner_result.get("xrun_exit_status", -1)) == 0
    )
    runner_valid = not runner_errors and status in accepted
    if status == "OUTPUT_MATCH" and not strict_signature_valid:
        runner_valid = False
        runner_errors.append("OUTPUT_MATCH does not satisfy strict signature contract")

    golden = parse_golden_trace(golden_path, selection_id)
    fault = parse_fault_trace(fault_trace_path, fault_id)
    common_scopes = sorted(set(golden.series) & set(fault.series))

    scope_facts: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    any_activated = False
    all_injection_effective = bool(common_scopes)
    any_site_divergence = False
    any_receiver_divergence = False
    any_pre_replay_divergence = False

    for scope in common_scopes:
        g_series = golden.series[scope]
        f_series = fault.series[scope]
        g_activity = golden.activity.get(scope, {})
        f_activity = fault.activity.get(scope, {})
        activated = bool(g_activity.get(f"seen{required}", False)) or (
            required in g_series.known_values(0)
        )
        any_activated = any_activated or activated

        observed_known = f_series.known_values(1)
        if f_activity.get("observed_seen0", False):
            observed_known.add("0")
        if f_activity.get("observed_seen1", False):
            observed_known.add("1")
        injection_effective = bool(observed_known) and observed_known <= {str(stuck_at)}
        all_injection_effective = all_injection_effective and injection_effective

        site_differences = compare_columns(g_series, f_series, 0, 1)
        receiver_differences = compare_columns(g_series, f_series, 1, 2)
        pre_replay_differences = compare_columns(g_series, f_series, 0, 0)
        any_site_divergence = any_site_divergence or bool(site_differences)
        any_receiver_divergence = any_receiver_divergence or bool(receiver_differences)
        any_pre_replay_divergence = any_pre_replay_divergence or bool(
            pre_replay_differences
        )

        earliest_site = site_differences[0] if site_differences else None
        earliest_receiver = (
            receiver_differences[0] if receiver_differences else None
        )
        bit_differences: list[dict[str, Any]] = []
        if earliest_receiver is not None:
            bit_differences = receiver_bit_differences(
                earliest_receiver[2], earliest_receiver[3], len(receivers)
            )
            for bit in bit_differences:
                metadata = receivers[bit["receiver_index"]]
                candidates.append(
                    {
                        "cycle": earliest_receiver[0],
                        "time": earliest_receiver[1],
                        "scope": scope,
                        "signal_role": metadata.get("role"),
                        "receiver_index": bit["receiver_index"],
                        "expression": metadata.get("expression"),
                        "golden_value": bit["golden_value"],
                        "fault_value": bit["fault_value"],
                    }
                )
        if earliest_site is not None:
            candidates.append(
                {
                    "cycle": earliest_site[0],
                    "time": earliest_site[1],
                    "scope": scope,
                    "signal_role": "injected_site",
                    "receiver_index": None,
                    "expression": spec["site"]["source_net"],
                    "golden_value": earliest_site[2],
                    "fault_value": earliest_site[3],
                }
            )

        scope_facts.append(
            {
                "scope": scope,
                "activated": activated,
                "injection_effective": injection_effective,
                "golden_source_known_values": sorted(g_series.known_values(0)),
                "fault_pre_known_values": sorted(f_series.known_values(0)),
                "fault_observed_known_values": sorted(observed_known),
                "site_divergence_count": len(site_differences),
                "receiver_divergence_count": len(receiver_differences),
                "pre_fault_replay_divergence_count": len(pre_replay_differences),
                "earliest_site_divergence_cycle": (
                    earliest_site[0] if earliest_site else None
                ),
                "earliest_receiver_divergence_cycle": (
                    earliest_receiver[0] if earliest_receiver else None
                ),
                "earliest_receiver_bit_differences": bit_differences,
            }
        )

    trace_validity = {
        "golden_valid": golden.valid,
        "fault_valid": fault.valid,
        "golden_errors": golden.errors,
        "fault_errors": fault.errors,
        "golden_header_count": golden.header_count,
        "fault_header_count": fault.header_count,
        "golden_sample_row_count": golden.sample_row_count,
        "fault_sample_row_count": fault.sample_row_count,
        "golden_activity_row_count": golden.activity_row_count,
        "fault_activity_row_count": fault.activity_row_count,
    }
    raw_facts: dict[str, Any] = {
        "provenance": {
            "fault_spec": str(fault_path),
            "fault_spec_sha256": sha256_file(fault_path),
            "fault_spec_digest_sha256": spec["fault_spec_digest_sha256"],
            "golden_trace": str(golden_path),
            "golden_trace_sha256": golden.file_sha256,
            "fault_trace": str(fault_trace_path),
            "fault_trace_sha256": fault.file_sha256,
            "runner_result": str(result_path),
            "runner_result_sha256": (
                sha256_file(result_path) if result_path.is_file() else None
            ),
            "xrun_log": str(log_path),
            "xrun_log_sha256": sha256_file(log_path) if log_path.is_file() else None,
            "semantics_policy": str(policy_path),
            "semantics_policy_sha256": sha256_file(policy_path),
            "semantics_implementation": str(semantics_path),
            "semantics_implementation_sha256": sha256_file(semantics_path),
            "oracle_analyzer": str(Path(__file__).resolve()),
            "oracle_analyzer_sha256": sha256_file(Path(__file__).resolve()),
        },
        "runner": {
            "valid_fault_run": runner_valid,
            "validation_errors": runner_errors,
            "status": status,
            "phase": runner_result.get("phase"),
            "run_kind": runner_result.get("run_kind"),
            "xrun_exit_status": runner_result.get("xrun_exit_status"),
            "strict_signature_valid": strict_signature_valid,
            "verdict_reason": runner_result.get("reason"),
            "markers": markers,
        },
        "trace_validity": trace_validity,
        "scope_alignment": {
            "common_scope_count": len(common_scopes),
            "common_scopes": common_scopes,
            "golden_only_scopes": sorted(set(golden.series) - set(fault.series)),
            "fault_only_scopes": sorted(set(fault.series) - set(golden.series)),
        },
        "activation": {
            "activated": any_activated,
            "required_source_value": required,
            "stuck_at_value": str(stuck_at),
        },
        "injection": {
            "effective": all_injection_effective,
            "expected_observed_value": str(stuck_at),
        },
        "divergence": {
            "site_diverged": any_site_divergence,
            "receiver_diverged": any_receiver_divergence,
            "pre_fault_replay_diverged": any_pre_replay_divergence,
        },
        "functional_outcome": {
            "status": status,
            "log_evidence": extract_log_evidence(log_path),
        },
        "scope_facts": scope_facts,
    }

    semantic = semantics_module.classify(raw_facts, policy)
    candidates.sort(
        key=lambda item: (
            int(item["cycle"]),
            0 if item["signal_role"] != "injected_site" else 1,
            str(item["scope"]),
            -1 if item["receiver_index"] is None else int(item["receiver_index"]),
        )
    )
    diagnostic = candidates[0] if candidates else {
        "cycle": None,
        "time": None,
        "scope": None,
        "signal_role": (
            "final_output_signature"
            if status in {"OUTPUT_MISMATCH", "TIMEOUT"}
            else None
        ),
        "receiver_index": None,
        "expression": None,
        "golden_value": None,
        "fault_value": None,
    }
    if candidates and any_receiver_divergence and all_injection_effective:
        confidence = "high"
    elif candidates and all_injection_effective:
        confidence = "medium"
    elif status in {"OUTPUT_MISMATCH", "TIMEOUT"} and runner_valid:
        confidence = "medium"
    else:
        confidence = "low"

    oracle: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "generated_at_utc": utc_now(),
        "stage": ORACLE_STAGE,
        "fault_id": fault_id,
        "selection_id": selection_id,
        "site_id": spec["site_id"],
        "design": spec["design"],
        "workload": spec["workload"],
        "fault_class": spec["fault_class"],
        "injection_kind": spec["injection_kind"],
        "polarity": spec["polarity"],
        "stuck_at": stuck_at,
        "raw_facts": raw_facts,
        "semantic_classification": asdict(semantic),
        "diagnostic_oracle": {
            "oracle_kind": (
                "earliest_cycle_local_divergence"
                if candidates
                else "functional_outcome_oracle"
            ),
            "confidence": confidence,
            "earliest_cycle": diagnostic["cycle"],
            "earliest_time": diagnostic["time"],
            "scope": diagnostic["scope"],
            "signal_role": diagnostic["signal_role"],
            "receiver_index": diagnostic["receiver_index"],
            "expression": diagnostic["expression"],
            "golden_value": diagnostic["golden_value"],
            "fault_value": diagnostic["fault_value"],
            "candidate_count": len(candidates),
            "candidate_preview": candidates[:20],
            "assertion_seed_status": policy["assertion_seed_status"],
        },
        "interpretation_boundary": policy["scope_statement"],
        "storage_confirmation": {
            "faulty_netlist_required_for_oracle": False,
            "vcd_required_for_oracle": False,
            "raw_trace_can_be_deleted_after_validated_oracle": True,
        },
    }
    oracle["oracle_digest_sha256"] = canonical_json_digest(
        {
            key: value
            for key, value in oracle.items()
            if key not in {"generated_at_utc", "oracle_digest_sha256"}
        }
    )
    return oracle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fault-json", type=Path, required=True)
    parser.add_argument("--golden-trace", type=Path, required=True)
    parser.add_argument("--fault-trace", type=Path, required=True)
    parser.add_argument("--result-json", type=Path, required=True)
    parser.add_argument("--xrun-log", type=Path, required=True)
    parser.add_argument("--semantics", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--oracle-output", type=Path, required=True)
    parser.add_argument("--report-output", type=Path, required=True)
    parser.add_argument("--sva-output", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        oracle = analyze(args)
        atomic_write(
            args.oracle_output,
            json.dumps(oracle, indent=2, sort_keys=False) + "\n",
            args.force,
        )
        atomic_write(args.report_output, render_report(oracle), args.force)
        atomic_write(args.sva_output, render_sva_seed(oracle), args.force)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    semantic = oracle["semantic_classification"]
    print(f"Fault ID             : {oracle['fault_id']}")
    print(f"Raw runner status    : {oracle['raw_facts']['runner']['status']}")
    print(f"Semantic class       : {semantic['primary_class']}")
    print(f"Semantics version    : {semantic['semantics_version']}")
    print(f"Oracle JSON          : {args.oracle_output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
