#!/usr/bin/env python3
"""Generate and fail-closed audit one Stage-5 diagnostic mm_ram overlay.

``prepare_stage5_mm_ram_impl.py`` performs the exact source transformation.
This wrapper preserves the existing command-line interface, audits both the
out-of-bounds write and read adapters, writes ``mm_ram_ownership.json``, and
augments the generator report. It never edits generated source after creation.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

WRAPPER_VERSION = "4.0.0"
IMPL_PATH = Path(__file__).resolve().with_name("prepare_stage5_mm_ram_impl.py")

MANAGED_OWNERS = {
    "f2a_assert_mode_q": "config",
    "f2a_assert_mode_name": "config",
    "f2a_assert_event_file": "config",
    "f2a_assert_event_fd": "config",
    "f2a_cycle_q": "state",
    "f2a_assert_event_count_q": "state",
    "f2a_detector_seen_q": "state",
    "f2a_write_address_allowed": "predicate",
    "f2a_oob_write_violation": "predicate",
    "f2a_oob_write_first_event": "predicate",
    "f2a_oob_read_violation": "predicate",
    "f2a_oob_read_first_event": "predicate",
}

DECLARATION_RE = re.compile(
    r"(?m)^[ \t]*"
    r"(?P<type>int(?:\s+unsigned)?|string|integer|longint\s+unsigned|logic)"
    r"[ \t]+(?P<name>f2a_[A-Za-z_$][A-Za-z0-9_$]*)"
    r"[ \t]*(?:=[ \t]*(?P<initializer>[^;]+))?;"
)
ASSIGNMENT_RE_TEMPLATE = r"\b{variable}\b\s*(?:<=|(?<![=!<>])=(?!=))"
MODE_READER_RE = re.compile(
    r'\$value\$plusargs\s*\(\s*"f2a_assert_mode=%s"\s*,',
    re.DOTALL,
)
EVENT_FILE_READER_RE = re.compile(
    r'\$value\$plusargs\s*\(\s*"f2a_assert_event_file=%s"\s*,\s*'
    r'f2a_assert_event_file\s*\)',
    re.DOTALL,
)


class OverlayError(RuntimeError):
    """Controlled overlay generation or audit failure."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_impl_module():
    if not IMPL_PATH.is_file() or IMPL_PATH.stat().st_size == 0:
        raise OverlayError(f"generator implementation is missing: {IMPL_PATH}")
    spec = importlib.util.spec_from_file_location(
        "f2a_prepare_stage5_mm_ram_impl", IMPL_PATH
    )
    if spec is None or spec.loader is None:
        raise OverlayError(f"cannot import generator implementation: {IMPL_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def mask_comments_and_strings(text: str) -> str:
    """Mask comments and strings while preserving offsets and newlines."""

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
                raise OverlayError("unterminated block comment")
            stop += 2
            for cursor in range(index, stop):
                if output[cursor] != "\n":
                    output[cursor] = " "
            index = stop
            continue

        if text[index] == '"':
            cursor = index + 1
            escaped = False
            while cursor < length:
                character = text[cursor]
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    cursor += 1
                    break
                cursor += 1
            if cursor > length or text[cursor - 1] != '"':
                raise OverlayError("unterminated string literal")
            for mark in range(index, cursor):
                if output[mark] != "\n":
                    output[mark] = " "
            index = cursor
            continue

        index += 1

    return "".join(output)


def line_number(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


def balanced_begin_span(masked: str, begin_position: int, label: str) -> tuple[int, int]:
    token_re = re.compile(r"\b(?:begin|end)\b")
    depth = 0
    saw_begin = False

    for match in token_re.finditer(masked, begin_position):
        token = match.group(0)
        if token == "begin":
            depth += 1
            saw_begin = True
        else:
            depth -= 1

        if saw_begin and depth == 0:
            return begin_position, match.end()
        if depth < 0:
            break

    raise OverlayError(f"unterminated {label} block")


def find_named_block(masked: str, label: str) -> tuple[int, int]:
    pattern = re.compile(rf"\bbegin\s*:\s*{re.escape(label)}\b")
    matches = list(pattern.finditer(masked))
    if len(matches) != 1:
        raise OverlayError(f"expected one {label} block; found {len(matches)}")
    return balanced_begin_span(masked, matches[0].start(), label)


def find_task_span(masked: str, task_name: str) -> tuple[int, int]:
    pattern = re.compile(
        rf"\btask\s+automatic\s+{re.escape(task_name)}\b"
    )
    matches = list(pattern.finditer(masked))
    if len(matches) != 1:
        raise OverlayError(f"expected one {task_name} task; found {len(matches)}")
    end_match = re.search(r"\bendtask\b", masked[matches[0].end():])
    if end_match is None:
        raise OverlayError(f"unterminated {task_name} task")
    return matches[0].start(), matches[0].end() + end_match.end()


def declarations(text: str, masked: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for match in DECLARATION_RE.finditer(masked):
        original = DECLARATION_RE.match(text, match.start())
        if original is None:
            raise OverlayError(
                f"declaration offset mismatch at line {line_number(text, match.start())}"
            )
        records.append(
            {
                "name": original.group("name"),
                "type": original.group("type"),
                "initializer": (
                    original.group("initializer").strip()
                    if original.group("initializer") is not None
                    else None
                ),
                "start": original.start(),
                "stop": original.end(),
                "line": line_number(text, original.start()),
            }
        )
    return records


def assignment_positions(
    text: str,
    masked: str,
    variable: str,
    declaration_records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    pattern = re.compile(
        ASSIGNMENT_RE_TEMPLATE.format(variable=re.escape(variable)),
        re.DOTALL,
    )
    declaration_spans = [
        (int(item["start"]), int(item["stop"]))
        for item in declaration_records
    ]
    records: list[dict[str, Any]] = []
    for match in pattern.finditer(masked):
        if any(start <= match.start() < stop for start, stop in declaration_spans):
            continue
        records.append(
            {
                "position": match.start(),
                "line": line_number(text, match.start()),
            }
        )
    return records


def in_span(position: int, span: tuple[int, int]) -> bool:
    return span[0] <= position < span[1]


def require_exact_count(text: str, token: str, expected: int, label: str) -> None:
    actual = text.count(token)
    if actual != expected:
        raise OverlayError(f"{label}: expected {expected}, found {actual}")


def audit_overlay(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    if not path.is_file() or path.stat().st_size == 0:
        raise OverlayError(f"overlay not found or empty: {path}")

    module = load_impl_module()
    text = path.read_text(encoding="utf-8", errors="strict")
    masked = mask_comments_and_strings(text)

    # Audit the exact transformation contract exported by the implementation.
    require_exact_count(text, module.MARKER, 1, "adapter marker count")
    require_exact_count(text, module.DECLARATIONS, 1, "declaration insertion count")
    require_exact_count(
        text,
        module.DIAGNOSTIC_DETECTOR_BLOCK,
        1,
        "diagnostic detector insertion count",
    )
    require_exact_count(text, module.WRITE_BRANCH_NEW, 1, "write branch replacement")
    require_exact_count(text, module.READ_FATAL_NEW, 1, "read branch replacement")
    require_exact_count(text, module.WRITE_BRANCH_OLD, 0, "old write branch residue")
    require_exact_count(text, module.ASSERTION_OLD, 0, "old write assertion residue")
    require_exact_count(text, module.READ_FATAL_OLD, 0, "old read fatal residue")

    require_exact_count(
        text,
        "begin : f2a_diagnostic_out_of_bounds_write",
        1,
        "write detector count",
    )
    require_exact_count(
        text,
        "begin : f2a_diagnostic_out_of_bounds_read",
        1,
        "read detector count",
    )
    require_exact_count(
        text,
        "task automatic f2a_emit_out_of_bounds_write_event",
        1,
        "write event task count",
    )
    require_exact_count(
        text,
        "task automatic f2a_emit_out_of_bounds_read_event",
        1,
        "read event task count",
    )
    require_exact_count(
        text,
        '$fdisplay(f2a_assert_event_fd, "\\t%s", action_name);',
        2,
        "explicit TSV action separators",
    )

    if len(MODE_READER_RE.findall(text)) != 1:
        raise OverlayError("overlay must contain exactly one mode plusarg reader")
    if len(EVENT_FILE_READER_RE.findall(text)) != 1:
        raise OverlayError("overlay must contain exactly one event-file plusarg reader")

    spans = {
        "config": find_named_block(masked, "f2a_assertion_mode_init"),
        "state": find_named_block(masked, "f2a_assertion_state"),
        "predicate": find_named_block(masked, "f2a_assertion_predicates"),
    }
    detector_spans = {
        "write": find_named_block(masked, "f2a_diagnostic_out_of_bounds_write"),
        "read": find_named_block(masked, "f2a_diagnostic_out_of_bounds_read"),
    }
    task_spans = {
        "write": find_task_span(masked, "f2a_emit_out_of_bounds_write_event"),
        "read": find_task_span(masked, "f2a_emit_out_of_bounds_read_event"),
    }

    declaration_records = declarations(text, masked)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in declaration_records:
        name = str(record["name"])
        if name in MANAGED_OWNERS:
            grouped[name].append(record)

    missing = sorted(set(MANAGED_OWNERS) - set(grouped))
    if missing:
        raise OverlayError(f"managed declarations are missing: {missing}")

    owner_report: dict[str, Any] = {}
    for variable, owner_kind in MANAGED_OWNERS.items():
        records = grouped[variable]
        if len(records) != 1:
            raise OverlayError(
                f"{variable} declaration count must be 1; found {len(records)}"
            )
        record = records[0]
        if record["initializer"] is not None:
            raise OverlayError(
                f"{variable} must not use a declaration initializer; "
                f"line={record['line']}"
            )

        assignments = assignment_positions(
            text, masked, variable, declaration_records
        )
        if not assignments:
            raise OverlayError(f"{variable} has no assignment in its owner")
        outside = [
            int(item["line"])
            for item in assignments
            if not in_span(int(item["position"]), spans[owner_kind])
        ]
        if outside:
            raise OverlayError(
                f"{variable} has assignments outside its {owner_kind} owner: "
                f"lines={outside}"
            )

        owner_report[variable] = {
            "owner": owner_kind,
            "declaration_line": int(record["line"]),
            "assignment_lines": [int(item["line"]) for item in assignments],
        }

    for task_name, task_span in task_spans.items():
        task_text = masked[task_span[0]:task_span[1]]
        task_writes = [
            variable
            for variable in MANAGED_OWNERS
            if re.search(
                ASSIGNMENT_RE_TEMPLATE.format(variable=re.escape(variable)),
                task_text,
                re.DOTALL,
            )
        ]
        if task_writes:
            raise OverlayError(
                f"{task_name} event task writes managed state: {task_writes}"
            )

    mode_reader = MODE_READER_RE.search(text)
    event_reader = EVENT_FILE_READER_RE.search(text)
    if mode_reader is None or not in_span(mode_reader.start(), spans["config"]):
        raise OverlayError("mode plusarg reader is outside configuration owner")
    if event_reader is None or not in_span(event_reader.start(), spans["config"]):
        raise OverlayError("event-file plusarg reader is outside configuration owner")

    detector_contracts = {
        "write": (
            "f2a_oob_write_first_event",
            "f2a_emit_out_of_bounds_write_event",
            {"RECORD_ONLY", "RECORD_AND_QUARANTINE"},
        ),
        "read": (
            "f2a_oob_read_first_event",
            "f2a_emit_out_of_bounds_read_event",
            {"RECORD_ONLY", "RECORD_AND_RETURN_ZERO"},
        ),
    }
    detector_report: dict[str, Any] = {}
    for detector_name, (predicate, task_name, actions) in detector_contracts.items():
        span = detector_spans[detector_name]
        detector_text = text[span[0]:span[1]]
        detector_masked = masked[span[0]:span[1]]

        if detector_masked.count(predicate) != 1:
            raise OverlayError(
                f"{detector_name} detector must consume {predicate} exactly once"
            )
        call_count = len(
            re.findall(rf"\b{re.escape(task_name)}\s*\(", detector_masked)
        )
        if call_count != 2:
            raise OverlayError(
                f"{detector_name} detector must call {task_name} twice; "
                f"found {call_count}"
            )
        missing_actions = sorted(action for action in actions if action not in detector_text)
        if missing_actions:
            raise OverlayError(
                f"{detector_name} detector is missing actions: {missing_actions}"
            )

        detector_report[detector_name] = {
            "predicate": predicate,
            "event_task": task_name,
            "event_task_call_count": call_count,
            "actions": sorted(actions),
        }

    return {
        "schema_version": "1.0",
        "wrapper_version": WRAPPER_VERSION,
        "implementation_version": getattr(module, "PROGRAM_VERSION", None),
        "kind": "stage5_diagnostic_mm_ram_ownership_audit",
        "status": "PASS",
        "overlay": str(path),
        "overlay_sha256": sha256_file(path),
        "duplicate_declaration_count": 0,
        "managed_declaration_initializer_count": 0,
        "mode_reader_count": 1,
        "event_file_reader_count": 1,
        "configuration_owner_count": 1,
        "state_owner_count": 1,
        "predicate_owner_count": 1,
        "event_task_count": 2,
        "event_task_managed_write_count": 0,
        "original_write_assertion_block_count": 0,
        "original_read_fatal_block_count": 0,
        "procedural_detector_count": 2,
        "tsv_action_separator_count": 2,
        "diagnostic_detector_implementation": "PROCEDURAL_FIRST_EVENT",
        "first_event_policy": "FIRST_VIOLATION_ONLY",
        "owners": owner_report,
        "detectors": detector_report,
    }


def run_impl(args: Sequence[str]) -> int:
    completed = subprocess.run(
        [sys.executable, str(IMPL_PATH), *args],
        check=False,
    )
    return int(completed.returncode)


def option_value(args: Sequence[str], option: str) -> str | None:
    for index, token in enumerate(args):
        if token == option and index + 1 < len(args):
            return args[index + 1]
        prefix = option + "="
        if token.startswith(prefix):
            return token[len(prefix):]
    return None


def selftest() -> int:
    module = load_impl_module()
    policy = {
        "schema_version": "1.0",
        "supported_modes": ["native", "observe", "diagnostic_quarantine"],
        "mode_contracts": {
            "native": {},
            "observe": {},
            "diagnostic_quarantine": {},
        },
        "detectors": [
            {
                "detector_id": "cv32e40p.mm_ram.out_of_bounds_write",
                "effect_hint": "ILLEGAL_MEMORY_WRITE",
                "quarantine_action": "ACKNOWLEDGE_AND_DROP_WRITE",
                "diagnostic_adapter": "MM_RAM_STAGE5_OVERLAY_V2",
            },
            {
                "detector_id": "cv32e40p.mm_ram.out_of_bounds_read",
                "effect_hint": "ILLEGAL_MEMORY_READ",
                "quarantine_action": "RETURN_ZERO_AND_CONTINUE",
                "diagnostic_adapter": "MM_RAM_STAGE5_OVERLAY_V2",
            },
        ],
    }
    source_text = (
        "module synthetic;\n"
        + module.DECLARATION_ANCHOR
        + module.WRITE_BRANCH_OLD
        + module.ASSERTION_OLD
        + module.READ_FATAL_OLD
        + "endmodule\n"
    )

    with tempfile.TemporaryDirectory(prefix="f2a_mmram_selftest_") as temporary:
        root = Path(temporary)
        source = root / "mm_ram.sv"
        policy_path = root / "policy.json"
        overlay = root / "mm_ram.stage5.sv"
        source.write_text(source_text, encoding="utf-8")
        policy_path.write_text(json.dumps(policy), encoding="utf-8")

        generated, validation = module.build_overlay(source, policy_path)
        overlay.write_text(generated, encoding="utf-8")
        report = audit_overlay(overlay)

        if validation.get("procedural_detector_count") != 2:
            raise OverlayError("implementation selftest did not produce two detectors")
        if validation.get("event_task_count") != 2:
            raise OverlayError("implementation selftest did not produce two event tasks")
        if report.get("status") != "PASS":
            raise OverlayError("ownership selftest did not pass")

    print(
        "Stage-5 diagnostic overlay selftest: PASS "
        f"wrapper={WRAPPER_VERSION} impl={module.PROGRAM_VERSION}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if args == ["--f2a-ownership-selftest"]:
        return selftest()

    if len(args) == 2 and args[0] == "--f2a-ownership-inspect":
        print(json.dumps(audit_overlay(Path(args[1])), indent=2))
        return 0

    if any(token in {"-h", "--help", "--version"} for token in args):
        return run_impl(args)

    if len(args) < 2:
        raise OverlayError("source and output arguments are required")

    output = Path(args[1]).expanduser().resolve()
    report_value = option_value(args, "--report")
    if report_value is None:
        raise OverlayError("--report is required")
    preparation_report = Path(report_value).expanduser().resolve()

    status = run_impl(args)
    if status != 0:
        return status

    audit = audit_overlay(output)
    ownership_report = preparation_report.with_name("mm_ram_ownership.json")
    if ownership_report.exists():
        raise OverlayError(f"refusing to overwrite ownership report: {ownership_report}")
    ownership_report.write_text(
        json.dumps(audit, indent=2) + "\n", encoding="utf-8"
    )

    try:
        preparation = json.loads(preparation_report.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise OverlayError(f"invalid generator preparation report: {exc}") from exc
    if not isinstance(preparation, dict):
        raise OverlayError("generator preparation report must be a JSON object")
    if preparation.get("output_sha256") != audit["overlay_sha256"]:
        raise OverlayError("generator report and ownership audit SHA mismatch")

    preparation["wrapper_version"] = WRAPPER_VERSION
    preparation["ownership_report"] = str(ownership_report)
    preparation["ownership_report_sha256"] = sha256_file(ownership_report)
    preparation["ownership_validation"] = audit
    preparation_report.write_text(
        json.dumps(preparation, indent=2) + "\n", encoding="utf-8"
    )

    print(
        "F2A_OVERLAY_OWNERSHIP: "
        f"status=PASS overlay={output} report={ownership_report}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OverlayError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
