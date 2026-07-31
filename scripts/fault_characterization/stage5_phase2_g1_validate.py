#!/usr/bin/env python3
"""Validate the clean Stage-5 Phase2-G1 compile/elaboration experiment.

Phase2-G1 proves four things only:

1. Mode-configuration infrastructure exists exactly once.
2. Instrumentation ownership is structurally unambiguous.
3. Golden and fault runs compile the same single generated overlay.
4. Compile/elaboration succeeds without entering simulation.

This validator intentionally does not claim full NATIVE runtime equivalence.
That behavioral equivalence belongs to Phase2-G2.  G1 checks source-level
NATIVE guardrails: the original fatal call is retained and the NATIVE action
branch does not write transaction or memory state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


VERSION = "2.1.0"
MODES = ("native", "observe", "diagnostic_quarantine")
FATAL_PHRASE = "out of bounds write to %08x with %08x"

MODE_READER_RE = re.compile(
    r'\$value\$plusargs\s*\(\s*"f2a_assert_mode=%s"\s*,',
    re.DOTALL,
)

EVENT_FILE_READER_RE = re.compile(
    r'\$value\$plusargs\s*\(\s*'
    r'"f2a_assert_event_file=%s"\s*,\s*'
    r'f2a_assert_event_file\s*\)',
    re.DOTALL,
)

STATE_LABEL_RE = re.compile(
    r"\bbegin\s*:\s*f2a_assertion_state\b",
    re.DOTALL,
)

EVENT_TASK_RE = re.compile(
    r"\btask\s+automatic\s+"
    r"f2a_emit_out_of_bounds_write_event\b",
    re.DOTALL,
)

NET_TYPES = (
    "wire",
    "uwire",
    "tri",
    "tri0",
    "tri1",
    "wand",
    "wor",
    "triand",
    "trior",
)

MANAGED_DECLARATIONS = {
    "f2a_cycle_q",
    "f2a_oob_write_violation_q",
    "f2a_assert_mode_q",
    "f2a_assert_mode_name",
    "f2a_assert_event_file",
    "f2a_assert_event_fd",
    "f2a_assert_event_count_q",
}

# Variables below are owned through explicit SystemVerilog assignments.
# f2a_assert_event_file is intentionally absent: $value$plusargs writes its
# second argument, so its owner is audited separately as a system-function
# output argument inside the mode-configuration initial block.
ASSIGNMENT_OWNERS = {
    "f2a_cycle_q": "state",
    "f2a_oob_write_violation_q": "state",
    "f2a_assert_mode_q": "config",
    "f2a_assert_mode_name": "config",
    "f2a_assert_event_fd": "config",
    "f2a_assert_event_count_q": "event",
}

FORBIDDEN_NATIVE_LHS_RE = re.compile(
    r"\b(?:"
    r"data_(?:addr|wdata|be|req|we|gnt|rvalid|rdata)_[io]"
    r"|mem(?:ory)?[A-Za-z0-9_$]*"
    r"|ram[A-Za-z0-9_$]*"
    r")\b"
    r"\s*(?:\[[^\]]+\]\s*)?"
    r"(?:<=|(?<![=!<>])=(?!=))",
    re.DOTALL,
)


class ValidationError(RuntimeError):
    """Controlled Phase2-G1 validation failure."""


@dataclass(frozen=True)
class Span:
    start: int
    stop: int
    label: str

    def contains(self, position: int) -> bool:
        return self.start <= position < self.stop


@dataclass(frozen=True)
class Assignment:
    variable: str
    operator_position: int
    statement_start: int
    statement_stop: int
    line: int
    statement: str
    kind: str


@dataclass(frozen=True)
class ViolationOwner:
    kind: str
    line: int
    statement: str


def require_file(path: Path, label: str) -> Path:
    path = path.expanduser().resolve()
    if not path.is_file() or path.stat().st_size == 0:
        raise ValidationError(f"{label} not found or empty: {path}")
    return path


def load_json(path: Path, label: str) -> dict[str, Any]:
    path = require_file(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be a JSON object: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def line_number(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


def mask_comments_and_strings(text: str) -> str:
    """Mask comments/string contents while preserving source offsets."""

    output = list(text)
    index = 0
    length = len(text)

    while index < length:
        if text.startswith("//", index):
            stop = text.find("\n", index)
            if stop < 0:
                stop = length
            for cursor in range(index, stop):
                output[cursor] = " "
            index = stop
            continue

        if text.startswith("/*", index):
            stop = text.find("*/", index + 2)
            if stop < 0:
                raise ValidationError("unterminated block comment in overlay")
            stop += 2
            for cursor in range(index, stop):
                if output[cursor] != "\n":
                    output[cursor] = " "
            index = stop
            continue

        if text[index] == '"':
            cursor = index + 1
            while cursor < length:
                if text[cursor] == "\\":
                    cursor += 2
                    continue
                if text[cursor] == '"':
                    cursor += 1
                    break
                cursor += 1
            if cursor > length or text[cursor - 1] != '"':
                raise ValidationError("unterminated string in overlay")
            for mark in range(index, cursor):
                if output[mark] != "\n":
                    output[mark] = " "
            index = cursor
            continue

        index += 1

    return "".join(output)


def keyword_tokens(masked: str, keywords: Iterable[str]) -> list[tuple[str, int, int]]:
    alternatives = "|".join(re.escape(item) for item in keywords)
    pattern = re.compile(rf"\b(?:{alternatives})\b")
    return [
        (match.group(0), match.start(), match.end())
        for match in pattern.finditer(masked)
    ]


def balanced_begin_span(masked: str, begin_position: int, label: str) -> Span:
    tokens = keyword_tokens(masked[begin_position:], ("begin", "end"))
    depth = 0
    saw_begin = False

    for token, relative_start, relative_stop in tokens:
        absolute_start = begin_position + relative_start
        absolute_stop = begin_position + relative_stop

        if token == "begin":
            depth += 1
            saw_begin = True
        else:
            depth -= 1

        if saw_begin and depth == 0:
            return Span(begin_position, absolute_stop, label)

        if depth < 0:
            break

    raise ValidationError(f"unterminated {label} begin/end block")


def find_first_begin(masked: str, start: int, stop: int, label: str) -> int:
    match = re.search(r"\bbegin\b", masked[start:stop])
    if match is None:
        raise ValidationError(f"cannot find begin for {label}")
    return start + match.start()


def find_state_span(text: str, masked: str) -> Span:
    labels = list(STATE_LABEL_RE.finditer(masked))
    if len(labels) != 1:
        raise ValidationError(
            "expected exactly one f2a_assertion_state block; "
            f"found {len(labels)}"
        )

    label = labels[0]
    begin_position = label.start()
    search_start = max(0, begin_position - 500)
    prefix = masked[search_start:begin_position]

    if re.search(r"\balways_ff\b", prefix) is None:
        raise ValidationError(
            "f2a_assertion_state is not owned by an always_ff process"
        )

    return balanced_begin_span(masked, begin_position, "f2a_assertion_state")


def find_config_span(text: str, masked: str) -> Span:
    readers = list(MODE_READER_RE.finditer(text))
    if len(readers) != 1:
        raise ValidationError(
            "expected exactly one f2a_assert_mode plusarg reader; "
            f"found {len(readers)}"
        )

    reader_position = readers[0].start()
    initial_matches = list(re.finditer(r"\binitial\b", masked[:reader_position]))
    if not initial_matches:
        raise ValidationError("cannot find mode-configuration initial block")

    initial_position = initial_matches[-1].start()
    begin_position = find_first_begin(
        masked,
        initial_position,
        reader_position,
        "mode configuration",
    )

    span = balanced_begin_span(masked, begin_position, "mode configuration")
    if not span.contains(reader_position):
        raise ValidationError("mode reader is outside resolved configuration block")
    return span


def find_task_span(text: str, masked: str) -> Span:
    tasks = list(EVENT_TASK_RE.finditer(masked))
    if len(tasks) != 1:
        raise ValidationError(
            "expected exactly one f2a_emit_out_of_bounds_write_event task; "
            f"found {len(tasks)}"
        )

    start = tasks[0].start()
    end_match = re.search(r"\bendtask\b", masked[tasks[0].end():])
    if end_match is None:
        raise ValidationError("unterminated event-emission task")

    stop = tasks[0].end() + end_match.end()
    return Span(start, stop, "event-emission task")


def statement_bounds(masked: str, position: int) -> tuple[int, int]:
    previous_semicolon = masked.rfind(";", 0, position)
    previous_begin = max(
        (match.end() for match in re.finditer(r"\bbegin\b", masked[:position])),
        default=0,
    )
    previous_end = max(
        (match.end() for match in re.finditer(r"\bend\b", masked[:position])),
        default=0,
    )
    start = max(previous_semicolon + 1, previous_begin, previous_end)
    stop = masked.find(";", position)
    if stop < 0:
        raise ValidationError(
            f"assignment at line {line_number(masked, position)} has no semicolon"
        )
    return start, stop + 1


def classify_assignment(statement_masked: str) -> str:
    stripped = statement_masked.strip()
    if re.match(r"^assign\b", stripped):
        return "explicit_continuous_assignment"

    net_prefix = "|".join(re.escape(item) for item in NET_TYPES)
    if re.match(rf"^(?:{net_prefix})\b", stripped):
        return "net_declaration_assignment"

    declaration_prefix = re.compile(
        r"^(?:"
        r"logic|bit|reg|integer|int|longint|time|string"
        r"|f2a_[A-Za-z0-9_$]+_e"
        r")\b"
    )
    if declaration_prefix.match(stripped):
        return "variable_declaration_initializer"

    return "procedural_assignment"


def assignments_for(text: str, masked: str, variable: str) -> list[Assignment]:
    pattern = re.compile(
        rf"\b{re.escape(variable)}\b\s*"
        rf"(?:<=|(?<![=!<>])=(?!=))",
        re.DOTALL,
    )

    records: list[Assignment] = []
    seen_statements: set[tuple[int, int]] = set()

    for match in pattern.finditer(masked):
        start, stop = statement_bounds(masked, match.start())
        key = (start, stop)
        if key in seen_statements:
            continue
        seen_statements.add(key)

        records.append(
            Assignment(
                variable=variable,
                operator_position=match.start(),
                statement_start=start,
                statement_stop=stop,
                line=line_number(text, match.start()),
                statement=text[start:stop].strip(),
                kind=classify_assignment(masked[start:stop]),
            )
        )

    return records


def require_assignments_in_spans(
    assignments: Sequence[Assignment],
    spans: Sequence[Span],
    variable: str,
    owner: str,
) -> None:
    if not assignments:
        raise ValidationError(f"{variable} has no assignment")

    outside = [
        item.line
        for item in assignments
        if not any(span.contains(item.operator_position) for span in spans)
    ]

    if outside:
        raise ValidationError(
            f"{variable} has assignments outside {owner}: lines={outside}"
        )

    invalid_kinds = [
        item
        for item in assignments
        if item.kind in {
            "variable_declaration_initializer",
            "net_declaration_assignment",
        }
    ]
    if invalid_kinds:
        details = [(item.line, item.kind) for item in invalid_kinds]
        raise ValidationError(
            f"{variable} uses declaration-based assignment instead of its "
            f"declared owner: {details}"
        )


def audit_event_file_owner(
    text: str,
    masked: str,
    config_span: Span,
) -> list[dict[str, Any]]:
    """Audit f2a_assert_event_file ownership.

    SystemVerilog $value$plusargs stores the converted value into its second
    argument.  That write is not represented by an '=' or '<=' token, so it
    must not be validated with assignments_for().

    The ownership contract is:
      * exactly one matching $value$plusargs reader;
      * the reader is inside the resolved mode-configuration initial block;
      * any optional explicit assignments are also inside that same block;
      * declaration initializers remain forbidden by the ownership report.
    """

    readers = list(EVENT_FILE_READER_RE.finditer(text))
    if len(readers) != 1:
        raise ValidationError(
            "expected exactly one f2a_assert_event_file plusarg reader; "
            f"found {len(readers)}"
        )

    reader = readers[0]
    if not config_span.contains(reader.start()):
        raise ValidationError(
            "f2a_assert_event_file plusarg reader is outside the "
            "mode-configuration initial block"
        )

    explicit = assignments_for(
        text,
        masked,
        "f2a_assert_event_file",
    )

    outside = [
        item.line
        for item in explicit
        if not config_span.contains(item.operator_position)
    ]
    if outside:
        raise ValidationError(
            "f2a_assert_event_file has explicit assignments outside the "
            f"mode-configuration initial block: lines={outside}"
        )

    invalid = [
        (item.line, item.kind)
        for item in explicit
        if item.kind in {
            "variable_declaration_initializer",
            "net_declaration_assignment",
        }
    ]
    if invalid:
        raise ValidationError(
            "f2a_assert_event_file uses declaration-based assignment: "
            f"{invalid}"
        )

    records: list[dict[str, Any]] = [
        {
            "line": line_number(text, reader.start()),
            "kind": "system_function_output_argument",
            "statement": normalize_space(
                text[reader.start():reader.end()]
            ),
        }
    ]

    records.extend(
        {
            "line": item.line,
            "kind": item.kind,
            "statement": normalize_space(item.statement),
        }
        for item in explicit
    )

    return records


def parse_version(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError as exc:
        raise ValidationError(f"invalid ownership wrapper version: {version}") from exc


def audit_ownership_report(overlay_path: Path) -> dict[str, Any]:
    report_path = overlay_path.with_name("mm_ram.stage5.ownership.json")
    report = load_json(report_path, "overlay ownership report")

    version = str(report.get("wrapper_version", ""))
    if parse_version(version) < (1, 2, 0):
        raise ValidationError(
            "ownership wrapper must be at least 1.2.0; "
            f"found {version or '<missing>'}"
        )

    if Path(str(report.get("overlay", ""))).resolve() != overlay_path.resolve():
        raise ValidationError("ownership report overlay path mismatch")

    required_zero = {
        "duplicate_declaration_count": report.get("duplicate_declaration_count"),
        "managed_declaration_initializer_count": report.get(
            "managed_declaration_initializer_count"
        ),
    }
    bad_zero = {key: value for key, value in required_zero.items() if value != 0}
    if bad_zero:
        raise ValidationError(f"ownership report contains unresolved declarations: {bad_zero}")

    required_one = {
        "f2a_assertion_state_process_count": report.get(
            "f2a_assertion_state_process_count"
        ),
        "f2a_assert_mode_reader_count": report.get("f2a_assert_mode_reader_count"),
        "f2a_assertion_event_task_count": report.get(
            "f2a_assertion_event_task_count"
        ),
    }
    bad_one = {key: value for key, value in required_one.items() if value != 1}
    if bad_one:
        raise ValidationError(f"ownership report uniqueness failure: {bad_one}")

    declarations = report.get("final_managed_declarations")
    if not isinstance(declarations, dict):
        raise ValidationError("ownership report lacks final_managed_declarations")

    for variable in MANAGED_DECLARATIONS:
        records = declarations.get(variable)
        if not isinstance(records, list) or len(records) != 1:
            raise ValidationError(
                f"ownership report requires one declaration for {variable}; "
                f"found {records!r}"
            )
        if records[0].get("initializer") is not None:
            raise ValidationError(
                f"ownership report retains declaration initializer for {variable}"
            )

    return report


def find_always_comb_spans(masked: str) -> list[Span]:
    spans: list[Span] = []
    for match in re.finditer(r"\balways_comb\b", masked):
        next_semicolon = masked.find(";", match.end())
        next_begin = re.search(r"\bbegin\b", masked[match.end():])
        if next_begin is None:
            continue
        begin_position = match.end() + next_begin.start()
        if next_semicolon >= 0 and next_semicolon < begin_position:
            continue
        spans.append(balanced_begin_span(masked, begin_position, "always_comb"))
    return spans


def detect_violation_owner(text: str, masked: str) -> ViolationOwner:
    variable = "f2a_oob_write_violation"
    assignments = assignments_for(text, masked, variable)

    explicit = [
        item for item in assignments if item.kind == "explicit_continuous_assignment"
    ]
    net_declarations = [
        item for item in assignments if item.kind == "net_declaration_assignment"
    ]
    variable_initializers = [
        item for item in assignments if item.kind == "variable_declaration_initializer"
    ]
    procedural = [
        item for item in assignments if item.kind == "procedural_assignment"
    ]

    owner_candidates: list[ViolationOwner] = []

    owner_candidates.extend(
        ViolationOwner(item.kind, item.line, item.statement) for item in explicit
    )
    owner_candidates.extend(
        ViolationOwner(item.kind, item.line, item.statement) for item in net_declarations
    )

    if variable_initializers:
        details = [(item.line, item.statement) for item in variable_initializers]
        raise ValidationError(
            "f2a_oob_write_violation is a dynamic predicate but uses a variable "
            f"declaration initializer: {details}"
        )

    if procedural:
        always_comb_spans = find_always_comb_spans(masked)
        containing = [
            span
            for span in always_comb_spans
            if any(span.contains(item.operator_position) for item in procedural)
        ]
        unique = {(span.start, span.stop): span for span in containing}

        outside = [
            item.line
            for item in procedural
            if not any(span.contains(item.operator_position) for span in containing)
        ]
        if outside:
            raise ValidationError(
                "f2a_oob_write_violation has procedural assignments outside "
                f"always_comb: lines={outside}"
            )

        for span in unique.values():
            owner_candidates.append(
                ViolationOwner(
                    "always_comb_process",
                    line_number(text, span.start),
                    normalize_space(text[span.start:span.stop]),
                )
            )

    if len(owner_candidates) != 1:
        details = [
            {"kind": item.kind, "line": item.line, "statement": item.statement}
            for item in owner_candidates
        ]
        raise ValidationError(
            "expected exactly one dynamic owner for f2a_oob_write_violation; "
            f"found {len(owner_candidates)}: {details}"
        )

    return owner_candidates[0]


def extract_fatal_call(text: str) -> tuple[str, int]:
    phrase_positions = [match.start() for match in re.finditer(re.escape(FATAL_PHRASE), text)]
    if len(phrase_positions) != 1:
        raise ValidationError(
            "expected exactly one out-of-bounds fatal phrase; "
            f"found {len(phrase_positions)}"
        )

    phrase_position = phrase_positions[0]
    call_start = text.rfind("$fatal", 0, phrase_position)
    if call_start < 0:
        raise ValidationError("fatal phrase is not inside a $fatal call")

    open_paren = text.find("(", call_start, phrase_position)
    if open_paren < 0:
        raise ValidationError("cannot locate $fatal opening parenthesis")

    depth = 0
    in_string = False
    escaped = False
    stop = None

    for index in range(open_paren, len(text)):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue

        if character == '"':
            in_string = True
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                semicolon = text.find(";", index)
                if semicolon < 0:
                    raise ValidationError("$fatal call has no terminating semicolon")
                stop = semicolon + 1
                break

    if stop is None:
        raise ValidationError("unterminated $fatal call")

    return normalize_space(text[call_start:stop]), call_start


def find_native_action_span(text: str, masked: str, fatal_position: int) -> Span:
    patterns = (
        re.compile(r"\bF2A_ASSERT_NATIVE\b\s*:\s*\bbegin\b", re.DOTALL),
        re.compile(r'"native"\s*:\s*\bbegin\b', re.DOTALL),
    )
    candidates: list[Span] = []

    for pattern in patterns:
        for match in pattern.finditer(masked):
            begin_match = re.search(r"\bbegin\b", masked[match.start():match.end()])
            if begin_match is None:
                continue
            begin_position = match.start() + begin_match.start()
            span = balanced_begin_span(masked, begin_position, "NATIVE action branch")
            if span.contains(fatal_position):
                candidates.append(span)

    unique = {(span.start, span.stop): span for span in candidates}
    if len(unique) != 1:
        raise ValidationError(
            "cannot resolve exactly one NATIVE action branch containing the "
            f"original fatal; found {len(unique)}"
        )
    return next(iter(unique.values()))


def audit_overlay(original_path: Path, overlay_path: Path) -> dict[str, Any]:
    original_path = require_file(original_path, "original mm_ram")
    overlay_path = require_file(overlay_path, "generated mm_ram overlay")

    original = original_path.read_text(encoding="utf-8", errors="strict")
    overlay = overlay_path.read_text(encoding="utf-8", errors="strict")
    masked = mask_comments_and_strings(overlay)

    ownership_report = audit_ownership_report(overlay_path)

    mode_reader_count = len(MODE_READER_RE.findall(overlay))
    if mode_reader_count != 1:
        raise ValidationError(
            "mode configuration reader count is not one: "
            f"found {mode_reader_count}"
        )

    for mode in MODES:
        if f'"{mode}"' not in overlay:
            raise ValidationError(f"overlay is missing mode: {mode}")

    state_span = find_state_span(overlay, masked)
    config_span = find_config_span(overlay, masked)
    task_span = find_task_span(overlay, masked)

    assignment_audit: dict[str, list[dict[str, Any]]] = {}

    for variable, owner_kind in ASSIGNMENT_OWNERS.items():
        assignments = assignments_for(overlay, masked, variable)
        if owner_kind == "state":
            allowed = [state_span]
            owner_label = "f2a_assertion_state"
        elif owner_kind == "config":
            allowed = [config_span]
            owner_label = "mode-configuration initial block"
        else:
            allowed = [config_span, task_span]
            owner_label = "event-emission subsystem"

        require_assignments_in_spans(
            assignments,
            allowed,
            variable,
            owner_label,
        )

        assignment_audit[variable] = [
            {
                "line": item.line,
                "kind": item.kind,
                "statement": normalize_space(item.statement),
            }
            for item in assignments
        ]

    assignment_audit["f2a_assert_event_file"] = audit_event_file_owner(
        overlay,
        masked,
        config_span,
    )

    violation_owner = detect_violation_owner(overlay, masked)

    original_fatal, _ = extract_fatal_call(original)
    overlay_fatal, overlay_fatal_position = extract_fatal_call(overlay)
    if original_fatal != overlay_fatal:
        raise ValidationError("NATIVE $fatal call changed from the original detector")

    native_span = find_native_action_span(
        overlay,
        masked,
        overlay_fatal_position,
    )
    native_text = overlay[native_span.start:native_span.stop]
    native_masked = masked[native_span.start:native_span.stop]

    forbidden_native_assignments = [
        normalize_space(native_text[match.start():match.end()])
        for match in FORBIDDEN_NATIVE_LHS_RE.finditer(native_masked)
    ]
    if forbidden_native_assignments:
        raise ValidationError(
            "NATIVE detector branch writes transaction/memory state: "
            f"{forbidden_native_assignments}"
        )

    if "FATAL_TERMINATION" not in native_text:
        raise ValidationError("NATIVE branch does not emit FATAL_TERMINATION")

    return {
        "overlay": str(overlay_path),
        "overlay_sha256": sha256_file(overlay_path),
        "ownership_report": str(
            overlay_path.with_name("mm_ram.stage5.ownership.json")
        ),
        "ownership_wrapper_version": ownership_report["wrapper_version"],
        "mode_configuration_reader_count": mode_reader_count,
        "event_file_reader_count": 1,
        "supported_modes": list(MODES),
        "assertion_state_block_count": 1,
        "event_task_count": 1,
        "managed_assignment_audit": assignment_audit,
        "violation_owner": {
            "kind": violation_owner.kind,
            "line": violation_owner.line,
            "statement": normalize_space(violation_owner.statement),
        },
        "violation_owner_count": 1,
        "native_original_fatal_preserved": True,
        "native_transaction_or_memory_assignments": [],
        "native_drops_write": False,
        "native_acknowledges_unsafe_transaction": False,
        "transformation_signature_count": 1,
        "native_source_guardrails_validated": True,
        "native_runtime_equivalence_validated": False,
        "native_runtime_equivalence_deferred_to_g2": True,
    }


def command_source_tokens(command_path: Path) -> list[str]:
    command = require_file(command_path, "xrun command").read_text(
        encoding="utf-8", errors="replace"
    )
    try:
        return shlex.split(command)
    except ValueError as exc:
        raise ValidationError(
            f"cannot parse command.txt: {command_path}: {exc}"
        ) from exc


def audit_compile_run(run_dir: Path, expected_kind: str) -> dict[str, Any]:
    run_dir = run_dir.expanduser().resolve()
    if not run_dir.is_dir():
        raise ValidationError(f"run directory not found: {run_dir}")

    result = load_json(run_dir / "result.json", f"{expected_kind} result")
    if result.get("phase") != "compile":
        raise ValidationError(f"{expected_kind}: phase is not compile")
    if result.get("run_kind") != expected_kind:
        raise ValidationError(f"{expected_kind}: run_kind mismatch")
    if result.get("run_purpose") != "COMPILE_CHECK":
        raise ValidationError(f"{expected_kind}: run_purpose is not COMPILE_CHECK")
    if result.get("status") != "COMPILE_PASS":
        raise ValidationError(
            f"{expected_kind}: compile result is not COMPILE_PASS: "
            f"{result.get('status')!r}"
        )
    if result.get("xrun_exit_status") != 0:
        raise ValidationError(f"{expected_kind}: xrun exit status is nonzero")

    tokens = command_source_tokens(run_dir / "command.txt")
    if "-elaborate" not in tokens:
        raise ValidationError(f"{expected_kind}: command did not use -elaborate")

    overlay_tokens = [
        token for token in tokens if Path(token).name == "mm_ram.stage5.sv"
    ]
    original_tokens = [
        token for token in tokens if Path(token).name == "mm_ram.sv"
    ]
    if len(overlay_tokens) != 1:
        raise ValidationError(
            f"{expected_kind}: expected one overlay source in command; "
            f"found {len(overlay_tokens)}"
        )
    if original_tokens:
        raise ValidationError(
            f"{expected_kind}: original mm_ram.sv and overlay were both compiled"
        )

    overlays = sorted(run_dir.rglob("mm_ram.stage5.sv"))
    if len(overlays) != 1:
        raise ValidationError(
            f"{expected_kind}: expected one retained overlay file; found {len(overlays)}"
        )
    overlay = overlays[0].resolve()
    if Path(overlay_tokens[0]).resolve() != overlay:
        raise ValidationError(
            f"{expected_kind}: command overlay does not match retained overlay"
        )

    log = require_file(run_dir / "xrun.log", f"{expected_kind} xrun log").read_text(
        encoding="utf-8", errors="replace"
    )
    forbidden = (
        "MULAXX",
        "ELBERR",
        "Multiple drivers to always_ff",
        "xmvlog: *E",
        "xmelab: *E",
        "xmsim: *E",
        "xrun: *E",
    )
    found = [marker for marker in forbidden if marker in log]
    if found:
        raise ValidationError(f"{expected_kind}: compile log errors: {found}")
    if "EXIT SUCCESS" in log:
        raise ValidationError(f"{expected_kind}: compile-only run entered simulation")

    trace_files = list(run_dir.rglob("*.trace.tsv")) + list(
        run_dir.rglob("*.trace.tsv.gz")
    )
    vcd_files = list(run_dir.rglob("*.vcd"))
    if trace_files:
        raise ValidationError(f"{expected_kind}: compile generated trace files")
    if vcd_files:
        raise ValidationError(f"{expected_kind}: compile generated VCD files")

    return {
        "run_directory": str(run_dir),
        "run_kind": expected_kind,
        "status": "COMPILE_PASS",
        "xrun_exit_status": 0,
        "overlay": str(overlay),
        "overlay_command_occurrences": 1,
        "original_mm_ram_command_occurrences": 0,
        "simulation_entered": False,
        "trace_files_generated": 0,
        "vcd_files_generated": 0,
    }


def validate_monitor_metadata(path: Path, role: str) -> dict[str, Any]:
    metadata = load_json(path, f"{role} monitor metadata")
    if metadata.get("role") != role:
        raise ValidationError(f"{role} monitor metadata role mismatch")
    if metadata.get("change_kind") != "TRACE_PATH_ONLY":
        raise ValidationError(f"{role} monitor was changed beyond trace path")
    if metadata.get("mode_adapter_appended") is not False:
        raise ValidationError(f"{role} monitor contains a second mode adapter")

    generated = require_file(Path(str(metadata["generated_monitor"])), f"{role} monitor")
    if metadata.get("generated_monitor_sha256") != sha256_file(generated):
        raise ValidationError(f"{role} monitor digest mismatch")

    text = generated.read_text(encoding="utf-8", errors="strict")
    forbidden = (
        "f2a_phase2_g1_mode_adapter",
        "F2A_PHASE2_G1_ADAPTER_BEGIN",
        "f2a_assert_mode=%s",
        "mm_ram.stage5.sv",
    )
    found = [marker for marker in forbidden if marker in text]
    if found:
        raise ValidationError(
            f"{role} monitor contains Phase-2 adapter markers: {found}"
        )
    return metadata


def write_report(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    if path.exists():
        raise ValidationError(f"refusing to overwrite report: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def selftest() -> int:
    text = r'''
module synthetic;
  string f2a_assert_event_file;
  initial begin : f2a_assertion_configuration
    if (!$value$plusargs(
          "f2a_assert_event_file=%s",
          f2a_assert_event_file
        )) begin
      $fatal(2, "missing event file");
    end
  end
endmodule
'''

    masked = mask_comments_and_strings(text)
    reader = EVENT_FILE_READER_RE.search(text)
    if reader is None:
        raise ValidationError(
            "selftest could not detect event-file plusarg reader"
        )

    initial = re.search(r"\binitial\b", masked)
    if initial is None:
        raise ValidationError("selftest could not find initial block")

    begin = find_first_begin(
        masked,
        initial.start(),
        reader.start(),
        "selftest configuration",
    )
    span = balanced_begin_span(
        masked,
        begin,
        "selftest configuration",
    )

    records = audit_event_file_owner(
        text,
        masked,
        span,
    )

    if len(records) != 1:
        raise ValidationError(
            "selftest expected one event-file ownership record"
        )

    if records[0]["kind"] != "system_function_output_argument":
        raise ValidationError(
            "selftest classified event-file ownership incorrectly"
        )

    if assignments_for(
        text,
        masked,
        "f2a_assert_event_file",
    ):
        raise ValidationError(
            "selftest incorrectly found an '=' assignment"
        )

    print("Phase2-G1 validator selftest: PASS")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv == ["--selftest"]:
        try:
            return selftest()
        except ValidationError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-mm-ram", type=Path, required=True)
    parser.add_argument("--golden-run", type=Path, required=True)
    parser.add_argument("--fault-run", type=Path, required=True)
    parser.add_argument("--golden-monitor-metadata", type=Path, required=True)
    parser.add_argument("--fault-monitor-metadata", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(raw_argv)

    try:
        original = require_file(args.original_mm_ram, "original mm_ram")
        golden_monitor = validate_monitor_metadata(args.golden_monitor_metadata, "golden")
        fault_monitor = validate_monitor_metadata(args.fault_monitor_metadata, "fault")
        golden_run = audit_compile_run(args.golden_run, "golden")
        fault_run = audit_compile_run(args.fault_run, "fault")
        golden_overlay = audit_overlay(original, Path(golden_run["overlay"]))
        fault_overlay = audit_overlay(original, Path(fault_run["overlay"]))

        if golden_overlay["overlay_sha256"] != fault_overlay["overlay_sha256"]:
            raise ValidationError("golden and fault overlays are not byte-identical")

        report = {
            "schema_version": "2.1",
            "program_version": VERSION,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "gate": "stage5_phase2_g1_clean",
            "status": "PASS",
            "original_mm_ram": str(original),
            "original_mm_ram_sha256": sha256_file(original),
            "golden_monitor": golden_monitor,
            "fault_monitor": fault_monitor,
            "golden_run": golden_run,
            "fault_run": fault_run,
            "golden_overlay_audit": golden_overlay,
            "fault_overlay_audit": fault_overlay,
            "claims": {
                "mode_configuration_infrastructure_validated": True,
                "single_mode_configuration_owner": True,
                "second_monitor_mode_adapter_present": False,
                "single_assertion_state_block": True,
                "single_event_task": True,
                "instrumentation_ownership_validated": True,
                "each_generated_source_transformed_once": True,
                "golden_and_fault_overlay_identical": True,
                "native_source_guardrails_validated": True,
                "native_original_fatal_preserved": True,
                "native_drops_write": False,
                "native_acknowledges_unsafe_transaction": False,
                "native_runtime_equivalence_validated": False,
                "native_runtime_equivalence_deferred_to_g2": True,
                "golden_compile_elaboration_passed": True,
                "fault_compile_elaboration_passed": True,
                "observe_runtime_executed": False,
                "quarantine_runtime_executed": False,
                "simulation_entered": False,
                "trace_files_generated": 0,
                "vcd_files_generated": 0,
            },
        }

        write_report(args.report, report)

    except ValidationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("Phase2-G1 clean validation: PASS")
    print(f"Report: {args.report.expanduser().resolve()}")
    print("NATIVE runtime equivalence: DEFERRED_TO_PHASE2_G2")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
