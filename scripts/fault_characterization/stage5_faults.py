#!/usr/bin/env python3
"""Fault2Assertion Stage 5 v1.0.8 compatibility entrypoint.

This entrypoint loads the preserved, complete v1.0.7 implementation from
``stage5_faults_v107_impl.py`` and replaces only the fault-netlist
materialization contract.  The correction guarantees that a temporary
pre-fault wire is explicitly declared before its first use in a cell or
hierarchical output connection.

The public CLI remains identical to v1.0.7:

* prepare
* apply
* make-golden-monitor
* make-fault-monitor
* split-golden-trace
* validate

The obsolete single-label ``analyze`` and ``aggregate`` commands are disabled
by the Phase-2/3 diagnostic-oracle pipeline.  Use ``stage5_multidim_oracle.py``
and its replay validator instead.  Materialization, monitor generation, trace
splitting, and validation remain unchanged.

The implementation file must be the exact v1.0.7 source that existed before
installing this entrypoint.  Keeping it as a separate immutable module makes the
hotfix small and auditable while preserving every previously tested Stage-5
monitor, trace, and oracle behavior.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


PROGRAM_VERSION = "1.0.8"
MATERIALIZATION_LAYOUT_VERSION = "declaration_before_first_use_v1"
_IMPL_PATH = Path(__file__).resolve().with_name("stage5_faults_v107_impl.py")
_IMPL_MODULE_NAME = "f2a_stage5_v107_impl"


def _load_impl() -> Any:
    if not _IMPL_PATH.is_file():
        raise RuntimeError(
            "preserved Stage-5 v1.0.7 implementation not found: "
            f"{_IMPL_PATH}\n"
            "Rename the original stage5_faults.py to "
            "stage5_faults_v107_impl.py before installing v1.0.8."
        )
    spec = importlib.util.spec_from_file_location(_IMPL_MODULE_NAME, _IMPL_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import preserved implementation: {_IMPL_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_IMPL_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


_impl = _load_impl()
_impl.PROGRAM_VERSION = PROGRAM_VERSION

Stage5Error = _impl.Stage5Error
SCHEMA_VERSION = _impl.SCHEMA_VERSION
STAGE4_CANDIDATE_MARKER = _impl.STAGE4_CANDIDATE_MARKER
STAGE4_SELECTION_MARKER = _impl.STAGE4_SELECTION_MARKER
STAGE5_CAMPAIGN_MARKER = _impl.STAGE5_CAMPAIGN_MARKER
STAGE5_FAULT_MARKER = _impl.STAGE5_FAULT_MARKER
STAGE5_ORACLE_MARKER = _impl.STAGE5_ORACLE_MARKER
FAULT_ID_RE = _impl.FAULT_ID_RE
SELECTION_ID_RE = _impl.SELECTION_ID_RE
NORMAL_IDENTIFIER_RE = _impl.NORMAL_IDENTIFIER_RE
STAGE5_CLOCK_EXPRESSION = _impl.STAGE5_CLOCK_EXPRESSION
PreparedDesign = _impl.PreparedDesign


_IDENTIFIER_CHAR_CLASS = r"A-Za-z0-9_$"


def _token_pattern(token: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![{_IDENTIFIER_CHAR_CLASS}])"
        rf"{re.escape(token)}"
        rf"(?![{_IDENTIFIER_CHAR_CLASS}])"
    )


def validate_materialized_netlist_text(
    text: str,
    *,
    module_name: str,
    source_net: str,
    stuck_at: int,
    temporary_net: str,
    fault_id: str,
) -> dict[str, Any]:
    """Validate the exact declaration/use/assignment ordering of one fault.

    This is the invariant that Gate 1 previously missed.  Xcelium creates an
    implicit wire when a name is first used before an explicit declaration.
    A later ``wire`` declaration then triggers ``*E,DUPIDN``.  The corrected
    layout requires the explicit declaration to be the first token occurrence
    inside the selected module.
    """

    if stuck_at not in {0, 1}:
        raise Stage5Error(f"invalid stuck-at value for {fault_id}: {stuck_at}")

    module_match = _impl.module_match_for(text, module_name)
    module_start = module_match.start()
    module_end = module_match.end()
    module_text = text[module_start:module_end]

    declaration_pattern = re.compile(
        rf"\bwire\s+{re.escape(temporary_net)}\s*;"
    )
    declarations = list(declaration_pattern.finditer(module_text))
    if len(declarations) != 1:
        raise Stage5Error(
            "temporary pre-fault net must have exactly one explicit wire "
            f"declaration: fault={fault_id}, net={temporary_net}, "
            f"declarations={len(declarations)}"
        )

    token_matches = list(_token_pattern(temporary_net).finditer(module_text))
    if len(token_matches) != 2:
        raise Stage5Error(
            "temporary pre-fault net must occur exactly twice: once in its "
            "declaration and once in the rewritten driver connection; "
            f"fault={fault_id}, net={temporary_net}, "
            f"occurrences={len(token_matches)}"
        )

    declaration = declarations[0]
    declaration_token_offset = module_text.find(
        temporary_net,
        declaration.start(),
        declaration.end(),
    )
    if declaration_token_offset < 0:
        raise Stage5Error(
            f"cannot locate temporary-net token inside declaration: {fault_id}"
        )

    first_token_offset = token_matches[0].start()
    connection_token_offset = token_matches[1].start()
    if first_token_offset != declaration_token_offset:
        raise Stage5Error(
            "temporary pre-fault net is used before its explicit declaration; "
            f"fault={fault_id}, net={temporary_net}, "
            f"first_use_offset={first_token_offset}, "
            f"declaration_offset={declaration_token_offset}"
        )
    if declaration_token_offset >= connection_token_offset:
        raise Stage5Error(
            "temporary pre-fault declaration does not precede the rewritten "
            f"driver connection: {fault_id}"
        )

    assignment = (
        f"assign {_impl.sv_expression(source_net)} = 1'b{stuck_at};"
    )
    assignment_count = module_text.count(assignment)
    if assignment_count != 1:
        raise Stage5Error(
            "faulted source net must have exactly one Stage-5 stuck-at "
            f"assignment: fault={fault_id}, assignment={assignment!r}, "
            f"count={assignment_count}"
        )

    assignment_offset = module_text.find(assignment)
    if assignment_offset <= connection_token_offset:
        raise Stage5Error(
            "stuck-at assignment must be inserted after the rewritten driver "
            f"connection and after existing declarations: {fault_id}"
        )

    return {
        "materialization_layout_version": MATERIALIZATION_LAYOUT_VERSION,
        "module": module_name,
        "temporary_net": temporary_net,
        "temporary_net_occurrence_count": len(token_matches),
        "declaration_offset_in_module": declaration_token_offset,
        "driver_use_offset_in_module": connection_token_offset,
        "assignment_offset_in_module": assignment_offset,
        "assignment": assignment,
    }


def build_fault_netlist_text(
    *,
    text: str,
    module_name: str,
    source_net: str,
    stuck_at: int,
    temporary_net: str,
    fault_id: str,
    connection_start: int,
    connection_end: int,
    replacement_connection: str,
    expected_original_connection: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Create one corrected run-local fault netlist from immutable source text."""

    if stuck_at not in {0, 1}:
        raise Stage5Error(f"invalid stuck-at value for {fault_id}: {stuck_at}")
    if temporary_net in text:
        raise Stage5Error(f"temporary-net collision: {temporary_net}")
    if not (0 <= connection_start < connection_end <= len(text)):
        raise Stage5Error(
            f"invalid driver connection span for {fault_id}: "
            f"{(connection_start, connection_end)}"
        )

    module_match = _impl.module_match_for(text, module_name)
    body_start = module_match.start("body")
    body_end = module_match.end("body")
    if not (body_start <= connection_start < connection_end <= body_end):
        raise Stage5Error(
            "driver connection is outside the selected module body: "
            f"fault={fault_id}, module={module_name}, "
            f"connection={(connection_start, connection_end)}, "
            f"body={(body_start, body_end)}"
        )

    original_connection = text[connection_start:connection_end]
    if expected_original_connection is not None:
        if _impl.canonical_signal(original_connection) != _impl.canonical_signal(
            expected_original_connection
        ):
            raise Stage5Error(
                f"driver connection changed for {fault_id}: "
                f"expected={expected_original_connection!r}, "
                f"actual={original_connection!r}"
            )

    declaration_block = (
        "\n"
        f"  // Fault2Assertion Stage-5 fault {fault_id}: "
        "temporary pre-fault net declared before first use\n"
        f"  wire {temporary_net};\n"
    )
    assignment_text = (
        f"assign {_impl.sv_expression(source_net)} = 1'b{stuck_at};"
    )
    assignment_block = (
        "\n"
        f"  // Fault2Assertion Stage-5 fault {fault_id}: "
        "run-local stuck-at assignment\n"
        f"  {assignment_text}\n"
    )

    modified = _impl.apply_edits(
        text,
        [
            (body_start, body_start, declaration_block),
            (connection_start, connection_end, replacement_connection),
            (body_end, body_end, assignment_block),
        ],
    )

    layout = validate_materialized_netlist_text(
        modified,
        module_name=module_name,
        source_net=source_net,
        stuck_at=stuck_at,
        temporary_net=temporary_net,
        fault_id=fault_id,
    )
    return modified, layout


def build_modified_netlist(
    prepared: PreparedDesign,
    site: Mapping[str, Any],
    fault_id: str,
    stuck_at: int,
    temporary_net: str,
) -> tuple[str, dict[str, Any]]:
    connection, replacement, resolved_driver = _impl.resolve_driver_edit(
        prepared,
        site,
        temporary_net,
    )
    module_name = str(site["module"])
    source_net = str(site["source_net"])

    modified, layout = build_fault_netlist_text(
        text=prepared.text,
        module_name=module_name,
        source_net=source_net,
        stuck_at=stuck_at,
        temporary_net=temporary_net,
        fault_id=fault_id,
        connection_start=connection.expression_start,
        connection_end=connection.expression_end,
        replacement_connection=replacement,
        expected_original_connection=resolved_driver["original_connection"],
    )

    modification = {
        "method": (
            "declare_temporary_net_before_first_use_then_"
            "split_unique_driver_output_and_assign_original_site_constant"
        ),
        "materialization_layout_version": MATERIALIZATION_LAYOUT_VERSION,
        "temporary_pre_fault_net": temporary_net,
        "temporary_net_declaration": f"wire {temporary_net};",
        "temporary_net_declaration_location": "module_body_start",
        "stuck_at_assignment": (
            f"assign {source_net} = 1'b{stuck_at};"
        ),
        "stuck_at_assignment_location": "module_body_end",
        "driver_connection": resolved_driver,
        "layout_validation": layout,
        "definition_level_semantics": (
            "the selected module-definition site is modified; every elaborated "
            "instance of that module observes the same injected site fault"
        ),
    }
    return modified, modification


_original_preflight_selected_sites = _impl.preflight_selected_sites


def preflight_selected_sites(
    prepared: PreparedDesign,
    candidates: Mapping[str, Any],
    selection: Mapping[str, Any],
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    """Run the original structural preflight plus declaration-order preflight."""

    candidate_by_id, selected_by_id = _original_preflight_selected_sites(
        prepared,
        candidates,
        selection,
    )

    failures: list[str] = []
    for rank, selected in enumerate(selection["selected_sites"], start=1):
        selection_id = str(selected["selection_id"])
        site_id = str(selected["site_id"])
        site = candidate_by_id[site_id]
        temporary_net = f"f2a_preflight_{selection_id.lower()}"
        try:
            build_modified_netlist(
                prepared,
                site,
                f"PREFLIGHT_{rank:06d}",
                0,
                temporary_net,
            )
        except Stage5Error as exc:
            failures.append(
                f"selection_id={selection_id} site_id={site_id}: {exc}"
            )

    if failures:
        preview_limit = 50
        preview = "\n".join(f"  - {item}" for item in failures[:preview_limit])
        suffix = ""
        if len(failures) > preview_limit:
            suffix = (
                f"\n  ... {len(failures) - preview_limit} additional failures omitted"
            )
        raise Stage5Error(
            "Stage-5 declaration-order materialization preflight failed before "
            "writing artifacts.\n"
            f"Invalid selected sites: {len(failures)}\n"
            f"{preview}{suffix}"
        )

    return candidate_by_id, selected_by_id


def command_apply(args: Any) -> int:
    fault_path = args.fault_json.resolve()
    output = args.output_netlist.resolve()
    spec = _impl.load_json(fault_path, "Stage-5 fault spec")

    if spec.get("stage") != STAGE5_FAULT_MARKER:
        raise Stage5Error("fault JSON is not a Stage-5 fault spec")
    if str(spec.get("program_version")) != PROGRAM_VERSION:
        raise Stage5Error(
            "fault spec was not generated by the active Stage-5 tool; "
            f"fault={spec.get('fault_id')}, "
            f"spec_version={spec.get('program_version')!r}, "
            f"tool_version={PROGRAM_VERSION!r}. Regenerate the mini campaign."
        )

    modification = spec.get("modification")
    if not isinstance(modification, dict):
        raise Stage5Error("fault spec modification metadata is missing")
    if modification.get("materialization_layout_version") != (
        MATERIALIZATION_LAYOUT_VERSION
    ):
        raise Stage5Error(
            "fault spec uses an obsolete materialization layout; "
            f"fault={spec.get('fault_id')}, "
            f"layout={modification.get('materialization_layout_version')!r}, "
            f"required={MATERIALIZATION_LAYOUT_VERSION!r}"
        )

    source = Path(str(spec["mapped_netlist"]["path"])).resolve()
    expected_sha = str(spec["mapped_netlist"]["sha256"])
    if not source.is_file():
        raise Stage5Error(f"golden mapped netlist not found: {source}")
    actual_sha = _impl.sha256_file(source)
    if actual_sha != expected_sha:
        raise Stage5Error(
            "golden netlist SHA mismatch\n"
            f"  expected: {expected_sha}\n"
            f"  actual:   {actual_sha}"
        )
    if output == source:
        raise Stage5Error("run-local output must not overwrite the golden netlist")
    if output.exists() and not args.force:
        raise Stage5Error(f"output netlist already exists: {output}")

    text = source.read_text(encoding="utf-8", errors="strict")
    driver = modification.get("driver_connection")
    if not isinstance(driver, dict):
        raise Stage5Error("fault spec driver_connection metadata is missing")

    fault_id = str(spec["fault_id"])
    module_name = str(spec["site"]["module"])
    source_net = str(spec["site"]["source_net"])
    stuck_at = int(spec["stuck_at"])
    temporary_net = str(modification["temporary_pre_fault_net"])

    modified, layout = build_fault_netlist_text(
        text=text,
        module_name=module_name,
        source_net=source_net,
        stuck_at=stuck_at,
        temporary_net=temporary_net,
        fault_id=fault_id,
        connection_start=int(driver["connection_start"]),
        connection_end=int(driver["connection_end"]),
        replacement_connection=str(driver["replacement_connection"]),
        expected_original_connection=str(driver["original_connection"]),
    )

    expected_layout = modification.get("layout_validation")
    if isinstance(expected_layout, dict):
        expected_fields = (
            "materialization_layout_version",
            "temporary_net_occurrence_count",
            "assignment",
        )
        for key in expected_fields:
            if expected_layout.get(key) != layout.get(key):
                raise Stage5Error(
                    "runtime materialization layout differs from the fault spec: "
                    f"fault={fault_id}, field={key}, "
                    f"spec={expected_layout.get(key)!r}, "
                    f"runtime={layout.get(key)!r}"
                )

    _impl.atomic_write_text(output, modified, force=args.force)
    if _impl.sha256_file(source) != expected_sha:
        output.unlink(missing_ok=True)
        raise Stage5Error("immutable golden netlist changed during apply")

    print(f"Fault ID             : {fault_id}")
    print(f"Program version      : {PROGRAM_VERSION}")
    print(f"Layout version       : {MATERIALIZATION_LAYOUT_VERSION}")
    print(f"Golden netlist       : {source}")
    print(f"Run-local netlist    : {output}")
    print(f"Run-local SHA-256    : {_impl.sha256_file(output)}")
    print("Declaration ordering : PASS")
    return 0


# Install the corrected functions into the preserved implementation module.
# Existing v1.0.7 command_prepare and parser functions resolve these globals at
# runtime, so all CLI entrypoints automatically use the v1.0.8 contract.
_impl.PROGRAM_VERSION = PROGRAM_VERSION
_impl.MATERIALIZATION_LAYOUT_VERSION = MATERIALIZATION_LAYOUT_VERSION
_impl.validate_materialized_netlist_text = validate_materialized_netlist_text
_impl.build_fault_netlist_text = build_fault_netlist_text
_impl.build_modified_netlist = build_modified_netlist
_impl.preflight_selected_sites = preflight_selected_sites
_impl.command_apply = command_apply


# Re-export the complete v1.0.7 public surface so version guards, validators,
# monitor generators, trace parsers, and oracle tools can import this entrypoint
# exactly as they imported the former single-file implementation.
for _name in dir(_impl):
    if _name.startswith("__"):
        continue
    globals().setdefault(_name, getattr(_impl, _name))

# Reassert corrected symbols after the generic re-export.
globals()["PROGRAM_VERSION"] = PROGRAM_VERSION
globals()["MATERIALIZATION_LAYOUT_VERSION"] = MATERIALIZATION_LAYOUT_VERSION
globals()["validate_materialized_netlist_text"] = validate_materialized_netlist_text
globals()["build_fault_netlist_text"] = build_fault_netlist_text
globals()["build_modified_netlist"] = build_modified_netlist
globals()["preflight_selected_sites"] = preflight_selected_sites
globals()["command_apply"] = command_apply


DISABLED_LEGACY_ORACLE_COMMANDS = {"analyze", "aggregate"}


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in DISABLED_LEGACY_ORACLE_COMMANDS:
        print(
            "ERROR: legacy single-label Stage-5 oracle commands are disabled. "
            "Use stage5_multidim_oracle.py after native/observe/quarantine "
            "evidence has been validated.",
            file=sys.stderr,
        )
        return 2
    return int(_impl.main(arguments))


if __name__ == "__main__":
    raise SystemExit(main())
