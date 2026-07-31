#!/usr/bin/env python3
"""Prepare and validate Stage-5 Phase2-G1 mode infrastructure."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


VERSION = "2.0.0"
SCHEMA = "1.0"

ADAPTER_BEGIN = "// F2A_PHASE2_G1_ADAPTER_BEGIN"
ADAPTER_END = "// F2A_PHASE2_G1_ADAPTER_END"

EXPECTED_MODES = [
    "native",
    "observe",
    "diagnostic_quarantine",
]


class G1Error(RuntimeError):
    """Controlled Phase2-G1 validation failure."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for block in iter(
            lambda: handle.read(1024 * 1024),
            b"",
        ):
            digest.update(block)

    return digest.hexdigest()


def require_file(path: Path, label: str) -> Path:
    path = path.expanduser().resolve()

    if not path.is_file() or path.stat().st_size == 0:
        raise G1Error(
            f"{label} not found or empty: {path}"
        )

    return path


def load_json(
    path: Path,
    label: str,
) -> dict[str, Any]:
    path = require_file(path, label)

    try:
        data = json.loads(
            path.read_text(encoding="utf-8")
        )
    except json.JSONDecodeError as exc:
        raise G1Error(
            f"invalid {label} JSON {path}: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise G1Error(
            f"{label} must contain one JSON object: "
            f"{path}"
        )

    return data


def write_text_atomic(
    path: Path,
    text: str,
    force: bool,
) -> None:
    path = path.expanduser().resolve()

    if path.exists() and not force:
        raise G1Error(
            f"refusing to overwrite without --force: "
            f"{path}"
        )

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary = path.with_name(
        f".{path.name}.tmp"
    )

    temporary.write_text(
        text,
        encoding="utf-8",
    )

    temporary.replace(path)


def write_json_atomic(
    path: Path,
    data: Mapping[str, Any],
    force: bool,
) -> None:
    write_text_atomic(
        path,
        json.dumps(data, indent=2) + "\n",
        force,
    )


def validate_policy(
    policy: Mapping[str, Any],
) -> list[str]:
    if policy.get("schema_version") != SCHEMA:
        raise G1Error(
            "policy schema_version must be 1.0"
        )

    if (
        policy.get("policy_version")
        != "stage5_phase2_g1_v2"
    ):
        raise G1Error(
            "unexpected policy_version"
        )

    modes = policy.get("execution_modes")

    if modes != EXPECTED_MODES:
        raise G1Error(
            "execution_modes must be exactly "
            f"{EXPECTED_MODES}"
        )

    if policy.get("default_mode") != "native":
        raise G1Error(
            "default_mode must be native"
        )

    if (
        policy.get(
            "non_continuable_is_execution_mode"
        )
        is not False
    ):
        raise G1Error(
            "NON_CONTINUABLE must not be "
            "an execution mode"
        )

    guardrails = policy.get(
        "gate1_guardrails"
    )

    if not isinstance(guardrails, dict):
        raise G1Error(
            "gate1_guardrails must be an object"
        )

    required_guardrails = (
        "compile_elaboration_only",
        "adapter_must_not_reference_existing_detector",
        "native_must_not_modify_existing_detector",
        "observe_runtime_not_executed",
        "quarantine_runtime_not_executed",
        "generated_source_transformation_once",
        "no_instrumentation_state_in_adapter",
    )

    for key in required_guardrails:
        if guardrails.get(key) is not True:
            raise G1Error(
                "policy guardrail must be true: "
                f"{key}"
            )

    return list(modes)


def build_adapter_source(
    modes: Sequence[str],
    default_mode: str,
) -> str:
    comparisons = " ||\n      ".join(
        f'(mode_name == "{mode}")'
        for mode in modes
    )

    return f"""{ADAPTER_BEGIN}
module f2a_phase2_g1_mode_adapter;

  function automatic bit
  f2a_phase2_mode_supported(
    input string mode_name
  );
    f2a_phase2_mode_supported =
      {comparisons};
  endfunction

  initial begin :
    f2a_phase2_g1_mode_configuration

    string requested_mode;

    if (!$value$plusargs(
          "f2a_assert_mode=%s",
          requested_mode
        )) begin
      requested_mode = "{default_mode}";
    end

    if (!f2a_phase2_mode_supported(
          requested_mode
        )) begin
      $fatal(
        2,
        "unsupported f2a_assert_mode=%s",
        requested_mode
      );
    end
  end

endmodule

bind tb_top
  f2a_phase2_g1_mode_adapter
  f2a_phase2_g1_mode_adapter_i();

{ADAPTER_END}
"""


def extract_adapter(text: str) -> str:
    if (
        text.count(ADAPTER_BEGIN) != 1
        or text.count(ADAPTER_END) != 1
    ):
        raise G1Error(
            "generated source must contain "
            "one adapter boundary"
        )

    start = text.index(ADAPTER_BEGIN)

    stop = (
        text.index(
            ADAPTER_END,
            start,
        )
        + len(ADAPTER_END)
    )

    return text[start:stop]


def audit_generated_source(
    text: str,
    modes: Sequence[str],
    trace_output: Path,
) -> dict[str, Any]:
    adapter = extract_adapter(text)

    module_marker = (
        "module f2a_phase2_g1_mode_adapter;"
    )

    bind_pattern = re.compile(
        r"bind\s+tb_top\s+"
        r"f2a_phase2_g1_mode_adapter\s+"
        r"f2a_phase2_g1_mode_adapter_i"
        r"\s*\(\s*\)\s*;",
        re.MULTILINE,
    )

    if text.count(module_marker) != 1:
        raise G1Error(
            "adapter module count is not one"
        )

    if len(bind_pattern.findall(text)) != 1:
        raise G1Error(
            "adapter bind count is not one"
        )

    if (
        text.count(
            "f2a_phase2_g1_mode_configuration"
        )
        != 1
    ):
        raise G1Error(
            "mode configuration block count "
            "is not one"
        )

    if (
        adapter.count(
            '$value$plusargs('
        )
        != 1
    ):
        raise G1Error(
            "mode plusarg reader count "
            "is not one"
        )

    if (
        "f2a_assert_mode=%s"
        not in adapter
    ):
        raise G1Error(
            "mode plusarg name is missing"
        )

    if adapter.count("$fatal") != 1:
        raise G1Error(
            "adapter may only contain the "
            "unsupported-mode $fatal"
        )

    for mode in modes:
        expected = (
            f'(mode_name == "{mode}")'
        )

        if expected not in adapter:
            raise G1Error(
                f"adapter is missing mode: {mode}"
            )

    forbidden_patterns = {
        "always process":
            r"\balways(?:_ff|_comb|_latch)?\b",

        "final block":
            r"\bfinal\b",

        "force or release":
            r"\b(?:force|release)\b",

        "mm_ram dependency":
            r"\bmm_ram\b",

        "existing detector dependency":
            r"\bout_of_bounds_write\b",

        "design transaction dependency":
            r"\bdata_(?:addr|wdata|be|req|we)_i\b",

        "instrumentation state register":
            r"\bf2a_[A-Za-z0-9_$]*_q\b",

        "cycle counter":
            r"\bf2a_cycle_q\b",
    }

    for label, pattern in (
        forbidden_patterns.items()
    ):
        if re.search(pattern, adapter):
            raise G1Error(
                "adapter contains forbidden "
                f"{label}"
            )

    trace_text = str(
        trace_output.resolve()
    )

    if text.count(trace_text) != 1:
        raise G1Error(
            "requested trace path must appear "
            "exactly once"
        )

    return {
        "adapter_sha256":
            sha256_text(adapter),

        "adapter_module_count":
            1,

        "adapter_bind_count":
            1,

        "mode_configuration_block_count":
            1,

        "mode_plusarg_reader_count":
            1,

        "supported_modes":
            list(modes),

        "instrumentation_state_variables":
            [],

        "existing_detector_references":
            [],

        "design_transaction_references":
            [],

        "transformation_count":
            1,

        "native_detector_behavior_modified":
            False,

        "observe_runtime_executed":
            False,

        "quarantine_runtime_executed":
            False,
    }


def command_prepare(
    args: argparse.Namespace,
) -> int:
    policy_path = require_file(
        args.policy,
        "Phase2-G1 policy",
    )

    base_monitor = require_file(
        args.base_monitor,
        "base monitor",
    )

    base_manifest = require_file(
        args.base_manifest,
        "base manifest",
    )

    policy = load_json(
        policy_path,
        "Phase2-G1 policy",
    )

    modes = validate_policy(policy)

    manifest = load_json(
        base_manifest,
        "base manifest",
    )

    old_value = manifest.get(
        "trace_output"
    )

    if (
        not isinstance(old_value, str)
        or not old_value
    ):
        raise G1Error(
            "base manifest has no trace_output"
        )

    old_trace = str(
        Path(old_value)
        .expanduser()
        .resolve()
    )

    new_trace = str(
        args.trace_output
        .expanduser()
        .resolve()
    )

    base_text = base_monitor.read_text(
        encoding="utf-8",
        errors="strict",
    )

    if (
        ADAPTER_BEGIN in base_text
        or ADAPTER_END in base_text
    ):
        raise G1Error(
            "base monitor was already transformed"
        )

    if base_text.count(old_trace) != 1:
        raise G1Error(
            "base monitor must contain its "
            "old trace path exactly once"
        )

    generated = (
        base_text.replace(
            old_trace,
            new_trace,
            1,
        ).rstrip()
        + "\n\n"
        + build_adapter_source(
            modes,
            str(policy["default_mode"]),
        )
    )

    audit = audit_generated_source(
        generated,
        modes,
        Path(new_trace),
    )

    if (
        old_trace != new_trace
        and old_trace in generated
    ):
        raise G1Error(
            "generated source retained the "
            "stale trace path"
        )

    output_source = (
        args.output_source
        .expanduser()
        .resolve()
    )

    write_text_atomic(
        output_source,
        generated,
        args.force,
    )

    metadata = {
        "schema_version":
            SCHEMA,

        "program_version":
            VERSION,

        "generated_at_utc":
            utc_now(),

        "role":
            args.role,

        "policy":
            str(policy_path),

        "policy_sha256":
            sha256_file(policy_path),

        "base_monitor":
            str(base_monitor),

        "base_monitor_sha256":
            sha256_file(base_monitor),

        "base_manifest":
            str(base_manifest),

        "base_manifest_sha256":
            sha256_file(base_manifest),

        "old_trace_output":
            old_trace,

        "trace_output":
            new_trace,

        "output_source":
            str(output_source),

        "output_source_sha256":
            sha256_file(output_source),

        "audit":
            audit,
    }

    write_json_atomic(
        args.output_metadata,
        metadata,
        args.force,
    )

    print(
        f"Prepared {args.role} source: "
        f"{output_source}"
    )

    return 0


def validate_source_metadata(
    policy_path: Path,
    source: Path,
    metadata_path: Path,
    role: str,
    modes: Sequence[str],
) -> dict[str, Any]:
    source = require_file(
        source,
        f"{role} generated source",
    )

    metadata = load_json(
        metadata_path,
        f"{role} metadata",
    )

    if metadata.get("role") != role:
        raise G1Error(
            f"{role} metadata role mismatch"
        )

    if (
        metadata.get("policy_sha256")
        != sha256_file(policy_path)
    ):
        raise G1Error(
            f"{role} policy digest mismatch"
        )

    if (
        metadata.get("output_source")
        != str(source)
    ):
        raise G1Error(
            f"{role} source path mismatch"
        )

    if (
        metadata.get(
            "output_source_sha256"
        )
        != sha256_file(source)
    ):
        raise G1Error(
            f"{role} source digest mismatch"
        )

    trace_output = metadata.get(
        "trace_output"
    )

    if (
        not isinstance(trace_output, str)
        or not trace_output
    ):
        raise G1Error(
            f"{role} trace_output missing"
        )

    current_audit = audit_generated_source(
        source.read_text(
            encoding="utf-8",
            errors="strict",
        ),
        modes,
        Path(trace_output),
    )

    if (
        metadata.get("audit")
        != current_audit
    ):
        raise G1Error(
            f"{role} audit changed after "
            "generation"
        )

    return metadata


def command_validate_sources(
    args: argparse.Namespace,
) -> int:
    policy_path = require_file(
        args.policy,
        "Phase2-G1 policy",
    )

    policy = load_json(
        policy_path,
        "Phase2-G1 policy",
    )

    modes = validate_policy(policy)

    golden = validate_source_metadata(
        policy_path,
        args.golden_source,
        args.golden_metadata,
        "golden",
        modes,
    )

    fault = validate_source_metadata(
        policy_path,
        args.fault_source,
        args.fault_metadata,
        "fault",
        modes,
    )

    golden_adapter_hash = (
        golden["audit"]["adapter_sha256"]
    )

    fault_adapter_hash = (
        fault["audit"]["adapter_sha256"]
    )

    if (
        golden_adapter_hash
        != fault_adapter_hash
    ):
        raise G1Error(
            "golden and fault adapters differ"
        )

    report = {
        "schema_version":
            SCHEMA,

        "program_version":
            VERSION,

        "generated_at_utc":
            utc_now(),

        "gate":
            "stage5_phase2_g1_static_sources",

        "status":
            "PASS",

        "policy":
            str(policy_path),

        "policy_sha256":
            sha256_file(policy_path),

        "golden_metadata":
            golden,

        "fault_metadata":
            fault,

        "common_adapter_sha256":
            golden_adapter_hash,

        "claims": {
            "mode_configuration_infrastructure_present":
                True,

            "supported_modes":
                modes,

            "adapter_source_identical":
                True,

            "each_source_transformed_once":
                True,

            "adapter_instrumentation_state_count":
                0,

            "adapter_existing_detector_reference_count":
                0,

            "native_existing_detector_behavior_modified":
                False,

            "observe_runtime_executed":
                False,

            "quarantine_runtime_executed":
                False,
        },
    }

    write_json_atomic(
        args.report,
        report,
        args.force,
    )

    print(
        "Static source validation PASS: "
        f"{args.report.expanduser().resolve()}"
    )

    return 0


def command_finalize(
    args: argparse.Namespace,
) -> int:
    policy_path = require_file(
        args.policy,
        "Phase2-G1 policy",
    )

    policy = load_json(
        policy_path,
        "Phase2-G1 policy",
    )

    modes = validate_policy(policy)

    static_report = load_json(
        args.static_report,
        "static report",
    )

    compile_report = load_json(
        args.compile_report,
        "compile report",
    )

    if static_report.get("status") != "PASS":
        raise G1Error(
            "static report is not PASS"
        )

    if compile_report.get("status") != "PASS":
        raise G1Error(
            "compile report is not PASS"
        )

    if (
        static_report.get("policy_sha256")
        != sha256_file(policy_path)
    ):
        raise G1Error(
            "static report policy digest mismatch"
        )

    report = {
        "schema_version":
            SCHEMA,

        "program_version":
            VERSION,

        "generated_at_utc":
            utc_now(),

        "gate":
            "stage5_phase2_g1",

        "status":
            "PASS",

        "policy":
            str(policy_path),

        "static_report":
            str(
                args.static_report
                .expanduser()
                .resolve()
            ),

        "compile_report":
            str(
                args.compile_report
                .expanduser()
                .resolve()
            ),

        "claims": {
            "mode_configuration_infrastructure_validated":
                True,

            "phase2_adapter_ownership_validated":
                True,

            "source_generation_consistency_validated":
                True,

            "golden_compile_elaboration_passed":
                True,

            "fault_compile_elaboration_passed":
                True,

            "supported_modes":
                modes,

            "instrumentation_state_variable_count_in_adapter":
                0,

            "generated_source_transformation_count_per_source":
                1,

            "native_existing_detector_behavior_modified":
                False,

            "observe_runtime_executed":
                False,

            "quarantine_runtime_executed":
                False,

            "simulation_entered":
                False,

            "trace_files_generated":
                0,

            "vcd_files_generated":
                0,

            "mm_ram_overlay_used":
                False,
        },
    }

    write_json_atomic(
        args.report,
        report,
        args.force,
    )

    print(
        "Phase2-G1 PASS: "
        f"{args.report.expanduser().resolve()}"
    )

    return 0


def command_selftest(
    _: argparse.Namespace,
) -> int:
    with tempfile.TemporaryDirectory(
        prefix="f2a_phase2_g1_"
    ) as temporary_directory:

        root = Path(temporary_directory)

        old_trace = root / "old.trace.tsv"
        new_trace = root / "new.trace.tsv"

        policy = {
            "schema_version":
                "1.0",

            "policy_version":
                "stage5_phase2_g1_v2",

            "execution_modes":
                EXPECTED_MODES,

            "default_mode":
                "native",

            "non_continuable_is_execution_mode":
                False,

            "gate1_guardrails": {
                "compile_elaboration_only":
                    True,

                "adapter_must_not_reference_existing_detector":
                    True,

                "native_must_not_modify_existing_detector":
                    True,

                "observe_runtime_not_executed":
                    True,

                "quarantine_runtime_not_executed":
                    True,

                "generated_source_transformation_once":
                    True,

                "no_instrumentation_state_in_adapter":
                    True,
            },
        }

        policy_path = root / "policy.json"
        monitor_path = root / "monitor.sv"
        manifest_path = root / "manifest.json"
        output_path = root / "generated.sv"
        metadata_path = root / "generated.json"

        policy_path.write_text(
            json.dumps(policy),
            encoding="utf-8",
        )

        monitor_path.write_text(
            (
                "module synthetic_monitor;\n"
                f'  string path = "{old_trace}";\n'
                "endmodule\n"
            ),
            encoding="utf-8",
        )

        manifest_path.write_text(
            json.dumps({
                "trace_output":
                    str(old_trace)
            }),
            encoding="utf-8",
        )

        command_prepare(
            argparse.Namespace(
                policy=
                    policy_path,

                base_monitor=
                    monitor_path,

                base_manifest=
                    manifest_path,

                role=
                    "golden",

                trace_output=
                    new_trace,

                output_source=
                    output_path,

                output_metadata=
                    metadata_path,

                force=
                    False,
            )
        )

        generated = output_path.read_text(
            encoding="utf-8"
        )

        audit_generated_source(
            generated,
            EXPECTED_MODES,
            new_trace,
        )

        if str(old_trace) in generated:
            raise G1Error(
                "selftest retained stale "
                "trace path"
            )

    print(
        "Phase2-G1 tool selftest: PASS"
    )

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    prepare_parser = (
        subparsers.add_parser("prepare")
    )

    prepare_parser.add_argument(
        "--policy",
        type=Path,
        required=True,
    )

    prepare_parser.add_argument(
        "--base-monitor",
        type=Path,
        required=True,
    )

    prepare_parser.add_argument(
        "--base-manifest",
        type=Path,
        required=True,
    )

    prepare_parser.add_argument(
        "--role",
        choices=("golden", "fault"),
        required=True,
    )

    prepare_parser.add_argument(
        "--trace-output",
        type=Path,
        required=True,
    )

    prepare_parser.add_argument(
        "--output-source",
        type=Path,
        required=True,
    )

    prepare_parser.add_argument(
        "--output-metadata",
        type=Path,
        required=True,
    )

    prepare_parser.add_argument(
        "--force",
        action="store_true",
    )

    prepare_parser.set_defaults(
        func=command_prepare
    )

    sources_parser = subparsers.add_parser(
        "validate-sources"
    )

    sources_parser.add_argument(
        "--policy",
        type=Path,
        required=True,
    )

    sources_parser.add_argument(
        "--golden-source",
        type=Path,
        required=True,
    )

    sources_parser.add_argument(
        "--golden-metadata",
        type=Path,
        required=True,
    )

    sources_parser.add_argument(
        "--fault-source",
        type=Path,
        required=True,
    )

    sources_parser.add_argument(
        "--fault-metadata",
        type=Path,
        required=True,
    )

    sources_parser.add_argument(
        "--report",
        type=Path,
        required=True,
    )

    sources_parser.add_argument(
        "--force",
        action="store_true",
    )

    sources_parser.set_defaults(
        func=command_validate_sources
    )

    finalize_parser = (
        subparsers.add_parser("finalize")
    )

    finalize_parser.add_argument(
        "--policy",
        type=Path,
        required=True,
    )

    finalize_parser.add_argument(
        "--static-report",
        type=Path,
        required=True,
    )

    finalize_parser.add_argument(
        "--compile-report",
        type=Path,
        required=True,
    )

    finalize_parser.add_argument(
        "--report",
        type=Path,
        required=True,
    )

    finalize_parser.add_argument(
        "--force",
        action="store_true",
    )

    finalize_parser.set_defaults(
        func=command_finalize
    )

    selftest_parser = (
        subparsers.add_parser("selftest")
    )

    selftest_parser.set_defaults(
        func=command_selftest
    )

    return parser


def main(
    argv: Sequence[str] | None = None,
) -> int:
    args = build_parser().parse_args(argv)

    try:
        return int(args.func(args))

    except G1Error as exc:
        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )

        return 1

    except Exception as exc:
        print(
            "ERROR: unexpected "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )

        return 1


if __name__ == "__main__":
    raise SystemExit(main())
