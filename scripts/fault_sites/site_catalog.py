#!/usr/bin/env python3
"""Build Stage-1 and Stage-2 fault-site catalogs from a mapped Verilog netlist.

The program is read-only with respect to the design. It never modifies a
netlist, never selects a stuck-at polarity, and never creates fault directories.

Stage 1 enumerates deterministic raw sites, drivers, sinks, hierarchy
connections, and sequential Q/QN outputs. Stage 2 reparses the immutable
netlist, constructs a flattened combinational/hierarchical dependency graph,
excludes clock, clock-control, asynchronous reset/set, test-only, unsupported,
and unobservable sites, and emits the sites that may enter workload-activity
profiling.

Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


SCHEMA_VERSION = "1.0"
PROGRAM_VERSION = "1.1.0"
STAGE_NAME = "stage_01_raw_site_enumeration"
STAGE2_SCHEMA_VERSION = "1.0"
STAGE2_STAGE_NAME = "stage_02_static_safety_filtering"

IDENTIFIER_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
STANDARD_NUMBER_RE = re.compile(
    r"(?:\d+)?'[sS]?[bBoOdDhH][0-9a-fA-FxXzZ?_]+"
)
NORMAL_SIGNAL_RE = re.compile(
    r"[A-Za-z_$][A-Za-z0-9_$]*(?:\s*\[[^\]]+\])?"
)
ESCAPED_SIGNAL_RE = re.compile(
    r"\\[^\s,()]+(?:\s*\[[^\]]+\])?"
)
GENERATED_UNCONNECTED_RE = re.compile(
    r"^UNCONNECTED(?:_HIER_Z)?(?:[0-9]+)?$"
)


class CatalogError(RuntimeError):
    """Fatal, user-actionable catalog or safety-filter error."""


@dataclass(frozen=True)
class Connection:
    pin: str
    expression: str
    expression_start: int
    expression_end: int


@dataclass(frozen=True)
class Instance:
    module: str
    cell_type: str
    instance: str
    statement_start: int
    statement_end: int
    connections: tuple[Connection, ...]
    is_standard_cell: bool


@dataclass(frozen=True)
class PortDecl:
    name: str
    key: str
    direction: str
    width: int | None
    packed_range: str | None


@dataclass(frozen=True)
class ContinuousAssign:
    module: str
    lhs: str
    rhs: str
    statement_start: int
    statement_end: int


@dataclass
class ModuleInfo:
    name: str
    ports: list[PortDecl]
    instances: list[Instance]
    assignments: list[ContinuousAssign]


@dataclass(frozen=True)
class Endpoint:
    kind: str
    module: str
    instance: str | None
    cell_type: str | None
    pin: str | None
    role: str
    expression: str
    metadata: Mapping[str, Any]

    def payload(self) -> dict[str, Any]:
        result = asdict(self)
        result["metadata"] = dict(self.metadata)
        return result


@dataclass(frozen=True)
class FamilyRule:
    name: str
    type_pattern: re.Pattern[str]
    output_pins: frozenset[str]
    special_input_roles: Mapping[str, str]


@dataclass(frozen=True)
class Policy:
    path: Path
    sha256: str
    schema_version: str
    name: str
    standard_cell_pattern: re.Pattern[str]
    family_rules: tuple[FamilyRule, ...]
    generic_input_roles: Mapping[str, str]
    stage1_rules: Mapping[str, Any]

    def is_standard_cell(self, cell_type: str) -> bool:
        return bool(self.standard_cell_pattern.fullmatch(cell_type))

    def family(self, cell_type: str) -> FamilyRule:
        for rule in self.family_rules:
            if rule.type_pattern.search(cell_type):
                return rule
        raise CatalogError(
            f"no cell-family rule matched standard cell type {cell_type!r}"
        )

    def output_pins(self, cell_type: str) -> frozenset[str]:
        return self.family(cell_type).output_pins

    def input_role(self, cell_type: str, pin: str) -> str:
        family = self.family(cell_type)
        if pin in family.special_input_roles:
            return str(family.special_input_roles[pin])
        if pin in self.generic_input_roles:
            return str(self.generic_input_roles[pin])
        return "combinational_input"



@dataclass(frozen=True)
class ProtectedDomainRule:
    name: str
    seed_sink_roles: frozenset[str]
    seed_source_kinds: frozenset[str]
    propagate_upstream: bool
    description: str


@dataclass(frozen=True)
class SafetyPolicy:
    path: Path
    sha256: str
    schema_version: str
    name: str
    required_stage1: Mapping[str, Any]
    protected_domains: tuple[ProtectedDomainRule, ...]
    direct_exclusions: Mapping[str, Any]
    scan_test_handling: Mapping[str, Any]
    coarse_observability: Mapping[str, Any]
    stage2_rules: Mapping[str, Any]


@dataclass(frozen=True)
class DependencyGraph:
    forward: Mapping[tuple[str, str], frozenset[tuple[str, str]]]
    reverse: Mapping[tuple[str, str], frozenset[tuple[str, str]]]
    direct_sink_roles: Mapping[tuple[str, str], frozenset[str]]
    top_output_nodes: frozenset[tuple[str, str]]
    sequential_checkpoint_nodes: frozenset[tuple[str, str]]
    nodes: frozenset[tuple[str, str]]
    edge_count: int
    combinational_cell_edge_count: int
    hierarchy_edge_count: int
    continuous_assign_edge_count: int
    skipped_non_simple_count: int


@dataclass(frozen=True)
class Stage2BuildResult:
    sites: list[dict[str, Any]]
    summary: dict[str, Any]
    module_summaries: list[dict[str, Any]]
    warnings: list[str]
    graph_summary: dict[str, Any]


@dataclass(frozen=True)
class ParseDiagnostics:
    masked_block_comments: int
    masked_line_comments: int
    module_count: int
    instance_statement_count: int
    standard_cell_instance_count: int
    hierarchy_instance_count: int
    continuous_assign_count: int
    declaration_statement_count: int
    ignored_statement_count: int


@dataclass(frozen=True)
class ParsedDesign:
    modules: Mapping[str, ModuleInfo]
    diagnostics: ParseDiagnostics


@dataclass(frozen=True)
class BuildResult:
    raw_sites: list[dict[str, Any]]
    unresolved_source_nets: list[dict[str, Any]]
    dangling_driven_nets: list[dict[str, Any]]
    module_summaries: list[dict[str, Any]]
    summary: dict[str, Any]
    warnings: list[str]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_signal(expression: str) -> str:
    """Canonical key used only for connectivity matching."""
    return re.sub(r"\s+", "", expression.strip())


def is_constant(expression: str) -> bool:
    value = canonical_signal(expression)
    if not value:
        return True
    lowered = value.lower()
    if lowered in {"0", "1", "1'b0", "1'b1", "1'bx", "1'bz"}:
        return True
    if STANDARD_NUMBER_RE.fullmatch(value):
        return True
    return bool(re.fullmatch(r"\d+", value))


def is_simple_signal(expression: str) -> bool:
    value = expression.strip()
    if not value or is_constant(value):
        return False
    return bool(
        NORMAL_SIGNAL_RE.fullmatch(value)
        or ESCAPED_SIGNAL_RE.fullmatch(value)
    )


def fanout_bucket(fanout: int) -> str:
    if fanout <= 0:
        return "0"
    if fanout == 1:
        return "1"
    if fanout == 2:
        return "2"
    if fanout <= 4:
        return "3_4"
    if fanout <= 8:
        return "5_8"
    return "gt_8"


def is_generated_unconnected_placeholder(signal_key: str) -> bool:
    return bool(GENERATED_UNCONNECTED_RE.fullmatch(signal_key))


def signal_has_explicit_select(expression: str) -> bool:
    """Return True when a simple expression already selects one bit.

    Normal identifiers use ``name[index]``.  An escaped identifier ends at
    whitespace, so ``\\array_name[0] [7]`` contains an explicit packed-bit
    select while ``\\array_name[0]`` is one whole escaped identifier.
    """
    value = expression.strip()
    if value.startswith("\\"):
        return bool(re.fullmatch(r"\\\S+\s+\[[^\]]+\]", value))
    return bool(re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*\s*\[[^\]]+\]", value))


def expand_simple_signal(expression: str, width: int | None) -> list[str]:
    """Expand a whole-vector simple signal into deterministic bit selects.

    Hierarchical named-port connections frequently connect one whole vector in
    the parent module while standard-cell consumers use individual bits.  The
    expansion is required to join those two views into one connectivity graph.
    """
    value = expression.strip()
    if width is None or width <= 1 or signal_has_explicit_select(value):
        return [value]
    if value.startswith("\\"):
        return [f"{value} [{index}]" for index in range(width)]
    return [f"{value}[{index}]" for index in range(width)]


def split_top_level_commas(text: str) -> list[str]:
    result: list[str] = []
    start = 0
    brace_depth = 0
    bracket_depth = 0
    parenthesis_depth = 0
    for index, char in enumerate(text):
        if char == "{":
            brace_depth += 1
        elif char == "}":
            brace_depth -= 1
        elif char == "[":
            bracket_depth += 1
        elif char == "]":
            bracket_depth -= 1
        elif char == "(":
            parenthesis_depth += 1
        elif char == ")":
            parenthesis_depth -= 1
        elif (
            char == ","
            and brace_depth == 0
            and bracket_depth == 0
            and parenthesis_depth == 0
        ):
            result.append(text[start:index].strip())
            start = index + 1
    result.append(text[start:].strip())
    return result


def has_single_outer_braces(text: str) -> bool:
    value = text.strip()
    if len(value) < 2 or value[0] != "{" or value[-1] != "}":
        return False
    depth = 0
    for index, char in enumerate(value):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and index != len(value) - 1:
                return False
            if depth < 0:
                return False
    return depth == 0


def expand_range(base: str, msb: int, lsb: int, escaped: bool) -> list[str]:
    step = -1 if msb > lsb else 1
    indices = range(msb, lsb + step, step)
    if escaped:
        return [f"{base} [{index}]" for index in indices]
    return [f"{base}[{index}]" for index in indices]


def flatten_connection_bits(
    expression: str,
    expected_width: int | None,
) -> list[str | None]:
    """Flatten a hierarchical port expression into scalar parent signals.

    ``None`` represents a constant or open bit.  Supported forms are simple
    signals, bit/range selects, sized constants, and nested concatenations.
    Unsupported arithmetic or replication expressions fail closed instead of
    silently creating an incomplete graph.
    """
    value = expression.strip()
    if value == "":
        return [None] * (expected_width or 1)

    sized_constant = re.fullmatch(
        r"(\d+)'[sS]?[bBoOdDhH][0-9a-fA-FxXzZ?_]+",
        canonical_signal(value),
    )
    if sized_constant is not None:
        width = int(sized_constant.group(1))
        if expected_width is not None and width != expected_width:
            raise CatalogError(
                f"constant width mismatch: expression={value!r}, "
                f"expected={expected_width}, actual={width}"
            )
        return [None] * width

    if re.fullmatch(r"\d+", canonical_signal(value)):
        if expected_width is None:
            raise CatalogError(
                f"unsized constant has unknown connection width: {value!r}"
            )
        return [None] * expected_width

    if has_single_outer_braces(value):
        inner = value[1:-1].strip()
        parts = split_top_level_commas(inner)
        if len(parts) == 1 and re.match(r"^\d+\s*\{", parts[0]):
            raise CatalogError(
                f"replication concatenation is not supported in Stage 1: {value!r}"
            )
        flattened: list[str | None] = []
        for part in parts:
            flattened.extend(flatten_connection_bits(part, None))
        if expected_width is not None and len(flattened) != expected_width:
            raise CatalogError(
                f"concatenation width mismatch: expression={value!r}, "
                f"expected={expected_width}, actual={len(flattened)}"
            )
        return flattened

    normal_range = re.fullmatch(
        r"([A-Za-z_$][A-Za-z0-9_$]*)\s*\[\s*(-?\d+)\s*:\s*(-?\d+)\s*\]",
        value,
    )
    if normal_range is not None:
        bits = expand_range(
            normal_range.group(1),
            int(normal_range.group(2)),
            int(normal_range.group(3)),
            escaped=False,
        )
        if expected_width is not None and len(bits) != expected_width:
            raise CatalogError(
                f"range width mismatch: expression={value!r}, "
                f"expected={expected_width}, actual={len(bits)}"
            )
        return bits

    escaped_range = re.fullmatch(
        r"(\\\S+)\s+\[\s*(-?\d+)\s*:\s*(-?\d+)\s*\]",
        value,
    )
    if escaped_range is not None:
        bits = expand_range(
            escaped_range.group(1),
            int(escaped_range.group(2)),
            int(escaped_range.group(3)),
            escaped=True,
        )
        if expected_width is not None and len(bits) != expected_width:
            raise CatalogError(
                f"escaped range width mismatch: expression={value!r}, "
                f"expected={expected_width}, actual={len(bits)}"
            )
        return bits

    if is_simple_signal(value):
        bits = expand_simple_signal(value, expected_width)
        if expected_width is not None and len(bits) != expected_width:
            raise CatalogError(
                f"simple-signal width mismatch: expression={value!r}, "
                f"expected={expected_width}, actual={len(bits)}"
            )
        return bits

    raise CatalogError(
        f"unsupported hierarchical connection expression: {value!r}"
    )


def parse_int_range_width(packed_range: str | None) -> int | None:
    if packed_range is None:
        return 1
    match = re.fullmatch(
        r"\[\s*(-?\d+)\s*:\s*(-?\d+)\s*\]",
        packed_range,
    )
    if match is None:
        return None
    msb = int(match.group(1))
    lsb = int(match.group(2))
    return abs(msb - lsb) + 1


def mask_comments_keep_length(text: str) -> tuple[str, int, int]:
    chars = list(text)
    block_count = 0
    line_count = 0

    for match in re.finditer(r"/\*.*?\*/", text, flags=re.DOTALL):
        block_count += 1
        for index in range(match.start(), match.end()):
            if chars[index] != "\n":
                chars[index] = " "

    masked = "".join(chars)
    chars = list(masked)
    for match in re.finditer(r"//[^\n]*", masked):
        line_count += 1
        for index in range(match.start(), match.end()):
            chars[index] = " "

    return "".join(chars), block_count, line_count


def iter_semicolon_statements(
    masked_text: str,
    start: int,
    end: int,
) -> Iterator[tuple[int, int]]:
    statement_start = start
    cursor = start
    while cursor < end:
        semicolon = masked_text.find(";", cursor, end)
        if semicolon < 0:
            break
        yield statement_start, semicolon + 1
        statement_start = semicolon + 1
        cursor = semicolon + 1


def parse_named_connections(
    original_statement: str,
    masked_statement: str,
    absolute_statement_start: int,
) -> tuple[Connection, ...]:
    result: list[Connection] = []
    cursor = 0

    while cursor < len(masked_statement):
        match = re.search(
            r"\.([A-Za-z_$][A-Za-z0-9_$]*)\s*\(",
            masked_statement[cursor:],
        )
        if match is None:
            break

        pin = match.group(1)
        open_pos = cursor + match.end() - 1
        depth = 1
        index = open_pos + 1

        while index < len(masked_statement) and depth > 0:
            char = masked_statement[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            index += 1

        if depth != 0:
            raise CatalogError(f"unbalanced named connection for pin {pin}")

        raw_start = open_pos + 1
        raw_end = index - 1
        expression_start = raw_start
        while (
            expression_start < raw_end
            and original_statement[expression_start].isspace()
        ):
            expression_start += 1

        expression_end = raw_end
        while (
            expression_end > expression_start
            and original_statement[expression_end - 1].isspace()
        ):
            expression_end -= 1

        expression = original_statement[expression_start:expression_end]
        result.append(
            Connection(
                pin=pin,
                expression=expression,
                expression_start=absolute_statement_start + expression_start,
                expression_end=absolute_statement_start + expression_end,
            )
        )
        cursor = index

    return tuple(result)


def split_declaration_names(text: str) -> list[str]:
    """Split declaration names on commas outside bracket pairs."""
    result: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(text):
        if char in "[{(":
            depth += 1
        elif char in "]})" and depth > 0:
            depth -= 1
        elif char == "," and depth == 0:
            result.append(text[start:index])
            start = index + 1
    result.append(text[start:])
    return result


def parse_port_declaration(statement: str) -> list[PortDecl] | None:
    stripped = statement.strip()
    if stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()

    match = re.match(r"^(input|output|inout)\b(.*)$", stripped, flags=re.DOTALL)
    if match is None:
        return None

    direction = match.group(1)
    rest = match.group(2).strip()

    # Remove common declaration qualifiers from the beginning only.
    qualifier_pattern = re.compile(
        r"^(?:(?:wire|reg|logic|signed|unsigned)\b\s*)+",
        flags=re.IGNORECASE,
    )
    qualifier_match = qualifier_pattern.match(rest)
    if qualifier_match is not None:
        rest = rest[qualifier_match.end():].lstrip()

    packed_range: str | None = None
    range_match = re.match(r"^(\[[^\]]+\])\s*(.*)$", rest, flags=re.DOTALL)
    if range_match is not None:
        packed_range = " ".join(range_match.group(1).split())
        rest = range_match.group(2).strip()

    width = parse_int_range_width(packed_range)
    declarations: list[PortDecl] = []

    for item in split_declaration_names(rest):
        name = item.strip()
        if not name:
            continue
        if "=" in name:
            name = name.split("=", maxsplit=1)[0].strip()
        # Keep escaped names and optional unpacked dimensions verbatim except for
        # irrelevant whitespace. The canonical key removes all whitespace.
        key = canonical_signal(name)
        if not key:
            continue
        declarations.append(
            PortDecl(
                name=name,
                key=key,
                direction=direction,
                width=width,
                packed_range=packed_range,
            )
        )

    if not declarations:
        raise CatalogError(f"could not parse names from port declaration: {statement!r}")
    return declarations


def parse_continuous_assign(
    module: str,
    statement: str,
    statement_start: int,
    statement_end: int,
) -> ContinuousAssign | None:
    stripped = statement.strip()
    match = re.match(r"^assign\s+(.+?)\s*=\s*(.+?)\s*;\s*$", stripped, flags=re.DOTALL)
    if match is None:
        return None
    return ContinuousAssign(
        module=module,
        lhs=match.group(1).strip(),
        rhs=match.group(2).strip(),
        statement_start=statement_start,
        statement_end=statement_end,
    )


def parse_instance_statement(
    module: str,
    original_statement: str,
    masked_statement: str,
    statement_start: int,
    statement_end: int,
    policy: Policy,
) -> Instance | None:
    # Mapped netlists in this project use named connections and no instance
    # parameter overrides. Rejecting unsupported forms is safer than guessing.
    match = re.match(
        r"\s*([A-Za-z_$][A-Za-z0-9_$]*)\s+([^\s(]+)\s*\((.*)\)\s*;\s*$",
        masked_statement,
        flags=re.DOTALL,
    )
    if match is None:
        return None

    cell_type = match.group(1)
    instance_name = match.group(2)
    connection_start, _ = match.span(3)
    original_connections = original_statement[connection_start:match.end(3)]
    masked_connections = masked_statement[connection_start:match.end(3)]
    connections = parse_named_connections(
        original_connections,
        masked_connections,
        statement_start + connection_start,
    )
    if not connections:
        raise CatalogError(
            f"no named connections parsed for {module}/{instance_name} ({cell_type})"
        )

    return Instance(
        module=module,
        cell_type=cell_type,
        instance=instance_name,
        statement_start=statement_start,
        statement_end=statement_end,
        connections=connections,
        is_standard_cell=policy.is_standard_cell(cell_type),
    )


def parse_design(netlist_text: str, policy: Policy) -> ParsedDesign:
    masked, block_comments, line_comments = mask_comments_keep_length(netlist_text)
    module_pattern = re.compile(
        r"\bmodule\s+([^\s(]+)(?P<header>.*?)\s*;(?P<body>.*?)\bendmodule\b",
        flags=re.DOTALL,
    )

    modules: dict[str, ModuleInfo] = {}
    instance_count = 0
    standard_cell_count = 0
    hierarchy_instance_count = 0
    assignment_count = 0
    declaration_count = 0
    ignored_count = 0

    for module_match in module_pattern.finditer(masked):
        module_name = module_match.group(1)
        if module_name in modules:
            raise CatalogError(f"duplicate module definition: {module_name}")

        ports: list[PortDecl] = []
        instances: list[Instance] = []
        assignments: list[ContinuousAssign] = []
        body_start = module_match.start("body")
        body_end = module_match.end("body")

        for statement_start, statement_end in iter_semicolon_statements(
            masked, body_start, body_end
        ):
            masked_statement = masked[statement_start:statement_end]
            original_statement = netlist_text[statement_start:statement_end]
            if not masked_statement.strip():
                continue

            port_decls = parse_port_declaration(original_statement)
            if port_decls is not None:
                ports.extend(port_decls)
                declaration_count += 1
                continue

            assignment = parse_continuous_assign(
                module_name,
                original_statement,
                statement_start,
                statement_end,
            )
            if assignment is not None:
                assignments.append(assignment)
                assignment_count += 1
                continue

            instance = parse_instance_statement(
                module_name,
                original_statement,
                masked_statement,
                statement_start,
                statement_end,
                policy,
            )
            if instance is not None:
                instances.append(instance)
                instance_count += 1
                if instance.is_standard_cell:
                    standard_cell_count += 1
                else:
                    hierarchy_instance_count += 1
                continue

            # Wires, parameters, localparams, and other declarations are not
            # connectivity endpoints for Stage 1 and are counted for diagnostics.
            ignored_count += 1

        # Port keys must be unique within each module.
        duplicates = [
            key
            for key, count in Counter(port.key for port in ports).items()
            if count > 1
        ]
        if duplicates:
            raise CatalogError(
                f"duplicate port declarations in {module_name}: {duplicates[:10]}"
            )

        modules[module_name] = ModuleInfo(
            name=module_name,
            ports=ports,
            instances=instances,
            assignments=assignments,
        )

    if not modules:
        raise CatalogError("no Verilog modules were parsed")
    if standard_cell_count == 0:
        raise CatalogError("no standard-cell instances were parsed")

    # Every non-standard instance must refer to a module defined in this file.
    unknown_hierarchy_types = sorted(
        {
            instance.cell_type
            for module in modules.values()
            for instance in module.instances
            if not instance.is_standard_cell and instance.cell_type not in modules
        }
    )
    if unknown_hierarchy_types:
        raise CatalogError(
            "hierarchical instance types have no matching module definition: "
            + ", ".join(unknown_hierarchy_types[:20])
        )

    return ParsedDesign(
        modules=modules,
        diagnostics=ParseDiagnostics(
            masked_block_comments=block_comments,
            masked_line_comments=line_comments,
            module_count=len(modules),
            instance_statement_count=instance_count,
            standard_cell_instance_count=standard_cell_count,
            hierarchy_instance_count=hierarchy_instance_count,
            continuous_assign_count=assignment_count,
            declaration_statement_count=declaration_count,
            ignored_statement_count=ignored_count,
        ),
    )


def load_policy(path: Path) -> Policy:
    if not path.is_file():
        raise CatalogError(f"policy file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CatalogError(f"invalid JSON policy {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CatalogError("policy root must be a JSON object")

    required = {
        "schema_version",
        "policy_name",
        "standard_cell_type_regex",
        "cell_families",
        "generic_input_roles",
        "stage1_rules",
    }
    missing = sorted(required - payload.keys())
    if missing:
        raise CatalogError("policy missing keys: " + ", ".join(missing))

    try:
        standard_pattern = re.compile(str(payload["standard_cell_type_regex"]))
    except re.error as exc:
        raise CatalogError(f"invalid standard-cell regex: {exc}") from exc

    family_payload = payload["cell_families"]
    if not isinstance(family_payload, list) or not family_payload:
        raise CatalogError("cell_families must be a non-empty list")

    family_rules: list[FamilyRule] = []
    for index, item in enumerate(family_payload):
        if not isinstance(item, dict):
            raise CatalogError(f"cell_families[{index}] must be an object")
        try:
            name = str(item["name"])
            pattern = re.compile(str(item["type_regex"]))
            output_pins = frozenset(str(pin) for pin in item["output_pins"])
            special_roles = {
                str(pin): str(role)
                for pin, role in dict(item.get("special_input_roles", {})).items()
            }
        except (KeyError, TypeError, re.error) as exc:
            raise CatalogError(
                f"invalid cell_families[{index}]: {exc}"
            ) from exc
        if not output_pins:
            raise CatalogError(f"cell family {name!r} has no output pins")
        family_rules.append(
            FamilyRule(
                name=name,
                type_pattern=pattern,
                output_pins=output_pins,
                special_input_roles=special_roles,
            )
        )

    return Policy(
        path=path.resolve(),
        sha256=sha256_file(path),
        schema_version=str(payload["schema_version"]),
        name=str(payload["policy_name"]),
        standard_cell_pattern=standard_pattern,
        family_rules=tuple(family_rules),
        generic_input_roles={
            str(pin): str(role)
            for pin, role in dict(payload["generic_input_roles"]).items()
        },
        stage1_rules=dict(payload["stage1_rules"]),
    )


def port_match(signal_key: str, ports: Sequence[PortDecl]) -> PortDecl | None:
    matches = [
        port
        for port in ports
        if signal_key == port.key or signal_key.startswith(port.key + "[")
    ]
    if not matches:
        return None
    # Escaped array-style names require longest-prefix matching.
    return max(matches, key=lambda port: len(port.key))


def endpoint_sort_key(endpoint: Endpoint) -> tuple[str, str, str, str, str]:
    return (
        endpoint.kind,
        endpoint.instance or "",
        endpoint.cell_type or "",
        endpoint.pin or "",
        endpoint.expression,
    )


def source_kind(driver: Endpoint, policy: Policy) -> str:
    if driver.kind == "module_input":
        return "module_input"
    if driver.kind == "hierarchical_instance_output":
        return "hierarchical_module_output"
    if driver.kind == "continuous_assign":
        rhs_kind = str(driver.metadata.get("rhs_kind", "expression"))
        return f"continuous_assign_{rhs_kind}"
    if driver.kind != "standard_cell_output" or driver.cell_type is None:
        return driver.kind

    family = policy.family(driver.cell_type).name
    if family == "sequential" and driver.pin in {"Q", "QN"}:
        return "sequential_output"
    if family == "clock_gate":
        return "clock_gate_output"
    if family == "clock_buffer":
        return "clock_buffer_output"
    if family == "buffer_or_inverter":
        return "buffer_or_inverter_output"
    if family in {"full_adder", "half_adder"}:
        return "arithmetic_output"
    return "combinational_output"


def validate_standard_cell_connections(
    instance: Instance,
    policy: Policy,
) -> None:
    connected_pins = {connection.pin for connection in instance.connections}
    output_pins = policy.output_pins(instance.cell_type)
    connected_outputs = connected_pins & output_pins
    if not connected_outputs:
        raise CatalogError(
            "could not identify an output connection for standard cell "
            f"{instance.module}/{instance.instance} ({instance.cell_type}); "
            f"connected pins={sorted(connected_pins)}, "
            f"policy outputs={sorted(output_pins)}"
        )


def build_inventory(parsed: ParsedDesign, policy: Policy, top_module: str) -> BuildResult:
    if top_module not in parsed.modules:
        raise CatalogError(f"top module not found: {top_module}")

    drivers_by_module_net: dict[tuple[str, str], list[Endpoint]] = defaultdict(list)
    sinks_by_module_net: dict[tuple[str, str], list[Endpoint]] = defaultdict(list)
    display_by_module_net: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    non_simple_connections: Counter[str] = Counter()
    constant_connections: Counter[str] = Counter()
    cell_types: Counter[str] = Counter()
    cell_families: Counter[str] = Counter()
    pin_roles: Counter[str] = Counter()
    hierarchy_types: Counter[str] = Counter()
    warnings: list[str] = []

    for module_name in sorted(parsed.modules):
        module = parsed.modules[module_name]

        for instance in module.instances:
            if instance.is_standard_cell:
                validate_standard_cell_connections(instance, policy)
                cell_types[instance.cell_type] += 1
                cell_families[policy.family(instance.cell_type).name] += 1
                output_pins = policy.output_pins(instance.cell_type)

                for connection in instance.connections:
                    expression = connection.expression.strip()
                    if is_constant(expression):
                        constant_connections[
                            f"standard_cell:{connection.pin}"
                        ] += 1
                        continue
                    if not is_simple_signal(expression):
                        non_simple_connections[
                            f"standard_cell:{connection.pin}"
                        ] += 1
                        continue

                    key = (module_name, canonical_signal(expression))
                    display_by_module_net[key][expression] += 1
                    if connection.pin in output_pins:
                        drivers_by_module_net[key].append(
                            Endpoint(
                                kind="standard_cell_output",
                                module=module_name,
                                instance=instance.instance,
                                cell_type=instance.cell_type,
                                pin=connection.pin,
                                role="driver",
                                expression=expression,
                                metadata={
                                    "cell_family": policy.family(instance.cell_type).name
                                },
                            )
                        )
                    else:
                        role = policy.input_role(instance.cell_type, connection.pin)
                        pin_roles[role] += 1
                        sinks_by_module_net[key].append(
                            Endpoint(
                                kind="standard_cell_input",
                                module=module_name,
                                instance=instance.instance,
                                cell_type=instance.cell_type,
                                pin=connection.pin,
                                role=role,
                                expression=expression,
                                metadata={
                                    "cell_family": policy.family(instance.cell_type).name
                                },
                            )
                        )
                continue

            # Hierarchical module instance: pin directions come from the child
            # module's declarations, not from signal-name heuristics.
            child = parsed.modules[instance.cell_type]
            hierarchy_types[instance.cell_type] += 1
            child_ports_by_name = {port.key: port for port in child.ports}

            for connection in instance.connections:
                port = child_ports_by_name.get(canonical_signal(connection.pin))
                if port is None:
                    raise CatalogError(
                        "hierarchical connection pin not declared by child module: "
                        f"parent={module_name}, instance={instance.instance}, "
                        f"child={instance.cell_type}, pin={connection.pin}"
                    )
                expression = connection.expression.strip()
                flattened_bits = flatten_connection_bits(expression, port.width)
                for bit_position, resolved_expression in enumerate(flattened_bits):
                    if resolved_expression is None:
                        constant_connections[f"hierarchy:{port.direction}"] += 1
                        continue
                    key = (module_name, canonical_signal(resolved_expression))
                    display_by_module_net[key][resolved_expression] += 1
                    metadata = {
                        "child_module": instance.cell_type,
                        "child_port_direction": port.direction,
                        "child_port_width": port.width,
                        "vector_connection": len(flattened_bits) > 1,
                        "connection_bit_position_from_msb": (
                            bit_position if len(flattened_bits) > 1 else None
                        ),
                        "original_connection_expression": expression,
                    }

                    if port.direction in {"output", "inout"}:
                        drivers_by_module_net[key].append(
                            Endpoint(
                                kind="hierarchical_instance_output",
                                module=module_name,
                                instance=instance.instance,
                                cell_type=instance.cell_type,
                                pin=connection.pin,
                                role="driver",
                                expression=resolved_expression,
                                metadata=metadata,
                            )
                        )
                    if port.direction in {"input", "inout"}:
                        role = "hierarchical_instance_input"
                        pin_roles[role] += 1
                        sinks_by_module_net[key].append(
                            Endpoint(
                                kind="hierarchical_instance_input",
                                module=module_name,
                                instance=instance.instance,
                                cell_type=instance.cell_type,
                                pin=connection.pin,
                                role=role,
                                expression=resolved_expression,
                                metadata=metadata,
                            )
                        )

        for assignment in module.assignments:
            lhs = assignment.lhs.strip()
            rhs = assignment.rhs.strip()
            if not is_simple_signal(lhs):
                non_simple_connections["continuous_assign_lhs"] += 1
                continue

            lhs_key = (module_name, canonical_signal(lhs))
            display_by_module_net[lhs_key][lhs] += 1
            if is_constant(rhs):
                rhs_kind = "constant"
            elif is_simple_signal(rhs):
                rhs_kind = "alias"
            else:
                rhs_kind = "expression"

            drivers_by_module_net[lhs_key].append(
                Endpoint(
                    kind="continuous_assign",
                    module=module_name,
                    instance=None,
                    cell_type=None,
                    pin=None,
                    role="driver",
                    expression=lhs,
                    metadata={
                        "rhs": rhs,
                        "rhs_kind": rhs_kind,
                        "statement_start": assignment.statement_start,
                    },
                )
            )

            if rhs_kind == "alias":
                rhs_key = (module_name, canonical_signal(rhs))
                display_by_module_net[rhs_key][rhs] += 1
                pin_roles["continuous_assign_source"] += 1
                sinks_by_module_net[rhs_key].append(
                    Endpoint(
                        kind="continuous_assign_source",
                        module=module_name,
                        instance=None,
                        cell_type=None,
                        pin=None,
                        role="continuous_assign_source",
                        expression=rhs,
                        metadata={
                            "lhs": lhs,
                            "statement_start": assignment.statement_start,
                        },
                    )
                )

    # Add module-boundary semantics after all explicit connections are known.
    all_keys = set(drivers_by_module_net) | set(sinks_by_module_net)
    for module_name, signal_key in sorted(all_keys):
        module = parsed.modules[module_name]
        port = port_match(signal_key, module.ports)
        if port is None:
            continue

        display = signal_key
        display_by_module_net[(module_name, signal_key)][display] += 1
        metadata = {
            "declared_port": port.name,
            "declared_port_key": port.key,
            "declared_direction": port.direction,
            "declared_width": port.width,
        }

        if port.direction in {"input", "inout"}:
            drivers_by_module_net[(module_name, signal_key)].append(
                Endpoint(
                    kind="module_input",
                    module=module_name,
                    instance=None,
                    cell_type=None,
                    pin=port.name,
                    role="boundary_driver",
                    expression=display,
                    metadata=metadata,
                )
            )
        if port.direction in {"output", "inout"}:
            pin_roles["module_output"] += 1
            sinks_by_module_net[(module_name, signal_key)].append(
                Endpoint(
                    kind="module_output",
                    module=module_name,
                    instance=None,
                    cell_type=None,
                    pin=port.name,
                    role="module_output",
                    expression=display,
                    metadata=metadata,
                )
            )

    raw_records: list[dict[str, Any]] = []
    unresolved_records: list[dict[str, Any]] = []
    dangling_records: list[dict[str, Any]] = []

    all_keys = sorted(set(drivers_by_module_net) | set(sinks_by_module_net))
    for module_name, signal_key in all_keys:
        drivers = sorted(
            drivers_by_module_net.get((module_name, signal_key), []),
            key=endpoint_sort_key,
        )
        sinks = sorted(
            sinks_by_module_net.get((module_name, signal_key), []),
            key=endpoint_sort_key,
        )
        display_counter = display_by_module_net.get((module_name, signal_key), Counter())
        source_net = (
            sorted(display_counter.items(), key=lambda item: (-item[1], item[0]))[0][0]
            if display_counter
            else signal_key
        )

        if not drivers and sinks:
            unresolved_records.append(
                {
                    "module": module_name,
                    "source_net": source_net,
                    "source_key": signal_key,
                    "sink_count": len(sinks),
                    "sinks": [endpoint.payload() for endpoint in sinks],
                    "generated_unconnected_placeholder": (
                        is_generated_unconnected_placeholder(signal_key)
                    ),
                    "reason": "no_driver_resolved",
                }
            )
            continue

        if drivers and not sinks:
            dangling_records.append(
                {
                    "module": module_name,
                    "source_net": source_net,
                    "source_key": signal_key,
                    "driver_count": len(drivers),
                    "drivers": [endpoint.payload() for endpoint in drivers],
                    "generated_unconnected_placeholder": (
                        is_generated_unconnected_placeholder(signal_key)
                    ),
                    "reason": "no_downstream_sink_resolved",
                }
            )
            continue

        if not drivers or not sinks:
            continue

        driver_status = "unique" if len(drivers) == 1 else "multiple"
        kinds = sorted({source_kind(driver, policy) for driver in drivers})
        primary_kind = kinds[0] if len(kinds) == 1 else "mixed_driver_kinds"
        logic_sinks = [sink for sink in sinks if sink.kind != "module_output"]
        boundary_sinks = [sink for sink in sinks if sink.kind == "module_output"]
        state_site = bool(
            len(drivers) == 1
            and source_kind(drivers[0], policy) == "sequential_output"
        )
        port = port_match(signal_key, parsed.modules[module_name].ports)

        raw_records.append(
            {
                "site_key": f"{module_name}|{signal_key}",
                "module": module_name,
                "source_net": source_net,
                "source_key": signal_key,
                "source_kind": primary_kind,
                "source_kinds": kinds,
                "driver_status": driver_status,
                "driver_count": len(drivers),
                "drivers": [endpoint.payload() for endpoint in drivers],
                "state_site": state_site,
                "declared_port": (
                    None
                    if port is None
                    else {
                        "name": port.name,
                        "direction": port.direction,
                        "width": port.width,
                    }
                ),
                "logic_fanout": len(logic_sinks),
                "boundary_observer_count": len(boundary_sinks),
                "total_sink_count": len(sinks),
                "fanout_bucket": fanout_bucket(len(logic_sinks)),
                "sink_role_counts": dict(
                    sorted(Counter(sink.role for sink in sinks).items())
                ),
                "sinks": [endpoint.payload() for endpoint in sinks],
                "stage1_status": (
                    "eligible_for_stage2_inventory"
                    if len(drivers) == 1
                    else "multiple_driver_review"
                ),
            }
        )

    raw_records.sort(
        key=lambda item: (
            str(item["module"]),
            str(item["source_key"]),
        )
    )
    for index, record in enumerate(raw_records, start=1):
        record["site_id"] = f"RS{index:06d}"

    unresolved_records.sort(
        key=lambda item: (str(item["module"]), str(item["source_key"]))
    )
    dangling_records.sort(
        key=lambda item: (str(item["module"]), str(item["source_key"]))
    )

    module_summaries: list[dict[str, Any]] = []
    raw_by_module = Counter(record["module"] for record in raw_records)
    state_by_module = Counter(
        record["module"] for record in raw_records if record["state_site"]
    )
    unresolved_by_module = Counter(item["module"] for item in unresolved_records)
    dangling_by_module = Counter(item["module"] for item in dangling_records)

    instantiated_module_types = Counter(
        instance.cell_type
        for module in parsed.modules.values()
        for instance in module.instances
        if not instance.is_standard_cell
    )

    for module_name in sorted(parsed.modules):
        module = parsed.modules[module_name]
        port_direction_counts = Counter(port.direction for port in module.ports)
        port_bit_counts: dict[str, int | None] = {}
        for direction in ("input", "output", "inout"):
            matching = [port for port in module.ports if port.direction == direction]
            if any(port.width is None for port in matching):
                port_bit_counts[direction] = None
            else:
                port_bit_counts[direction] = sum(port.width or 0 for port in matching)

        standard_instances = [
            instance for instance in module.instances if instance.is_standard_cell
        ]
        hierarchy_instances = [
            instance for instance in module.instances if not instance.is_standard_cell
        ]
        module_summaries.append(
            {
                "module": module_name,
                "is_top": module_name == top_module,
                "hierarchical_instance_count_in_design": instantiated_module_types[module_name],
                "port_count_by_direction": dict(sorted(port_direction_counts.items())),
                "port_bit_count_by_direction": port_bit_counts,
                "standard_cell_instance_count": len(standard_instances),
                "hierarchy_instance_count": len(hierarchy_instances),
                "continuous_assign_count": len(module.assignments),
                "raw_site_count": raw_by_module[module_name],
                "state_site_count": state_by_module[module_name],
                "unresolved_source_count": unresolved_by_module[module_name],
                "dangling_driven_net_count": dangling_by_module[module_name],
            }
        )

    by_source_kind = Counter(str(record["source_kind"]) for record in raw_records)
    by_driver_status = Counter(str(record["driver_status"]) for record in raw_records)
    by_fanout_bucket = Counter(str(record["fanout_bucket"]) for record in raw_records)
    unique_driver_records = [
        record for record in raw_records if record["driver_status"] == "unique"
    ]

    top_ports = parsed.modules[top_module].ports
    top_pi_count = sum(1 for port in top_ports if port.direction in {"input", "inout"})
    top_po_count = sum(1 for port in top_ports if port.direction in {"output", "inout"})
    top_pi_bits = (
        None
        if any(
            port.width is None
            for port in top_ports
            if port.direction in {"input", "inout"}
        )
        else sum(
            port.width or 0
            for port in top_ports
            if port.direction in {"input", "inout"}
        )
    )
    top_po_bits = (
        None
        if any(
            port.width is None
            for port in top_ports
            if port.direction in {"output", "inout"}
        )
        else sum(
            port.width or 0
            for port in top_ports
            if port.direction in {"output", "inout"}
        )
    )

    summary = {
        "top_module": top_module,
        "top_level_port_summary": {
            "primary_input_port_count": top_pi_count,
            "primary_output_port_count": top_po_count,
            "primary_input_bit_count": top_pi_bits,
            "primary_output_bit_count": top_po_bits,
        },
        "raw_site_count": len(raw_records),
        "unique_driver_raw_site_count": len(unique_driver_records),
        "multiple_driver_raw_site_count": sum(
            1 for record in raw_records if record["driver_status"] == "multiple"
        ),
        "stage2_inventory_eligible_count": sum(
            1
            for record in raw_records
            if record["stage1_status"] == "eligible_for_stage2_inventory"
        ),
        "state_site_count": sum(1 for record in raw_records if record["state_site"]),
        "fanout_one_unique_driver_count": sum(
            1
            for record in unique_driver_records
            if int(record["logic_fanout"]) == 1
        ),
        "fanout_ge_two_unique_driver_count": sum(
            1
            for record in unique_driver_records
            if int(record["logic_fanout"]) >= 2
        ),
        "output_only_unique_driver_count": sum(
            1
            for record in unique_driver_records
            if int(record["logic_fanout"]) == 0
            and int(record["boundary_observer_count"]) > 0
        ),
        "unresolved_source_net_count": len(unresolved_records),
        "unresolved_placeholder_count": sum(
            1
            for item in unresolved_records
            if item["generated_unconnected_placeholder"]
        ),
        "unresolved_nonplaceholder_count": sum(
            1
            for item in unresolved_records
            if not item["generated_unconnected_placeholder"]
        ),
        "dangling_driven_net_count": len(dangling_records),
        "dangling_placeholder_count": sum(
            1
            for item in dangling_records
            if item["generated_unconnected_placeholder"]
        ),
        "dangling_nonplaceholder_count": sum(
            1
            for item in dangling_records
            if not item["generated_unconnected_placeholder"]
        ),
        "by_source_kind": dict(sorted(by_source_kind.items())),
        "by_driver_status": dict(sorted(by_driver_status.items())),
        "by_fanout_bucket": dict(sorted(by_fanout_bucket.items())),
        "standard_cell_types": dict(sorted(cell_types.items())),
        "standard_cell_families": dict(sorted(cell_families.items())),
        "hierarchical_module_instance_types": dict(sorted(hierarchy_types.items())),
        "sink_roles": dict(sorted(pin_roles.items())),
        "constant_connection_counts": dict(sorted(constant_connections.items())),
        "non_simple_connection_counts": dict(sorted(non_simple_connections.items())),
    }

    unresolved_nonplaceholder = summary["unresolved_nonplaceholder_count"]
    dangling_nonplaceholder = summary["dangling_nonplaceholder_count"]
    if unresolved_nonplaceholder:
        warnings.append(
            f"{unresolved_nonplaceholder} non-placeholder nets have sinks but no "
            "resolved driver; review hierarchy/alias resolution"
        )
    if dangling_nonplaceholder:
        warnings.append(
            f"{dangling_nonplaceholder} non-placeholder nets have drivers but no "
            "resolved downstream sink"
        )
    if summary["multiple_driver_raw_site_count"]:
        warnings.append(
            f"{summary['multiple_driver_raw_site_count']} raw sites have multiple drivers"
        )

    return BuildResult(
        raw_sites=raw_records,
        unresolved_source_nets=unresolved_records,
        dangling_driven_nets=dangling_records,
        module_summaries=module_summaries,
        summary=summary,
        warnings=warnings,
    )



def load_safety_policy(path: Path) -> SafetyPolicy:
    if not path.is_file():
        raise CatalogError(f"Stage-2 safety policy not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CatalogError(f"invalid Stage-2 safety-policy JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CatalogError("Stage-2 safety-policy root must be a JSON object")

    required = {
        "schema_version",
        "policy_name",
        "required_stage1",
        "protected_domains",
        "direct_exclusions",
        "scan_test_handling",
        "coarse_observability",
        "stage2_rules",
    }
    missing = sorted(required - payload.keys())
    if missing:
        raise CatalogError(
            "Stage-2 safety policy missing keys: " + ", ".join(missing)
        )

    domains_payload = payload["protected_domains"]
    if not isinstance(domains_payload, dict) or not domains_payload:
        raise CatalogError("protected_domains must be a non-empty JSON object")

    domain_rules: list[ProtectedDomainRule] = []
    for name, item in domains_payload.items():
        if not isinstance(item, dict):
            raise CatalogError(f"protected domain {name!r} must be an object")
        sink_roles = item.get("seed_sink_roles", [])
        source_kinds = item.get("seed_source_kinds", [])
        if not isinstance(sink_roles, list) or not isinstance(source_kinds, list):
            raise CatalogError(
                f"protected domain {name!r} seed lists must be arrays"
            )
        domain_rules.append(
            ProtectedDomainRule(
                name=str(name),
                seed_sink_roles=frozenset(str(role) for role in sink_roles),
                seed_source_kinds=frozenset(
                    str(kind) for kind in source_kinds
                ),
                propagate_upstream=bool(item.get("propagate_upstream", True)),
                description=str(item.get("description", "")),
            )
        )

    return SafetyPolicy(
        path=path.resolve(),
        sha256=sha256_file(path),
        schema_version=str(payload["schema_version"]),
        name=str(payload["policy_name"]),
        required_stage1=dict(payload["required_stage1"]),
        protected_domains=tuple(domain_rules),
        direct_exclusions=dict(payload["direct_exclusions"]),
        scan_test_handling=dict(payload["scan_test_handling"]),
        coarse_observability=dict(payload["coarse_observability"]),
        stage2_rules=dict(payload["stage2_rules"]),
    )


def known_stage1_roles(policy: Policy) -> set[str]:
    roles = set(str(role) for role in policy.generic_input_roles.values())
    for family in policy.family_rules:
        roles.update(str(role) for role in family.special_input_roles.values())
    roles.update(
        {
            "combinational_input",
            "hierarchical_instance_input",
            "module_output",
            "continuous_assign_source",
        }
    )
    return roles


def validate_safety_policy_against_stage1(
    safety: SafetyPolicy,
    stage1_policy: Policy,
) -> None:
    known_roles = known_stage1_roles(stage1_policy)
    requested_roles: set[str] = set()
    for domain in safety.protected_domains:
        requested_roles.update(domain.seed_sink_roles)
    requested_roles.update(
        str(role)
        for role in safety.scan_test_handling.get("flag_sink_roles", [])
    )
    requested_roles.update(
        str(role)
        for role in safety.scan_test_handling.get("test_only_sink_roles", [])
    )
    requested_roles.update(
        str(role)
        for role in safety.coarse_observability.get(
            "sequential_checkpoint_sink_roles", []
        )
    )
    unknown = sorted(requested_roles - known_roles)
    if unknown:
        raise CatalogError(
            "Stage-2 safety policy refers to unknown Stage-1 sink roles: "
            + ", ".join(unknown)
        )


def node_of(module: str, expression: str) -> tuple[str, str]:
    return module, canonical_signal(expression)


def node_to_text(node: tuple[str, str]) -> str:
    return f"{node[0]}|{node[1]}"


def add_graph_edge(
    forward: dict[tuple[str, str], set[tuple[str, str]]],
    reverse: dict[tuple[str, str], set[tuple[str, str]]],
    source: tuple[str, str],
    target: tuple[str, str],
) -> bool:
    if source == target:
        return False
    before = len(forward[source])
    forward[source].add(target)
    reverse[target].add(source)
    return len(forward[source]) != before


def build_dependency_graph(
    parsed: ParsedDesign,
    stage1_policy: Policy,
    top_module: str,
    sequential_checkpoint_roles: frozenset[str],
) -> DependencyGraph:
    if top_module not in parsed.modules:
        raise CatalogError(f"top module not found while building graph: {top_module}")

    instantiated_module_types = Counter(
        instance.cell_type
        for module in parsed.modules.values()
        for instance in module.instances
        if not instance.is_standard_cell
    )
    repeated = sorted(
        module_name
        for module_name, count in instantiated_module_types.items()
        if count > 1
    )
    if repeated:
        raise CatalogError(
            "Stage-2 module-definition graph cannot safely distinguish repeated "
            "instances of these module types: " + ", ".join(repeated[:20])
        )

    forward: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    reverse: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    direct_roles: dict[tuple[str, str], set[str]] = defaultdict(set)
    nodes: set[tuple[str, str]] = set()
    top_outputs: set[tuple[str, str]] = set()
    sequential_checkpoints: set[tuple[str, str]] = set()

    combinational_edges = 0
    hierarchy_edges = 0
    assign_edges = 0
    skipped_non_simple = 0

    for module_name in sorted(parsed.modules):
        module = parsed.modules[module_name]

        for port in module.ports:
            for bit in expand_simple_signal(port.name, port.width):
                current = node_of(module_name, bit)
                nodes.add(current)
                if module_name == top_module and port.direction in {"output", "inout"}:
                    top_outputs.add(current)

        for instance in module.instances:
            if instance.is_standard_cell:
                output_pins = stage1_policy.output_pins(instance.cell_type)
                family = stage1_policy.family(instance.cell_type).name
                input_nodes: list[tuple[str, str]] = []
                output_nodes: list[tuple[str, str]] = []

                for connection in instance.connections:
                    expression = connection.expression.strip()
                    if is_constant(expression):
                        continue
                    if not is_simple_signal(expression):
                        skipped_non_simple += 1
                        continue
                    current = node_of(module_name, expression)
                    nodes.add(current)
                    if connection.pin in output_pins:
                        output_nodes.append(current)
                    else:
                        role = stage1_policy.input_role(
                            instance.cell_type,
                            connection.pin,
                        )
                        direct_roles[current].add(role)
                        if role in sequential_checkpoint_roles:
                            sequential_checkpoints.add(current)
                        input_nodes.append(current)

                # Sequential cells terminate the combinational dependency graph.
                # D/scan/control/clock inputs do not combinationally drive Q/QN.
                if family == "sequential":
                    continue

                for source in input_nodes:
                    for target in output_nodes:
                        if add_graph_edge(forward, reverse, source, target):
                            combinational_edges += 1
                continue

            child = parsed.modules[instance.cell_type]
            child_ports = {port.key: port for port in child.ports}
            for connection in instance.connections:
                port = child_ports.get(canonical_signal(connection.pin))
                if port is None:
                    raise CatalogError(
                        "hierarchy graph found undeclared child port: "
                        f"parent={module_name}, instance={instance.instance}, "
                        f"child={instance.cell_type}, pin={connection.pin}"
                    )
                parent_bits = flatten_connection_bits(
                    connection.expression,
                    port.width,
                )
                child_bits = expand_simple_signal(port.name, port.width)
                if len(parent_bits) != len(child_bits):
                    raise CatalogError(
                        "hierarchy graph bit-count mismatch: "
                        f"parent={module_name}, instance={instance.instance}, "
                        f"pin={connection.pin}, parent_bits={len(parent_bits)}, "
                        f"child_bits={len(child_bits)}"
                    )

                for parent_bit, child_bit in zip(parent_bits, child_bits):
                    child_node = node_of(instance.cell_type, child_bit)
                    nodes.add(child_node)
                    if parent_bit is None:
                        continue
                    parent_node = node_of(module_name, parent_bit)
                    nodes.add(parent_node)
                    if port.direction in {"input", "inout"}:
                        if add_graph_edge(
                            forward,
                            reverse,
                            parent_node,
                            child_node,
                        ):
                            hierarchy_edges += 1
                    if port.direction in {"output", "inout"}:
                        if add_graph_edge(
                            forward,
                            reverse,
                            child_node,
                            parent_node,
                        ):
                            hierarchy_edges += 1

        for assignment in module.assignments:
            lhs = assignment.lhs.strip()
            rhs = assignment.rhs.strip()
            if not is_simple_signal(lhs):
                skipped_non_simple += 1
                continue
            lhs_node = node_of(module_name, lhs)
            nodes.add(lhs_node)
            if is_simple_signal(rhs):
                rhs_node = node_of(module_name, rhs)
                nodes.add(rhs_node)
                if add_graph_edge(forward, reverse, rhs_node, lhs_node):
                    assign_edges += 1
            elif not is_constant(rhs):
                skipped_non_simple += 1

    all_nodes = set(nodes) | set(forward) | set(reverse)
    for source, targets in forward.items():
        all_nodes.add(source)
        all_nodes.update(targets)
    for target, sources in reverse.items():
        all_nodes.add(target)
        all_nodes.update(sources)

    frozen_forward = {
        node: frozenset(sorted(forward.get(node, set())))
        for node in sorted(all_nodes)
    }
    frozen_reverse = {
        node: frozenset(sorted(reverse.get(node, set())))
        for node in sorted(all_nodes)
    }
    frozen_roles = {
        node: frozenset(sorted(direct_roles.get(node, set())))
        for node in sorted(all_nodes)
        if direct_roles.get(node)
    }

    return DependencyGraph(
        forward=frozen_forward,
        reverse=frozen_reverse,
        direct_sink_roles=frozen_roles,
        top_output_nodes=frozenset(sorted(top_outputs)),
        sequential_checkpoint_nodes=frozenset(
            sorted(sequential_checkpoints)
        ),
        nodes=frozenset(sorted(all_nodes)),
        edge_count=(combinational_edges + hierarchy_edges + assign_edges),
        combinational_cell_edge_count=combinational_edges,
        hierarchy_edge_count=hierarchy_edges,
        continuous_assign_edge_count=assign_edges,
        skipped_non_simple_count=skipped_non_simple,
    )


def reverse_distances(
    reverse: Mapping[tuple[str, str], frozenset[tuple[str, str]]],
    seeds: Iterable[tuple[str, str]],
) -> dict[tuple[str, str], int]:
    distances: dict[tuple[str, str], int] = {}
    queue: deque[tuple[str, str]] = deque()
    for seed in sorted(set(seeds)):
        if seed in distances:
            continue
        distances[seed] = 0
        queue.append(seed)

    while queue:
        current = queue.popleft()
        next_distance = distances[current] + 1
        for predecessor in sorted(reverse.get(current, frozenset())):
            if predecessor in distances:
                continue
            distances[predecessor] = next_distance
            queue.append(predecessor)
    return distances


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise CatalogError(f"{label} not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CatalogError(f"invalid {label} JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CatalogError(f"{label} root must be a JSON object")
    return payload


def load_and_rebuild_stage1_catalog(
    json_path: Path,
) -> tuple[
    dict[str, Any],
    Path,
    Policy,
    ParsedDesign,
    BuildResult,
]:
    payload = _load_json_object(json_path, "Stage-1 catalog")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise CatalogError(
            "Stage-1 schema mismatch: "
            f"expected={SCHEMA_VERSION}, actual={payload.get('schema_version')!r}"
        )
    if payload.get("stage") != STAGE_NAME:
        raise CatalogError(
            "Stage-1 stage mismatch: "
            f"expected={STAGE_NAME}, actual={payload.get('stage')!r}"
        )

    source = payload.get("source")
    policy_info = payload.get("policy")
    summary = payload.get("raw_site_summary")
    if not isinstance(source, dict) or not isinstance(policy_info, dict):
        raise CatalogError("Stage-1 source/policy metadata is missing")
    if not isinstance(summary, dict):
        raise CatalogError("Stage-1 raw_site_summary is missing")

    netlist = Path(str(source.get("path", ""))).resolve()
    policy_path = Path(str(policy_info.get("path", ""))).resolve()
    if not netlist.is_file():
        raise CatalogError(f"Stage-1 source netlist no longer exists: {netlist}")
    if not policy_path.is_file():
        raise CatalogError(f"Stage-1 policy no longer exists: {policy_path}")

    source_sha = sha256_file(netlist)
    policy_sha = sha256_file(policy_path)
    if source_sha != source.get("sha256"):
        raise CatalogError(
            "Stage-1 source SHA mismatch: "
            f"recorded={source.get('sha256')}, actual={source_sha}"
        )
    if policy_sha != policy_info.get("sha256"):
        raise CatalogError(
            "Stage-1 policy SHA mismatch: "
            f"recorded={policy_info.get('sha256')}, actual={policy_sha}"
        )

    raw_sites = payload.get("raw_sites")
    unresolved = payload.get("unresolved_source_nets")
    dangling = payload.get("dangling_driven_nets")
    if not isinstance(raw_sites, list):
        raise CatalogError("Stage-1 raw_sites array is missing")
    if not isinstance(unresolved, list) or not isinstance(dangling, list):
        raise CatalogError("Stage-1 diagnostic arrays are missing")

    stored_digest = inventory_digest(raw_sites, unresolved, dangling)
    recorded_digest = payload.get("inventory_digest_sha256")
    if stored_digest != recorded_digest:
        raise CatalogError(
            "Stage-1 stored digest mismatch: "
            f"recorded={recorded_digest}, recomputed={stored_digest}"
        )

    policy = load_policy(policy_path)
    text = netlist.read_text(encoding="utf-8", errors="strict")
    parsed = parse_design(text, policy)
    top_module = str(summary.get("top_module", ""))
    rebuilt = build_inventory(parsed, policy, top_module)
    rebuilt_digest = inventory_digest(
        rebuilt.raw_sites,
        rebuilt.unresolved_source_nets,
        rebuilt.dangling_driven_nets,
    )
    if rebuilt_digest != recorded_digest:
        raise CatalogError(
            "Stage-1 catalog differs from a deterministic reparse: "
            f"recorded={recorded_digest}, rebuilt={rebuilt_digest}"
        )

    return payload, netlist, policy, parsed, rebuilt


def stage2_digest(sites: Sequence[Mapping[str, Any]]) -> str:
    encoded = json.dumps(
        {"sites": list(sites)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_stage2_filter(
    *,
    stage1_payload: Mapping[str, Any],
    parsed: ParsedDesign,
    rebuilt_stage1: BuildResult,
    stage1_policy: Policy,
    safety_policy: SafetyPolicy,
) -> Stage2BuildResult:
    validate_safety_policy_against_stage1(safety_policy, stage1_policy)

    required = safety_policy.required_stage1
    if str(required.get("schema_version")) != str(
        stage1_payload.get("schema_version")
    ):
        raise CatalogError("Stage-2 policy rejects the Stage-1 schema version")
    if str(required.get("stage")) != str(stage1_payload.get("stage")):
        raise CatalogError("Stage-2 policy rejects the Stage-1 stage name")

    top_module = str(rebuilt_stage1.summary.get("top_module", ""))
    checkpoint_roles = frozenset(
        str(role)
        for role in safety_policy.coarse_observability.get(
            "sequential_checkpoint_sink_roles", []
        )
    )
    graph = build_dependency_graph(
        parsed,
        stage1_policy,
        top_module,
        checkpoint_roles,
    )

    raw_sites = rebuilt_stage1.raw_sites
    site_by_node = {
        (str(site["module"]), str(site["source_key"])): site
        for site in raw_sites
    }
    source_kind_nodes: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for node, site in site_by_node.items():
        source_kind_nodes[str(site["source_kind"])].add(node)

    domain_distances: dict[str, dict[tuple[str, str], int]] = {}
    domain_seed_counts: dict[str, int] = {}
    for domain in safety_policy.protected_domains:
        seeds: set[tuple[str, str]] = set()
        for node, roles in graph.direct_sink_roles.items():
            if roles & domain.seed_sink_roles:
                seeds.add(node)
        for kind in domain.seed_source_kinds:
            seeds.update(source_kind_nodes.get(kind, set()))
        domain_seed_counts[domain.name] = len(seeds)
        if not seeds:
            raise CatalogError(
                f"protected domain {domain.name!r} has no resolved seed nodes"
            )
        if domain.propagate_upstream:
            domain_distances[domain.name] = reverse_distances(
                graph.reverse,
                seeds,
            )
        else:
            domain_distances[domain.name] = {seed: 0 for seed in seeds}

    seq_distances = reverse_distances(
        graph.reverse,
        graph.sequential_checkpoint_nodes,
    )
    top_output_distances = reverse_distances(
        graph.reverse,
        graph.top_output_nodes,
    )

    direct_source_kinds = frozenset(
        str(kind)
        for kind in safety_policy.direct_exclusions.get("source_kinds", [])
    )
    flag_scan_roles = frozenset(
        str(role)
        for role in safety_policy.scan_test_handling.get(
            "flag_sink_roles", []
        )
    )
    test_only_roles = frozenset(
        str(role)
        for role in safety_policy.scan_test_handling.get(
            "test_only_sink_roles", []
        )
    )
    exclude_test_only = bool(
        safety_policy.scan_test_handling.get(
            "exclude_if_all_nonboundary_sinks_are_test_only",
            True,
        )
    )
    require_observable = bool(
        safety_policy.coarse_observability.get(
            "require_reachable_checkpoint",
            True,
        )
    )

    sites: list[dict[str, Any]] = []
    exclusion_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    eligible_by_source_kind: Counter[str] = Counter()
    eligible_by_fanout: Counter[str] = Counter()
    eligible_by_module: Counter[str] = Counter()
    domain_membership_counts: Counter[str] = Counter()
    scan_touch_count = 0
    state_eligible_count = 0
    warnings: list[str] = []

    for raw in raw_sites:
        node = (str(raw["module"]), str(raw["source_key"]))
        reasons: list[str] = []
        domains: list[str] = []
        domain_distance_payload: dict[str, int | None] = {}

        for domain in safety_policy.protected_domains:
            distance = domain_distances[domain.name].get(node)
            domain_distance_payload[domain.name] = distance
            if distance is not None:
                domains.append(domain.name)
                domain_membership_counts[domain.name] += 1
                reasons.append(f"protected_{domain.name}_cone")

        if raw.get("driver_status") != "unique":
            reasons.append("multiple_driver_site")

        source_kind_value = str(raw.get("source_kind"))
        if source_kind_value in direct_source_kinds:
            reasons.append(f"excluded_source_kind:{source_kind_value}")

        if bool(
            safety_policy.direct_exclusions.get(
                "generated_unconnected_placeholders",
                True,
            )
        ) and is_generated_unconnected_placeholder(str(raw.get("source_key", ""))):
            reasons.append("generated_unconnected_placeholder")

        sink_roles = {
            str(role)
            for role, count in dict(raw.get("sink_role_counts", {})).items()
            if int(count) > 0
        }
        scan_roles = sorted(sink_roles & flag_scan_roles)
        touches_scan = bool(scan_roles)
        if touches_scan:
            scan_touch_count += 1

        nonboundary_roles = sink_roles - {"module_output"}
        test_only = bool(
            exclude_test_only
            and nonboundary_roles
            and nonboundary_roles <= test_only_roles
        )
        if test_only:
            reasons.append("test_only_consumers")

        seq_distance = seq_distances.get(node)
        top_distance = top_output_distances.get(node)
        reaches_seq = seq_distance is not None
        reaches_top = top_distance is not None
        observable = reaches_seq or reaches_top
        if require_observable and not observable:
            reasons.append("no_functional_checkpoint_reachable")

        unique_reasons = sorted(set(reasons))
        status = (
            "eligible_for_activity_profile"
            if not unique_reasons
            else "excluded_by_static_filter"
        )
        status_counts[status] += 1
        for reason in unique_reasons:
            exclusion_counts[reason] += 1

        if status == "eligible_for_activity_profile":
            eligible_by_source_kind[source_kind_value] += 1
            eligible_by_fanout[str(raw.get("fanout_bucket"))] += 1
            eligible_by_module[str(raw.get("module"))] += 1
            if bool(raw.get("state_site")):
                state_eligible_count += 1

        drivers = list(raw.get("drivers", []))
        primary_driver = drivers[0] if len(drivers) == 1 else None
        nearest_distances = [
            distance
            for distance in (seq_distance, top_distance)
            if distance is not None
        ]

        sites.append(
            {
                "site_id": str(raw["site_id"]),
                "site_key": str(raw["site_key"]),
                "module": str(raw["module"]),
                "source_net": str(raw["source_net"]),
                "source_key": str(raw["source_key"]),
                "source_kind": source_kind_value,
                "state_site": bool(raw.get("state_site")),
                "driver_status": str(raw.get("driver_status")),
                "driver": primary_driver,
                "logic_fanout": int(raw.get("logic_fanout", 0)),
                "boundary_observer_count": int(
                    raw.get("boundary_observer_count", 0)
                ),
                "fanout_bucket": str(raw.get("fanout_bucket")),
                "sink_role_counts": dict(raw.get("sink_role_counts", {})),
                "static_safety": {
                    "clock_safe": not any(
                        name in domains
                        for name in ("clock_signal", "clock_control")
                    ),
                    "reset_set_safe": "async_reset_set" not in domains,
                    "scan_test_safe": not test_only,
                    "protected_domains": sorted(domains),
                    "distance_to_protected_domain": domain_distance_payload,
                    "touches_scan_structure": touches_scan,
                    "scan_sink_roles": scan_roles,
                    "test_only_consumers": test_only,
                },
                "coarse_observability": {
                    "reaches_sequential_data": reaches_seq,
                    "distance_to_nearest_sequential_data": seq_distance,
                    "reaches_top_level_output": reaches_top,
                    "distance_to_nearest_top_level_output": top_distance,
                    "nearest_checkpoint_distance": (
                        min(nearest_distances) if nearest_distances else None
                    ),
                },
                "stage2_status": status,
                "exclusion_reasons": unique_reasons,
            }
        )

    sites.sort(key=lambda item: str(item["site_id"]))

    raw_count = len(sites)
    eligible_count = status_counts["eligible_for_activity_profile"]
    excluded_count = status_counts["excluded_by_static_filter"]
    if raw_count != eligible_count + excluded_count:
        raise CatalogError("Stage-2 status accounting mismatch")

    module_summaries: list[dict[str, Any]] = []
    raw_by_module = Counter(str(site["module"]) for site in sites)
    eligible_module_counts = Counter(
        str(site["module"])
        for site in sites
        if site["stage2_status"] == "eligible_for_activity_profile"
    )
    excluded_module_counts = Counter(
        str(site["module"])
        for site in sites
        if site["stage2_status"] == "excluded_by_static_filter"
    )
    clock_module_counts = Counter(
        str(site["module"])
        for site in sites
        if any(
            domain in site["static_safety"]["protected_domains"]
            for domain in ("clock_signal", "clock_control")
        )
    )
    reset_module_counts = Counter(
        str(site["module"])
        for site in sites
        if "async_reset_set" in site["static_safety"]["protected_domains"]
    )
    for module_name in sorted(parsed.modules):
        module_summaries.append(
            {
                "module": module_name,
                "raw_site_count": raw_by_module[module_name],
                "eligible_site_count": eligible_module_counts[module_name],
                "excluded_site_count": excluded_module_counts[module_name],
                "clock_or_clock_control_cone_count": clock_module_counts[
                    module_name
                ],
                "async_reset_set_cone_count": reset_module_counts[module_name],
            }
        )

    if graph.skipped_non_simple_count:
        warnings.append(
            f"dependency graph skipped {graph.skipped_non_simple_count} "
            "non-simple connections"
        )
    if scan_touch_count:
        warnings.append(
            f"{scan_touch_count} sites touch scan pins; sites with additional "
            "functional consumers were retained and flagged"
        )

    summary = {
        "top_module": top_module,
        "raw_site_count": raw_count,
        "eligible_for_activity_profile_count": eligible_count,
        "excluded_by_static_filter_count": excluded_count,
        "eligible_state_site_count": state_eligible_count,
        "scan_touch_site_count": scan_touch_count,
        "by_status": dict(sorted(status_counts.items())),
        "exclusion_reason_counts": dict(sorted(exclusion_counts.items())),
        "protected_domain_site_counts": dict(
            sorted(domain_membership_counts.items())
        ),
        "eligible_by_source_kind": dict(
            sorted(eligible_by_source_kind.items())
        ),
        "eligible_by_fanout_bucket": dict(
            sorted(eligible_by_fanout.items())
        ),
        "eligible_by_module": dict(sorted(eligible_by_module.items())),
        "observation_checkpoint_counts": {
            "sequential_data_seed_count": len(
                graph.sequential_checkpoint_nodes
            ),
            "top_level_output_seed_count": len(graph.top_output_nodes),
            "sites_reaching_sequential_data": sum(
                1
                for site in sites
                if site["coarse_observability"]["reaches_sequential_data"]
            ),
            "sites_reaching_top_level_output": sum(
                1
                for site in sites
                if site["coarse_observability"]["reaches_top_level_output"]
            ),
        },
    }

    graph_summary = {
        "node_count": len(graph.nodes),
        "edge_count": graph.edge_count,
        "combinational_cell_edge_count": graph.combinational_cell_edge_count,
        "hierarchy_edge_count": graph.hierarchy_edge_count,
        "continuous_assign_edge_count": graph.continuous_assign_edge_count,
        "skipped_non_simple_count": graph.skipped_non_simple_count,
        "protected_domain_seed_counts": dict(sorted(domain_seed_counts.items())),
        "protected_domain_closure_counts": {
            name: len(distances)
            for name, distances in sorted(domain_distances.items())
        },
        "sequential_checkpoint_seed_count": len(
            graph.sequential_checkpoint_nodes
        ),
        "top_output_seed_count": len(graph.top_output_nodes),
    }

    return Stage2BuildResult(
        sites=sites,
        summary=summary,
        module_summaries=module_summaries,
        warnings=warnings,
        graph_summary=graph_summary,
    )


def make_stage2_payload(
    *,
    stage1_json: Path,
    stage1_payload: Mapping[str, Any],
    safety_policy: SafetyPolicy,
    build: Stage2BuildResult,
) -> dict[str, Any]:
    stage1_source = dict(stage1_payload["source"])
    stage1_policy = dict(stage1_payload["policy"])
    return {
        "schema_version": STAGE2_SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "generated_at_utc": utc_now(),
        "stage": STAGE2_STAGE_NAME,
        "design": str(stage1_payload["design"]),
        "source": stage1_source,
        "stage1_catalog": {
            "path": str(stage1_json.resolve()),
            "sha256": sha256_file(stage1_json),
            "schema_version": str(stage1_payload["schema_version"]),
            "stage": str(stage1_payload["stage"]),
            "inventory_digest_sha256": str(
                stage1_payload["inventory_digest_sha256"]
            ),
        },
        "stage1_policy": stage1_policy,
        "safety_policy": {
            "path": str(safety_policy.path),
            "sha256": safety_policy.sha256,
            "schema_version": safety_policy.schema_version,
            "name": safety_policy.name,
            "stage2_rules": dict(safety_policy.stage2_rules),
        },
        "definitions": {
            "protected_cone": (
                "a seed net that directly drives a protected sink role or has a "
                "protected source kind, plus every upstream net in the flattened "
                "combinational/hierarchical dependency graph"
            ),
            "coarse_observability": (
                "reachability to a sequential D checkpoint or top-level output; "
                "sequential cells terminate combinational traversal"
            ),
            "eligible_for_activity_profile": (
                "unique locally driven site, outside protected clock/reset/control "
                "cones, not test-only, and able to reach a functional checkpoint"
            ),
            "stage2_scope": (
                "static safety and coarse structural filtering only; no workload "
                "activity, no SA0/SA1 selection, and no netlist modification"
            ),
        },
        "graph_summary": build.graph_summary,
        "stage2_summary": build.summary,
        "module_summaries": build.module_summaries,
        "warnings": build.warnings,
        "static_filter_digest_sha256": stage2_digest(build.sites),
        "sites": build.sites,
    }


def render_stage2_report(payload: Mapping[str, Any]) -> str:
    summary = payload["stage2_summary"]
    graph = payload["graph_summary"]
    source = payload["source"]
    stage1 = payload["stage1_catalog"]
    policy = payload["safety_policy"]
    sites = payload["sites"]

    lines: list[str] = []
    lines.append("Stage 2 Static Safety and Coarse Structural Filtering")
    lines.append("=" * 80)
    lines.append(f"Design              : {payload['design']}")
    lines.append(f"Top module          : {summary['top_module']}")
    lines.append(f"Source netlist      : {source['path']}")
    lines.append(f"Source SHA256       : {source['sha256']}")
    lines.append(f"Stage-1 catalog     : {stage1['path']}")
    lines.append(f"Stage-1 digest      : {stage1['inventory_digest_sha256']}")
    lines.append(f"Safety policy       : {policy['name']}")
    lines.append(f"Safety policy SHA   : {policy['sha256']}")
    lines.append("")

    lines.append("Dependency graph")
    lines.append("-" * 80)
    lines.append(f"Nodes               : {graph['node_count']}")
    lines.append(f"Edges               : {graph['edge_count']}")
    lines.append(
        f"  combinational     : {graph['combinational_cell_edge_count']}"
    )
    lines.append(f"  hierarchy         : {graph['hierarchy_edge_count']}")
    lines.append(f"  continuous assign : {graph['continuous_assign_edge_count']}")
    lines.append(f"Skipped non-simple  : {graph['skipped_non_simple_count']}")
    lines.append("")

    lines.append("Protected-domain seeds and upstream closures")
    lines.append("-" * 80)
    seed_counts = graph["protected_domain_seed_counts"]
    closure_counts = graph["protected_domain_closure_counts"]
    width = max(len(name) for name in seed_counts)
    for name in seed_counts:
        lines.append(
            f"{name:<{width}} : seeds={seed_counts[name]:5d}  "
            f"closure={closure_counts[name]:5d}"
        )
    lines.append("")

    lines.append("Stage-2 disposition")
    lines.append("-" * 80)
    lines.append(f"Raw sites           : {summary['raw_site_count']}")
    lines.append(
        "Activity candidates : "
        f"{summary['eligible_for_activity_profile_count']}"
    )
    lines.append(
        f"Excluded            : {summary['excluded_by_static_filter_count']}"
    )
    lines.append(
        f"Eligible state sites: {summary['eligible_state_site_count']}"
    )
    lines.append(f"Scan-touch sites    : {summary['scan_touch_site_count']}")
    lines.append("")

    lines.append("Exclusion reasons")
    lines.append("-" * 80)
    reasons = summary["exclusion_reason_counts"]
    if reasons:
        reason_width = max(len(reason) for reason in reasons)
        for reason, count in reasons.items():
            lines.append(f"{reason:<{reason_width}} : {count}")
    else:
        lines.append("(none)")
    lines.append("")

    lines.append("Eligible sites by source kind")
    lines.append("-" * 80)
    values = summary["eligible_by_source_kind"]
    if values:
        value_width = max(len(name) for name in values)
        for name, count in values.items():
            lines.append(f"{name:<{value_width}} : {count}")
    else:
        lines.append("(none)")
    lines.append("")

    lines.append("Eligible sites by fanout bucket")
    lines.append("-" * 80)
    for name, count in summary["eligible_by_fanout_bucket"].items():
        lines.append(f"{name:>4} : {count}")
    lines.append("")

    checkpoints = summary["observation_checkpoint_counts"]
    lines.append("Coarse observation checkpoints")
    lines.append("-" * 80)
    lines.append(
        "Sequential-D seeds  : "
        f"{checkpoints['sequential_data_seed_count']}"
    )
    lines.append(
        "Top-output seeds    : "
        f"{checkpoints['top_level_output_seed_count']}"
    )
    lines.append(
        "Sites reaching D    : "
        f"{checkpoints['sites_reaching_sequential_data']}"
    )
    lines.append(
        "Sites reaching PO   : "
        f"{checkpoints['sites_reaching_top_level_output']}"
    )
    lines.append("")

    lines.append("Per-module disposition")
    lines.append("-" * 80)
    rows = payload["module_summaries"]
    module_width = max(len(row["module"]) for row in rows)
    for row in rows:
        lines.append(
            f"{row['module']:<{module_width}}  "
            f"raw={row['raw_site_count']:5d}  "
            f"keep={row['eligible_site_count']:5d}  "
            f"drop={row['excluded_site_count']:5d}  "
            f"clk={row['clock_or_clock_control_cone_count']:4d}  "
            f"rst={row['async_reset_set_cone_count']:4d}"
        )
    lines.append("")

    lines.append("Clock/reset safety examples")
    lines.append("-" * 80)
    protected_examples = [
        site
        for site in sites
        if site["static_safety"]["protected_domains"]
    ][:20]
    if protected_examples:
        for site in protected_examples:
            domains = ",".join(site["static_safety"]["protected_domains"])
            lines.append(
                f"{site['site_id']}  {site['site_key']}  [{domains}]"
            )
    else:
        lines.append("(none; this would indicate a safety-analysis failure)")
    lines.append("")

    lines.append("First retained activity candidates")
    lines.append("-" * 80)
    retained = [
        site
        for site in sites
        if site["stage2_status"] == "eligible_for_activity_profile"
    ][:20]
    if retained:
        for site in retained:
            obs = site["coarse_observability"]
            lines.append(
                f"{site['site_id']}  {site['site_key']}  "
                f"kind={site['source_kind']}  fanout={site['logic_fanout']}  "
                f"dD={obs['distance_to_nearest_sequential_data']}  "
                f"dPO={obs['distance_to_nearest_top_level_output']}"
            )
    else:
        lines.append("(none)")
    lines.append("")

    lines.append("Warnings")
    lines.append("-" * 80)
    warnings = payload.get("warnings", [])
    if warnings:
        for warning in warnings:
            lines.append(f"- {warning}")
    else:
        lines.append("None")
    lines.append("")

    lines.append("Stage-2 interpretation")
    lines.append("-" * 80)
    lines.append("No workload activity has been measured yet.")
    lines.append("No SA0/SA1 polarity has been selected.")
    lines.append("No fault instance or faulty netlist has been generated.")
    lines.append("Only sites marked eligible_for_activity_profile enter Stage 3.")
    lines.append("")
    return "\n".join(lines)


def run_static_filter(args: argparse.Namespace) -> int:
    stage1_json = args.stage1_json.resolve()
    safety_policy = load_safety_policy(args.safety_policy.resolve())
    (
        stage1_payload,
        _netlist,
        stage1_policy,
        parsed,
        rebuilt_stage1,
    ) = load_and_rebuild_stage1_catalog(stage1_json)

    if args.expect_stage1_digest is not None:
        expected = args.expect_stage1_digest.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise CatalogError(
                "--expect-stage1-digest must contain exactly 64 hex digits"
            )
        actual = str(stage1_payload["inventory_digest_sha256"])
        if actual != expected:
            raise CatalogError(
                "Stage-1 inventory digest mismatch: "
                f"expected={expected}, actual={actual}"
            )

    build = build_stage2_filter(
        stage1_payload=stage1_payload,
        parsed=parsed,
        rebuilt_stage1=rebuilt_stage1,
        stage1_policy=stage1_policy,
        safety_policy=safety_policy,
    )
    payload = make_stage2_payload(
        stage1_json=stage1_json,
        stage1_payload=stage1_payload,
        safety_policy=safety_policy,
        build=build,
    )
    json_text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    report_text = render_stage2_report(payload)
    atomic_write_text(args.json_output, json_text, args.force)
    atomic_write_text(args.text_output, report_text, args.force)

    summary = payload["stage2_summary"]
    graph = payload["graph_summary"]
    print(f"Stage-1 catalog     : {stage1_json}")
    print(f"Stage-1 digest      : {stage1_payload['inventory_digest_sha256']}")
    print(f"Safety policy       : {safety_policy.path}")
    print(f"Graph nodes         : {graph['node_count']}")
    print(f"Graph edges         : {graph['edge_count']}")
    print(f"Raw sites           : {summary['raw_site_count']}")
    print(
        "Activity candidates : "
        f"{summary['eligible_for_activity_profile_count']}"
    )
    print(f"Excluded            : {summary['excluded_by_static_filter_count']}")
    print(f"Stage-2 digest      : {payload['static_filter_digest_sha256']}")
    print(f"Wrote JSON          : {args.json_output.resolve()}")
    print(f"Wrote report        : {args.text_output.resolve()}")
    print("Stage-2 filtering   : PASS")
    return 0


def run_validate_static_output(args: argparse.Namespace) -> int:
    json_path = args.json.resolve()
    payload = _load_json_object(json_path, "Stage-2 catalog")
    if payload.get("schema_version") != STAGE2_SCHEMA_VERSION:
        raise CatalogError(
            "Stage-2 schema mismatch: "
            f"expected={STAGE2_SCHEMA_VERSION}, "
            f"actual={payload.get('schema_version')!r}"
        )
    if payload.get("stage") != STAGE2_STAGE_NAME:
        raise CatalogError(
            "Stage-2 stage mismatch: "
            f"expected={STAGE2_STAGE_NAME}, actual={payload.get('stage')!r}"
        )

    stage1_info = payload.get("stage1_catalog")
    safety_info = payload.get("safety_policy")
    if not isinstance(stage1_info, dict) or not isinstance(safety_info, dict):
        raise CatalogError("Stage-2 input metadata is missing")

    stage1_json = Path(str(stage1_info.get("path", ""))).resolve()
    safety_path = Path(str(safety_info.get("path", ""))).resolve()
    if not stage1_json.is_file():
        raise CatalogError(f"Stage-1 catalog no longer exists: {stage1_json}")
    if not safety_path.is_file():
        raise CatalogError(f"Stage-2 policy no longer exists: {safety_path}")
    if sha256_file(stage1_json) != stage1_info.get("sha256"):
        raise CatalogError("Stage-1 catalog file SHA changed after Stage 2")
    if sha256_file(safety_path) != safety_info.get("sha256"):
        raise CatalogError("Stage-2 safety-policy SHA changed after Stage 2")

    sites = payload.get("sites")
    if not isinstance(sites, list):
        raise CatalogError("Stage-2 sites array is missing")
    actual_ids = [str(site.get("site_id")) for site in sites]
    expected_ids = [f"RS{index:06d}" for index in range(1, len(sites) + 1)]
    if actual_ids != expected_ids:
        raise CatalogError("Stage-2 site IDs are incomplete or out of order")
    if len({str(site.get("site_key")) for site in sites}) != len(sites):
        raise CatalogError("Stage-2 duplicate site_key detected")

    for site in sites:
        status = site.get("stage2_status")
        reasons = site.get("exclusion_reasons")
        if status not in {
            "eligible_for_activity_profile",
            "excluded_by_static_filter",
        }:
            raise CatalogError(f"invalid Stage-2 status: {site.get('site_id')}")
        if not isinstance(reasons, list):
            raise CatalogError(f"missing exclusion reasons: {site.get('site_id')}")
        if status == "eligible_for_activity_profile" and reasons:
            raise CatalogError(
                f"eligible site contains exclusion reasons: {site.get('site_id')}"
            )
        if status == "excluded_by_static_filter" and not reasons:
            raise CatalogError(
                f"excluded site has no reason: {site.get('site_id')}"
            )

    recorded_digest = payload.get("static_filter_digest_sha256")
    stored_digest = stage2_digest(sites)
    if recorded_digest != stored_digest:
        raise CatalogError(
            "Stage-2 stored digest mismatch: "
            f"recorded={recorded_digest}, recomputed={stored_digest}"
        )

    (
        stage1_payload,
        _netlist,
        stage1_policy,
        parsed,
        rebuilt_stage1,
    ) = load_and_rebuild_stage1_catalog(stage1_json)
    safety_policy = load_safety_policy(safety_path)
    rebuilt = build_stage2_filter(
        stage1_payload=stage1_payload,
        parsed=parsed,
        rebuilt_stage1=rebuilt_stage1,
        stage1_policy=stage1_policy,
        safety_policy=safety_policy,
    )
    rebuilt_digest = stage2_digest(rebuilt.sites)
    if rebuilt_digest != recorded_digest:
        raise CatalogError(
            "recomputed Stage-2 filter differs from stored JSON: "
            f"stored={recorded_digest}, rebuilt={rebuilt_digest}"
        )

    summary = payload.get("stage2_summary")
    if not isinstance(summary, dict):
        raise CatalogError("Stage-2 summary is missing")
    eligible = sum(
        1
        for site in sites
        if site["stage2_status"] == "eligible_for_activity_profile"
    )
    excluded = len(sites) - eligible
    if eligible != summary.get("eligible_for_activity_profile_count"):
        raise CatalogError("Stage-2 eligible count does not match site records")
    if excluded != summary.get("excluded_by_static_filter_count"):
        raise CatalogError("Stage-2 excluded count does not match site records")

    print(f"Stage-2 JSON        : {json_path}")
    print(f"Stage-1 digest      : {stage1_payload['inventory_digest_sha256']}")
    print(f"Safety policy SHA   : {safety_policy.sha256}")
    print(f"Sites               : {len(sites)}")
    print(f"Activity candidates : {eligible}")
    print(f"Excluded            : {excluded}")
    print(f"Stage-2 digest      : {recorded_digest}")
    print("Stage-2 validation : PASS")
    return 0


def run_self_test_stage2(args: argparse.Namespace) -> int:
    stage1_policy = load_policy(args.policy.resolve())
    safety_policy = load_safety_policy(args.safety_policy.resolve())
    synthetic = r"""
module clock_leaf(clk_i, en_i, rst_ni, d_i, q_o);
  input clk_i, en_i, rst_ni, d_i;
  output q_o;
  wire gated_clk, safe_data, clock_ctrl;
  AND2_X1 en_logic(.A1(en_i), .A2(d_i), .ZN(clock_ctrl));
  CLKGATETST_X1 cg(.CK(clk_i), .E(clock_ctrl), .SE(1'b0), .GCK(gated_clk));
  AND2_X1 data_logic(.A1(d_i), .A2(en_i), .ZN(safe_data));
  DFFR_X1 state(.D(safe_data), .RN(rst_ni), .CK(gated_clk), .Q(q_o), .QN());
endmodule

module top(clk_i, rst_ni, en_i, d_i, q_o);
  input clk_i, rst_ni, en_i, d_i;
  output q_o;
  clock_leaf u_leaf(
    .clk_i(clk_i), .en_i(en_i), .rst_ni(rst_ni), .d_i(d_i), .q_o(q_o)
  );
endmodule
"""
    parsed = parse_design(synthetic, stage1_policy)
    stage1_build = build_inventory(parsed, stage1_policy, "top")
    stage1_payload = {
        "schema_version": SCHEMA_VERSION,
        "stage": STAGE_NAME,
        "design": "self_test",
        "source": {"path": "synthetic", "sha256": "synthetic", "size_bytes": 0},
        "policy": {
            "path": str(stage1_policy.path),
            "sha256": stage1_policy.sha256,
            "schema_version": stage1_policy.schema_version,
            "name": stage1_policy.name,
            "stage1_rules": dict(stage1_policy.stage1_rules),
        },
        "inventory_digest_sha256": inventory_digest(
            stage1_build.raw_sites,
            stage1_build.unresolved_source_nets,
            stage1_build.dangling_driven_nets,
        ),
    }
    stage2 = build_stage2_filter(
        stage1_payload=stage1_payload,
        parsed=parsed,
        rebuilt_stage1=stage1_build,
        stage1_policy=stage1_policy,
        safety_policy=safety_policy,
    )
    by_key = {site["site_key"]: site for site in stage2.sites}
    assert by_key["clock_leaf|gated_clk"]["stage2_status"] == (
        "excluded_by_static_filter"
    )
    assert "protected_clock_signal_cone" in by_key[
        "clock_leaf|gated_clk"
    ]["exclusion_reasons"]
    assert "protected_clock_control_cone" in by_key[
        "clock_leaf|clock_ctrl"
    ]["exclusion_reasons"]
    assert "protected_async_reset_set_cone" in by_key[
        "clock_leaf|rst_ni"
    ]["exclusion_reasons"]
    assert by_key["clock_leaf|safe_data"]["stage2_status"] == (
        "eligible_for_activity_profile"
    )

    print(f"Stage-1 policy      : {stage1_policy.path}")
    print(f"Stage-2 policy      : {safety_policy.path}")
    print(f"Raw sites           : {stage2.summary['raw_site_count']}")
    print(
        "Activity candidates : "
        f"{stage2.summary['eligible_for_activity_profile_count']}"
    )
    print(
        f"Excluded            : {stage2.summary['excluded_by_static_filter_count']}"
    )
    print("Stage-2 self-test   : PASS")
    return 0


def atomic_write_text(path: Path, text: str, force: bool) -> None:
    path = path.resolve()
    if path.exists() and not force:
        raise CatalogError(
            f"output already exists: {path}; use --force to replace it intentionally"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".tmp.",
        dir=str(path.parent),
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o644)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def render_report(payload: Mapping[str, Any]) -> str:
    parser = payload["parser_summary"]
    summary = payload["raw_site_summary"]
    source = payload["source"]
    policy = payload["policy"]

    lines: list[str] = []
    lines.append("Stage 1 Raw Site Enumeration")
    lines.append("=" * 80)
    lines.append(f"Design              : {payload['design']}")
    lines.append(f"Top module          : {summary['top_module']}")
    lines.append(f"Source netlist      : {source['path']}")
    lines.append(f"Source SHA256       : {source['sha256']}")
    lines.append(f"Policy              : {policy['name']}")
    lines.append(f"Policy SHA256       : {policy['sha256']}")
    lines.append("")

    lines.append("Parser summary")
    lines.append("-" * 80)
    lines.append(f"Modules             : {parser['module_count']}")
    lines.append(f"All instances       : {parser['instance_statement_count']}")
    lines.append(f"Standard cells      : {parser['standard_cell_instance_count']}")
    lines.append(f"Hierarchy instances : {parser['hierarchy_instance_count']}")
    lines.append(f"Continuous assigns  : {parser['continuous_assign_count']}")
    lines.append("")

    top_ports = summary["top_level_port_summary"]
    lines.append("Top-level interface")
    lines.append("-" * 80)
    lines.append(
        f"PI ports / bits     : {top_ports['primary_input_port_count']} / "
        f"{top_ports['primary_input_bit_count']}"
    )
    lines.append(
        f"PO ports / bits     : {top_ports['primary_output_port_count']} / "
        f"{top_ports['primary_output_bit_count']}"
    )
    lines.append("")

    lines.append("Raw-site summary")
    lines.append("-" * 80)
    lines.append(f"Raw sites           : {summary['raw_site_count']}")
    lines.append(f"Unique-driver sites : {summary['unique_driver_raw_site_count']}")
    lines.append(f"Stage-2 inventory   : {summary['stage2_inventory_eligible_count']}")
    lines.append(f"State Q/QN sites    : {summary['state_site_count']}")
    lines.append(f"Fanout = 1          : {summary['fanout_one_unique_driver_count']}")
    lines.append(f"Fanout >= 2         : {summary['fanout_ge_two_unique_driver_count']}")
    lines.append(f"Output-only sites   : {summary['output_only_unique_driver_count']}")
    lines.append(f"Multiple drivers    : {summary['multiple_driver_raw_site_count']}")
    lines.append(f"Unresolved sources  : {summary['unresolved_source_net_count']}")
    lines.append(f"  placeholders      : {summary['unresolved_placeholder_count']}")
    lines.append(f"  non-placeholders  : {summary['unresolved_nonplaceholder_count']}")
    lines.append(f"Dangling drivers    : {summary['dangling_driven_net_count']}")
    lines.append(f"  placeholders      : {summary['dangling_placeholder_count']}")
    lines.append(f"  non-placeholders  : {summary['dangling_nonplaceholder_count']}")
    lines.append("")

    for title, key in (
        ("By source kind", "by_source_kind"),
        ("By fanout bucket", "by_fanout_bucket"),
        ("Standard-cell families", "standard_cell_families"),
        ("Sink roles", "sink_roles"),
    ):
        lines.append(title)
        lines.append("-" * 80)
        values = summary[key]
        if not values:
            lines.append("(none)")
        else:
            width = max(len(str(name)) for name in values)
            for name, count in values.items():
                lines.append(f"{name:<{width}} : {count}")
        lines.append("")

    lines.append("Per-module raw-site counts")
    lines.append("-" * 80)
    module_rows = payload["module_summaries"]
    width = max(len(row["module"]) for row in module_rows)
    for row in module_rows:
        lines.append(
            f"{row['module']:<{width}}  "
            f"cells={row['standard_cell_instance_count']:5d}  "
            f"raw={row['raw_site_count']:5d}  "
            f"state={row['state_site_count']:4d}  "
            f"unresolved={row['unresolved_source_count']:3d}  "
            f"dangling={row['dangling_driven_net_count']:3d}"
        )
    lines.append("")

    lines.append("Warnings")
    lines.append("-" * 80)
    warnings = payload.get("warnings", [])
    if warnings:
        for warning in warnings:
            lines.append(f"- {warning}")
    else:
        lines.append("None")
    lines.append("")

    lines.append("Stage-1 interpretation")
    lines.append("-" * 80)
    lines.append("This output is a structural inventory only.")
    lines.append("No site is clock/reset/scan safe merely because it appears here.")
    lines.append("No SA0/SA1 polarity has been selected.")
    lines.append("No netlist has been modified.")
    lines.append("")
    return "\n".join(lines)


def inventory_digest(
    raw_sites: Sequence[Mapping[str, Any]],
    unresolved_source_nets: Sequence[Mapping[str, Any]],
    dangling_driven_nets: Sequence[Mapping[str, Any]],
) -> str:
    stable = {
        "raw_sites": list(raw_sites),
        "unresolved_source_nets": list(unresolved_source_nets),
        "dangling_driven_nets": list(dangling_driven_nets),
    }
    encoded = json.dumps(
        stable,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def make_payload(
    *,
    design: str,
    netlist: Path,
    netlist_sha256: str,
    policy: Policy,
    parsed: ParsedDesign,
    build: BuildResult,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "generated_at_utc": utc_now(),
        "stage": STAGE_NAME,
        "design": design,
        "source": {
            "path": str(netlist.resolve()),
            "sha256": netlist_sha256,
            "size_bytes": netlist.stat().st_size,
        },
        "policy": {
            "path": str(policy.path),
            "sha256": policy.sha256,
            "schema_version": policy.schema_version,
            "name": policy.name,
            "stage1_rules": dict(policy.stage1_rules),
        },
        "definitions": {
            "raw_site": (
                "one canonical net within one module that has at least one "
                "resolved driver and at least one resolved downstream sink or "
                "module-output observer"
            ),
            "logic_fanout": (
                "resolved downstream sinks excluding a module-output observer"
            ),
            "state_site": (
                "a unique standard-cell sequential Q/QN output; this is an "
                "inventory label, not injection approval"
            ),
            "stage1_status": (
                "eligible_for_stage2_inventory means unique driver only; "
                "clock/reset/scan safety is not evaluated in Stage 1"
            ),
        },
        "parser_summary": asdict(parsed.diagnostics),
        "raw_site_summary": build.summary,
        "module_summaries": build.module_summaries,
        "warnings": build.warnings,
        "inventory_digest_sha256": inventory_digest(
            build.raw_sites,
            build.unresolved_source_nets,
            build.dangling_driven_nets,
        ),
        "raw_sites": build.raw_sites,
        "unresolved_source_nets": build.unresolved_source_nets,
        "dangling_driven_nets": build.dangling_driven_nets,
    }


def validate_expectations(
    *,
    actual_sha256: str,
    parsed: ParsedDesign,
    expected_sha256: str | None,
    expected_module_count: int | None,
    expected_standard_cell_count: int | None,
) -> None:
    if expected_sha256 is not None:
        normalized = expected_sha256.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise CatalogError("--expect-sha256 must contain exactly 64 hex digits")
        if actual_sha256 != normalized:
            raise CatalogError(
                "source-netlist SHA256 mismatch: "
                f"expected={normalized}, actual={actual_sha256}"
            )

    actual_modules = parsed.diagnostics.module_count
    if (
        expected_module_count is not None
        and actual_modules != expected_module_count
    ):
        raise CatalogError(
            "module-count mismatch: "
            f"expected={expected_module_count}, actual={actual_modules}"
        )

    actual_cells = parsed.diagnostics.standard_cell_instance_count
    if (
        expected_standard_cell_count is not None
        and actual_cells != expected_standard_cell_count
    ):
        raise CatalogError(
            "standard-cell-count mismatch: "
            f"expected={expected_standard_cell_count}, actual={actual_cells}"
        )


def run_enumerate(args: argparse.Namespace) -> int:
    netlist = args.netlist.resolve()
    policy_path = args.policy.resolve()
    if not netlist.is_file():
        raise CatalogError(f"netlist not found: {netlist}")
    if netlist.stat().st_size == 0:
        raise CatalogError(f"netlist is empty: {netlist}")

    policy = load_policy(policy_path)
    netlist_sha = sha256_file(netlist)
    text = netlist.read_text(encoding="utf-8", errors="strict")
    parsed = parse_design(text, policy)
    validate_expectations(
        actual_sha256=netlist_sha,
        parsed=parsed,
        expected_sha256=args.expect_sha256,
        expected_module_count=args.expect_module_count,
        expected_standard_cell_count=args.expect_standard_cell_count,
    )
    build = build_inventory(parsed, policy, args.top_module)
    payload = make_payload(
        design=args.design,
        netlist=netlist,
        netlist_sha256=netlist_sha,
        policy=policy,
        parsed=parsed,
        build=build,
    )

    json_text = json.dumps(payload, indent=2, sort_keys=False) + "\n"
    report_text = render_report(payload)
    atomic_write_text(args.json_output, json_text, args.force)
    atomic_write_text(args.text_output, report_text, args.force)

    print(f"Stage              : {STAGE_NAME}")
    print(f"Source netlist     : {netlist}")
    print(f"Source SHA256      : {netlist_sha}")
    print(f"Modules            : {parsed.diagnostics.module_count}")
    print(f"Standard cells     : {parsed.diagnostics.standard_cell_instance_count}")
    print(f"Raw sites          : {build.summary['raw_site_count']}")
    print(f"Stage-2 inventory  : {build.summary['stage2_inventory_eligible_count']}")
    print(f"State sites        : {build.summary['state_site_count']}")
    print(f"JSON output        : {args.json_output.resolve()}")
    print(f"Text report        : {args.text_output.resolve()}")
    print("Stage-1 enumeration: PASS")
    return 0


def run_self_test(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy.resolve())
    synthetic = r"""
module leaf(clk, rst_n, a, b, q, y);
  input clk, rst_n, a, b;
  output q, y;
  wire n1, q, y;
  DFFR_X1 state_q_reg(.D(a), .RN(rst_n), .CK(clk), .Q(q), .QN());
  AND2_X1 g1(.A1(q), .A2(b), .ZN(n1));
  INV_X1 g2(.A(n1), .ZN(y));
endmodule

module top(clk, rst_n, a, b, y);
  input clk, rst_n, a, b;
  output y;
  wire q;
  leaf u_leaf(.clk(clk), .rst_n(rst_n), .a(a), .b(b), .q(q), .y(y));
endmodule
"""
    parsed = parse_design(synthetic, policy)
    build = build_inventory(parsed, policy, "top")

    assert parsed.diagnostics.module_count == 2
    assert parsed.diagnostics.standard_cell_instance_count == 3
    assert parsed.diagnostics.hierarchy_instance_count == 1
    assert any(
        site["module"] == "leaf"
        and site["source_key"] == "q"
        and site["state_site"] is True
        for site in build.raw_sites
    )
    assert any(
        site["module"] == "top"
        and site["source_key"] == "y"
        and site["source_kind"] == "hierarchical_module_output"
        for site in build.raw_sites
    )
    assert not any(
        site["stage1_status"] == "multiple_driver_review"
        for site in build.raw_sites
    )
    assert expand_simple_signal("bus", 3) == ["bus[0]", "bus[1]", "bus[2]"]
    assert expand_simple_signal("bus[2]", 8) == ["bus[2]"]
    assert expand_simple_signal(r"\escaped_array[0]", 2) == [
        r"\escaped_array[0] [0]",
        r"\escaped_array[0] [1]",
    ]
    assert flatten_connection_bits("{unused, bus[1:0]}", 3) == [
        "unused",
        "bus[1]",
        "bus[0]",
    ]
    assert flatten_connection_bits("{2'b00, bus[1:0]}", 4) == [
        None,
        None,
        "bus[1]",
        "bus[0]",
    ]

    print(f"Policy             : {policy.path}")
    print(f"Modules            : {parsed.diagnostics.module_count}")
    print(f"Standard cells     : {parsed.diagnostics.standard_cell_instance_count}")
    print(f"Hierarchy instances: {parsed.diagnostics.hierarchy_instance_count}")
    print(f"Raw sites          : {build.summary['raw_site_count']}")
    print("Internal self-test : PASS")
    return 0


def run_validate_output(args: argparse.Namespace) -> int:
    json_path = args.json.resolve()
    if not json_path.is_file():
        raise CatalogError(f"catalog JSON not found: {json_path}")
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CatalogError(f"invalid catalog JSON {json_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CatalogError("catalog JSON root must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise CatalogError(
            f"schema mismatch: expected={SCHEMA_VERSION}, "
            f"actual={payload.get('schema_version')!r}"
        )
    if payload.get("stage") != STAGE_NAME:
        raise CatalogError(
            f"stage mismatch: expected={STAGE_NAME}, "
            f"actual={payload.get('stage')!r}"
        )

    source = payload.get("source")
    policy_info = payload.get("policy")
    if not isinstance(source, dict) or not isinstance(policy_info, dict):
        raise CatalogError("catalog source/policy metadata is missing")
    netlist = Path(str(source.get("path", ""))).resolve()
    policy_path = Path(str(policy_info.get("path", ""))).resolve()
    if not netlist.is_file():
        raise CatalogError(f"catalog source netlist no longer exists: {netlist}")
    if not policy_path.is_file():
        raise CatalogError(f"catalog policy no longer exists: {policy_path}")
    actual_source_sha = sha256_file(netlist)
    actual_policy_sha = sha256_file(policy_path)
    if actual_source_sha != source.get("sha256"):
        raise CatalogError(
            "catalog source SHA mismatch: "
            f"recorded={source.get('sha256')}, actual={actual_source_sha}"
        )
    if actual_policy_sha != policy_info.get("sha256"):
        raise CatalogError(
            "catalog policy SHA mismatch: "
            f"recorded={policy_info.get('sha256')}, actual={actual_policy_sha}"
        )

    raw_sites = payload.get("raw_sites")
    unresolved = payload.get("unresolved_source_nets")
    dangling = payload.get("dangling_driven_nets")
    if not isinstance(raw_sites, list) or not isinstance(unresolved, list) or not isinstance(dangling, list):
        raise CatalogError("catalog inventory arrays are missing")

    expected_ids = [f"RS{index:06d}" for index in range(1, len(raw_sites) + 1)]
    actual_ids = [str(site.get("site_id")) for site in raw_sites]
    if actual_ids != expected_ids:
        raise CatalogError("site IDs are not complete, ordered, and deterministic")
    site_keys = [str(site.get("site_key")) for site in raw_sites]
    if len(set(site_keys)) != len(site_keys):
        raise CatalogError("duplicate site_key detected")

    for site in raw_sites:
        drivers = site.get("drivers")
        sinks = site.get("sinks")
        if not isinstance(drivers, list) or not isinstance(sinks, list):
            raise CatalogError(f"site endpoints missing: {site.get('site_id')}")
        if site.get("driver_count") != len(drivers):
            raise CatalogError(f"driver count mismatch: {site.get('site_id')}")
        if site.get("total_sink_count") != len(sinks):
            raise CatalogError(f"sink count mismatch: {site.get('site_id')}")
        logic_fanout = sum(1 for sink in sinks if sink.get("kind") != "module_output")
        boundary_count = sum(1 for sink in sinks if sink.get("kind") == "module_output")
        if site.get("logic_fanout") != logic_fanout:
            raise CatalogError(f"logic fanout mismatch: {site.get('site_id')}")
        if site.get("boundary_observer_count") != boundary_count:
            raise CatalogError(f"boundary count mismatch: {site.get('site_id')}")
        if site.get("fanout_bucket") != fanout_bucket(logic_fanout):
            raise CatalogError(f"fanout bucket mismatch: {site.get('site_id')}")
        expected_status = (
            "eligible_for_stage2_inventory"
            if len(drivers) == 1
            else "multiple_driver_review"
        )
        if site.get("stage1_status") != expected_status:
            raise CatalogError(f"stage1 status mismatch: {site.get('site_id')}")
        if site.get("state_site") and not (
            len(drivers) == 1 and site.get("source_kind") == "sequential_output"
        ):
            raise CatalogError(f"invalid state-site label: {site.get('site_id')}")

    recorded_digest = payload.get("inventory_digest_sha256")
    stored_digest = inventory_digest(raw_sites, unresolved, dangling)
    if recorded_digest != stored_digest:
        raise CatalogError(
            "stored inventory digest mismatch: "
            f"recorded={recorded_digest}, recomputed={stored_digest}"
        )

    policy = load_policy(policy_path)
    parsed = parse_design(netlist.read_text(encoding="utf-8", errors="strict"), policy)
    top_module = str(payload.get("raw_site_summary", {}).get("top_module", ""))
    rebuilt = build_inventory(parsed, policy, top_module)
    rebuilt_digest = inventory_digest(
        rebuilt.raw_sites,
        rebuilt.unresolved_source_nets,
        rebuilt.dangling_driven_nets,
    )
    if rebuilt_digest != recorded_digest:
        raise CatalogError(
            "reparsed inventory differs from catalog JSON: "
            f"recorded={recorded_digest}, rebuilt={rebuilt_digest}"
        )

    print(f"Catalog JSON        : {json_path}")
    print(f"Source SHA256       : {actual_source_sha}")
    print(f"Policy SHA256       : {actual_policy_sha}")
    print(f"Raw sites           : {len(raw_sites)}")
    print(f"Inventory digest    : {recorded_digest}")
    print("Stage-1 validation : PASS")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build Stage-1 raw-site and Stage-2 static-safety catalogs "
            "from a mapped Verilog netlist."
        )
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {PROGRAM_VERSION}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    self_test = subparsers.add_parser(
        "self-test",
        help="run an internal synthetic-netlist parser/inventory test",
    )
    self_test.add_argument("--policy", type=Path, required=True)
    self_test.set_defaults(handler=run_self_test)

    enumerate_parser = subparsers.add_parser(
        "enumerate",
        help="enumerate raw sites from one immutable mapped netlist",
    )
    enumerate_parser.add_argument("--netlist", type=Path, required=True)
    enumerate_parser.add_argument("--design", required=True)
    enumerate_parser.add_argument("--top-module", required=True)
    enumerate_parser.add_argument("--policy", type=Path, required=True)
    enumerate_parser.add_argument("--json-output", type=Path, required=True)
    enumerate_parser.add_argument("--text-output", type=Path, required=True)
    enumerate_parser.add_argument("--expect-sha256")
    enumerate_parser.add_argument("--expect-module-count", type=int)
    enumerate_parser.add_argument("--expect-standard-cell-count", type=int)
    enumerate_parser.add_argument(
        "--force",
        action="store_true",
        help="replace existing output files intentionally",
    )
    enumerate_parser.set_defaults(handler=run_enumerate)

    validate_parser = subparsers.add_parser(
        "validate-output",
        help="validate a generated Stage-1 catalog and deterministically reparse its inputs",
    )
    validate_parser.add_argument("--json", type=Path, required=True)
    validate_parser.set_defaults(handler=run_validate_output)

    self_test_stage2 = subparsers.add_parser(
        "self-test-stage2",
        help="run a synthetic clock/reset/static-filter safety test",
    )
    self_test_stage2.add_argument("--policy", type=Path, required=True)
    self_test_stage2.add_argument(
        "--safety-policy",
        type=Path,
        required=True,
    )
    self_test_stage2.set_defaults(handler=run_self_test_stage2)

    static_filter = subparsers.add_parser(
        "static-filter",
        help=(
            "apply Stage-2 clock/reset/test safety and coarse "
            "structural-observability filtering"
        ),
    )
    static_filter.add_argument("--stage1-json", type=Path, required=True)
    static_filter.add_argument("--safety-policy", type=Path, required=True)
    static_filter.add_argument("--json-output", type=Path, required=True)
    static_filter.add_argument("--text-output", type=Path, required=True)
    static_filter.add_argument("--expect-stage1-digest")
    static_filter.add_argument(
        "--force",
        action="store_true",
        help="replace existing Stage-2 outputs intentionally",
    )
    static_filter.set_defaults(handler=run_static_filter)

    validate_static = subparsers.add_parser(
        "validate-static-output",
        help=(
            "validate a generated Stage-2 catalog and deterministically "
            "rebuild the safety filter"
        ),
    )
    validate_static.add_argument("--json", type=Path, required=True)
    validate_static.set_defaults(handler=run_validate_static_output)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except CatalogError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except (OSError, UnicodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
