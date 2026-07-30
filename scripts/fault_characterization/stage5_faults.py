#!/usr/bin/env python3
"""Fault2Assertion Stage 5: materialization, compact characterization, and oracle generation.

The program consumes the Stage-4 candidates/selection artifacts and never keeps a
faulty mapped netlist.  A run-local netlist is reconstructed only for one
simulation, compact traces are compared, and the durable result is a small
per-fault diagnostic oracle.

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import gzip
import hashlib
import importlib.util
import json
import re
import shutil
import statistics
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

PROGRAM_VERSION = "1.0.7"
SCHEMA_VERSION = "1.0"
STAGE4_CANDIDATE_MARKER = "stage_04_fault_type_classification"
STAGE4_SELECTION_MARKER = "stage_04_targeted_fault_selection_plan"
STAGE5_CAMPAIGN_MARKER = "stage_05_fault_characterization_campaign"
STAGE5_FAULT_MARKER = "stage_05_fault_materialization"
STAGE5_ORACLE_MARKER = "stage_05_diagnostic_oracle"
FAULT_ID_RE = re.compile(r"^TF\d{6}_SA[01]$")
SELECTION_ID_RE = re.compile(r"^TS\d{6}$")
NORMAL_IDENTIFIER_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
STAGE5_CLOCK_EXPRESSION = "$root.tb_top.clk"


class Stage5Error(RuntimeError):
    """Controlled Stage-5 failure with a user-actionable message."""


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
        raise Stage5Error(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise Stage5Error(f"invalid {label} JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise Stage5Error(f"{label} must contain one JSON object: {path}")
    return payload


def atomic_write_text(path: Path, text: str, force: bool = False) -> None:
    path = path.resolve()
    if path.exists() and not force:
        raise Stage5Error(f"refusing to overwrite without --force: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    token = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    temporary = path.with_name(f".{path.name}.tmp.{token}")
    temporary.write_text(text, encoding="utf-8")
    temporary.chmod(0o644)
    temporary.replace(path)


def write_json(path: Path, payload: Mapping[str, Any], force: bool = False) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, indent=2, sort_keys=False, ensure_ascii=False) + "\n",
        force=force,
    )


def import_python_module(path: Path, module_name: str) -> Any:
    path = path.resolve()
    if not path.is_file():
        raise Stage5Error(f"Python module not found: {path}")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise Stage5Error(f"cannot import Python module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def canonical_signal(expression: str) -> str:
    return re.sub(r"\s+", "", expression.strip())


def safe_token(value: str, prefix: str = "f2a") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_$]", "_", value)
    if not cleaned or not re.match(r"[A-Za-z_$]", cleaned):
        cleaned = f"{prefix}_{cleaned}"
    return cleaned


def sv_identifier(value: str, label: str) -> str:
    stripped = value.strip()
    if NORMAL_IDENTIFIER_RE.fullmatch(stripped):
        return stripped
    if stripped.startswith("\\") and not re.search(r"\s", stripped):
        return stripped + " "
    raise Stage5Error(f"unsupported {label} identifier: {value!r}")


def sv_expression(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith("\\") and not re.search(r"\s", stripped):
        return stripped + " "
    return stripped


def sv_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def module_match_for(text: str, module_name: str) -> re.Match[str]:
    pattern = re.compile(
        rf"\bmodule\s+{re.escape(module_name)}(?=\s|\()(?:.*?)\s*;(?P<body>.*?)\bendmodule\b",
        flags=re.DOTALL,
    )
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise Stage5Error(
            f"expected exactly one module definition for {module_name!r}; found {len(matches)}"
        )
    return matches[0]


def apply_edits(text: str, edits: Sequence[tuple[int, int, str]]) -> str:
    output = text
    previous_start = len(text) + 1
    for start, end, replacement in sorted(edits, key=lambda item: item[0], reverse=True):
        if not (0 <= start <= end <= len(text)):
            raise Stage5Error(f"invalid netlist edit span: {(start, end)}")
        if end > previous_start:
            raise Stage5Error("overlapping netlist edits")
        output = output[:start] + replacement + output[end:]
        previous_start = start
    return output


def unified_patch(original: str, modified: str, source_name: str, fault_id: str) -> str:
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            modified.splitlines(keepends=True),
            fromfile=f"a/{source_name}",
            tofile=f"b/run-local/{fault_id}/fault_netlist.v",
            n=3,
        )
    )


@dataclass(frozen=True)
class PreparedDesign:
    module: Any
    policy: Any
    parsed: Any
    top_module: str
    inventory_by_node: Mapping[tuple[str, str], Mapping[str, Any]]
    netlist_path: Path
    netlist_sha256: str
    text: str


@dataclass
class SampleSeries:
    rows: list[tuple[int, int, tuple[str, ...]]] = field(default_factory=list)

    def add(self, cycle: int, time_value: int, values: Sequence[str]) -> None:
        normalized = tuple(str(value).strip().lower() for value in values)
        if self.rows and self.rows[-1][2] == normalized:
            return
        self.rows.append((cycle, time_value, normalized))

    def known_values(self, column: int) -> set[str]:
        result: set[str] = set()
        for _, _, values in self.rows:
            if column >= len(values):
                continue
            value = values[column]
            if value and not any(char in value for char in "xz"):
                result.add(value)
        return result


# ---------------------------------------------------------------------------
# Stage-4 loading and structural resolution
# ---------------------------------------------------------------------------


def validate_stage4(
    candidates: Mapping[str, Any],
    selection: Mapping[str, Any],
) -> None:
    if candidates.get("stage") != STAGE4_CANDIDATE_MARKER:
        raise Stage5Error(
            f"candidate stage mismatch: {candidates.get('stage')!r}"
        )
    if selection.get("stage") != STAGE4_SELECTION_MARKER:
        raise Stage5Error(
            f"selection stage mismatch: {selection.get('stage')!r}"
        )
    sites = candidates.get("sites")
    selected_sites = selection.get("selected_sites")
    fault_instances = selection.get("fault_instances")
    if not isinstance(sites, list):
        raise Stage5Error("Stage-4 candidates missing sites array")
    if not isinstance(selected_sites, list):
        raise Stage5Error("Stage-4 selection missing selected_sites array")
    if not isinstance(fault_instances, list):
        raise Stage5Error("Stage-4 selection missing fault_instances array")
    summary = selection.get("selection_summary", {})
    expected = int(summary.get("selected_fault_instance_count", -1))
    if expected != len(fault_instances):
        raise Stage5Error(
            f"Stage-4 fault-instance count mismatch: summary={expected}, actual={len(fault_instances)}"
        )
    if len(selected_sites) != int(summary.get("selected_unique_site_count", -1)):
        raise Stage5Error("Stage-4 selected-site count mismatch")
    fault_ids = [str(item.get("fault_id", "")) for item in fault_instances]
    if any(not FAULT_ID_RE.fullmatch(item) for item in fault_ids):
        raise Stage5Error("Stage-4 contains an invalid TF fault ID")
    if len(set(fault_ids)) != len(fault_ids):
        raise Stage5Error("Stage-4 contains duplicate TF fault IDs")


def infer_top_module(
    candidates: Mapping[str, Any],
    candidates_path: Path,
    parsed: Any,
) -> str:
    """Recover the same top module used by Stage 1/2.

    Stage-4 records the Stage-2 source metadata, but older artifacts do not
    necessarily copy ``top_module`` directly.  Prefer the recorded Stage-2 JSON,
    then use deterministic structural fallbacks.
    """

    source_stage2 = candidates.get("source_stage2")
    if isinstance(source_stage2, dict):
        stage2_value = source_stage2.get("path")
        if isinstance(stage2_value, str) and stage2_value:
            stage2_path = Path(stage2_value).expanduser()
            if not stage2_path.is_absolute():
                stage2_path = (candidates_path.parent / stage2_path).resolve()
            if stage2_path.is_file():
                stage2_payload = load_json(stage2_path, "recorded Stage-2 catalog")
                summary = stage2_payload.get("stage2_summary")
                if isinstance(summary, dict):
                    top = summary.get("top_module")
                    if isinstance(top, str) and top in parsed.modules:
                        return top

        direct_top = source_stage2.get("top_module")
        if isinstance(direct_top, str) and direct_top in parsed.modules:
            return direct_top

    # Project-specific deterministic fallback.
    if "cv32e40p_top" in parsed.modules:
        return "cv32e40p_top"

    instantiated = {
        instance.cell_type
        for module_info in parsed.modules.values()
        for instance in module_info.instances
        if instance.cell_type in parsed.modules
    }
    roots = sorted(set(parsed.modules) - instantiated)
    if len(roots) == 1:
        return roots[0]
    raise Stage5Error(
        "cannot uniquely recover Stage-1 top module; "
        f"root_candidates={roots}"
    )


def prepare_design(
    candidates: Mapping[str, Any],
    candidates_path: Path,
    site_catalog_tool: Path,
    policy_path: Path,
) -> PreparedDesign:
    mapped = candidates.get("mapped_netlist")
    if not isinstance(mapped, dict):
        raise Stage5Error("Stage-4 candidates missing mapped_netlist metadata")
    netlist_path = Path(str(mapped.get("path", ""))).resolve()
    expected_sha = str(mapped.get("sha256", ""))
    if not netlist_path.is_file():
        raise Stage5Error(f"mapped netlist not found: {netlist_path}")
    actual_sha = sha256_file(netlist_path)
    if actual_sha != expected_sha:
        raise Stage5Error(
            "mapped-netlist SHA-256 changed after Stage 4\n"
            f"  expected: {expected_sha}\n"
            f"  actual:   {actual_sha}"
        )
    module = import_python_module(site_catalog_tool, "f2a_stage5_site_catalog")
    policy = module.load_policy(policy_path.resolve())
    text = netlist_path.read_text(encoding="utf-8", errors="strict")
    parsed = module.parse_design(text, policy)
    top_module = infer_top_module(candidates, candidates_path, parsed)

    # Stage 2 intentionally keeps only one singular ``driver`` and omits the
    # full sink list.  Rebuild the immutable Stage-1 inventory from the exact
    # same netlist and policy so Stage 5 can recover complete drivers/sinks by
    # the stable (module, source_key) identity.
    inventory = module.build_inventory(parsed, policy, top_module)
    inventory_by_node: dict[tuple[str, str], Mapping[str, Any]] = {}
    for raw_site in inventory.raw_sites:
        key = (str(raw_site["module"]), str(raw_site["source_key"]))
        if key in inventory_by_node:
            raise Stage5Error(f"duplicate rebuilt Stage-1 site identity: {key}")
        inventory_by_node[key] = raw_site

    return PreparedDesign(
        module=module,
        policy=policy,
        parsed=parsed,
        top_module=top_module,
        inventory_by_node=inventory_by_node,
        netlist_path=netlist_path,
        netlist_sha256=actual_sha,
        text=text,
    )


def rebuilt_stage1_site(
    prepared: PreparedDesign,
    stage4_site: Mapping[str, Any],
) -> Mapping[str, Any]:
    key = (str(stage4_site["module"]), str(stage4_site["source_key"]))
    raw_site = prepared.inventory_by_node.get(key)
    if raw_site is None:
        raise Stage5Error(
            "selected Stage-4 site was not found in rebuilt Stage-1 inventory: "
            f"site_id={stage4_site.get('site_id')} key={key}"
        )
    if str(raw_site.get("site_key")) != str(stage4_site.get("site_key")):
        raise Stage5Error(
            "Stage-1/Stage-4 site-key mismatch: "
            f"site_id={stage4_site.get('site_id')} "
            f"stage1={raw_site.get('site_key')!r} "
            f"stage4={stage4_site.get('site_key')!r}"
        )
    if str(raw_site.get("site_id")) != str(stage4_site.get("site_id")):
        raise Stage5Error(
            "Stage-1/Stage-4 deterministic site-ID mismatch: "
            f"stage1={raw_site.get('site_id')!r} "
            f"stage4={stage4_site.get('site_id')!r}"
        )
    return raw_site


def find_instance(prepared: PreparedDesign, module_name: str, instance_name: str) -> Any:
    module_info = prepared.parsed.modules.get(module_name)
    if module_info is None:
        raise Stage5Error(f"module not found in parsed netlist: {module_name}")
    matches = [item for item in module_info.instances if item.instance == instance_name]
    if len(matches) != 1:
        raise Stage5Error(
            f"expected one instance {module_name}/{instance_name}; found {len(matches)}"
        )
    return matches[0]


def find_connection(instance: Any, pin_name: str) -> Any:
    matches = [item for item in instance.connections if item.pin == pin_name]
    if len(matches) != 1:
        raise Stage5Error(
            f"expected one connection {instance.module}/{instance.instance}/{pin_name}; "
            f"found {len(matches)}"
        )
    return matches[0]


def render_flattened_connection(values: Sequence[str | None]) -> str:
    if any(value is None for value in values):
        raise Stage5Error(
            "cannot rewrite a hierarchical output connection containing constant/open bits"
        )
    rendered = [sv_expression(str(value)) for value in values]
    if len(rendered) == 1:
        return rendered[0]
    return "{" + ", ".join(rendered) + "}"


def resolve_driver_edit(
    prepared: PreparedDesign,
    site: Mapping[str, Any],
    temporary_net: str,
) -> tuple[Any, str, dict[str, Any]]:
    raw_site = rebuilt_stage1_site(prepared, site)
    driver_status = raw_site.get("driver_status")
    drivers = raw_site.get("drivers")
    if driver_status != "unique" or not isinstance(drivers, list) or len(drivers) != 1:
        actual_count = len(drivers) if isinstance(drivers, list) else None
        raise Stage5Error(
            "selected site is not uniquely materializable in rebuilt Stage-1 inventory: "
            f"site_id={site.get('site_id')} "
            f"driver_status={driver_status!r} "
            f"drivers_length={actual_count!r}"
        )
    driver = drivers[0]
    if not isinstance(driver, dict):
        raise Stage5Error("rebuilt Stage-1 driver metadata must be an object")
    module_name = str(site["module"])
    source_key = str(site["source_key"])
    kind = str(driver.get("kind", ""))
    instance_name = str(driver.get("instance", ""))
    pin_name = str(driver.get("pin", ""))
    if kind not in {"standard_cell_output", "hierarchical_instance_output"}:
        raise Stage5Error(
            f"unsupported Stage-5 driver kind {kind!r} for {site.get('site_id')}"
        )
    instance = find_instance(prepared, module_name, instance_name)
    connection = find_connection(instance, pin_name)
    original_connection = connection.expression.strip()

    if kind == "standard_cell_output":
        if canonical_signal(original_connection) != source_key:
            raise Stage5Error(
                f"driver connection mismatch for {site.get('site_id')}: "
                f"catalog={source_key!r}, netlist={original_connection!r}"
            )
        replacement_connection = temporary_net
    else:
        child = prepared.parsed.modules.get(instance.cell_type)
        if child is None:
            raise Stage5Error(
                f"child module missing for hierarchical driver: {instance.cell_type}"
            )
        port_by_key = {
            prepared.module.canonical_signal(port.name): port for port in child.ports
        }
        port = port_by_key.get(prepared.module.canonical_signal(pin_name))
        if port is None:
            raise Stage5Error(
                f"child port not found: {instance.cell_type}.{pin_name}"
            )
        flattened = prepared.module.flatten_connection_bits(
            original_connection, port.width
        )
        matching = [
            index
            for index, expression in enumerate(flattened)
            if expression is not None
            and canonical_signal(str(expression)) == source_key
        ]
        if len(matching) != 1:
            raise Stage5Error(
                f"cannot uniquely locate selected hierarchical output bit for "
                f"{site.get('site_id')}; matches={matching}"
            )
        rewritten = list(flattened)
        rewritten[matching[0]] = temporary_net
        replacement_connection = render_flattened_connection(rewritten)

    resolved = {
        "kind": kind,
        "module": module_name,
        "instance": instance_name,
        "cell_type": str(driver.get("cell_type", instance.cell_type)),
        "pin": pin_name,
        "original_connection": original_connection,
        "replacement_connection": replacement_connection,
        "connection_start": connection.expression_start,
        "connection_end": connection.expression_end,
    }
    return connection, replacement_connection, resolved


def resolve_receiver_signals(
    prepared: PreparedDesign,
    site: Mapping[str, Any],
) -> list[dict[str, Any]]:
    raw_site = rebuilt_stage1_site(prepared, site)
    module_name = str(raw_site["module"])
    source_key = str(raw_site["source_key"])
    source_net = str(raw_site["source_net"])
    sinks = raw_site.get("sinks")
    if not isinstance(sinks, list):
        raise Stage5Error(
            f"rebuilt Stage-1 site sinks missing: {site.get('site_id')}"
        )

    output: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(expression: str, role: str, metadata: Mapping[str, Any]) -> None:
        key = canonical_signal(expression)
        if not key or key in seen:
            return
        if prepared.module.is_constant(expression):
            return
        if not prepared.module.is_simple_signal(expression):
            return
        seen.add(key)
        output.append(
            {
                "receiver_index": len(output),
                "expression": expression.strip(),
                "source_key": key,
                "role": role,
                "metadata": dict(metadata),
            }
        )

    for sink in sinks:
        if not isinstance(sink, dict):
            continue
        kind = str(sink.get("kind", ""))
        if kind == "standard_cell_input":
            instance_name = str(sink.get("instance", ""))
            instance = find_instance(prepared, module_name, instance_name)
            for connection in instance.connections:
                if connection.pin not in prepared.policy.output_pins(instance.cell_type):
                    continue
                add(
                    connection.expression,
                    "direct_receiver_output",
                    {
                        "receiver_instance": instance_name,
                        "receiver_cell_type": instance.cell_type,
                        "receiver_input_pin": sink.get("pin"),
                        "receiver_output_pin": connection.pin,
                        "receiver_input_role": sink.get("role"),
                    },
                )
        elif kind in {"hierarchical_instance_input", "module_output"}:
            add(
                source_net,
                "hierarchy_or_output_boundary",
                {
                    "sink_kind": kind,
                    "sink_instance": sink.get("instance"),
                    "sink_pin": sink.get("pin"),
                    "sink_role": sink.get("role"),
                },
            )

    if not output:
        add(
            source_net,
            "source_boundary_fallback",
            {"reason": "no directly resolvable receiver output"},
        )

    for index, item in enumerate(output):
        item["receiver_index"] = index
        item["receiver_id"] = f"R{index:03d}"
    if any(item["source_key"] == source_key for item in output):
        # This is allowed for boundary observation and is explicitly labeled.
        pass
    return output


def build_modified_netlist(
    prepared: PreparedDesign,
    site: Mapping[str, Any],
    fault_id: str,
    stuck_at: int,
    temporary_net: str,
) -> tuple[str, dict[str, Any]]:
    connection, replacement, resolved_driver = resolve_driver_edit(
        prepared, site, temporary_net
    )
    module_name = str(site["module"])
    source_net = str(site["source_net"])
    module_match = module_match_for(prepared.text, module_name)
    insertion = module_match.end("body")
    declaration = (
        "\n"
        f"  // Fault2Assertion Stage-5 fault {fault_id} run-local driver split\n"
        f"  wire {temporary_net};\n"
        f"  assign {sv_expression(source_net)} = 1'b{stuck_at};\n"
    )
    edits = [
        (connection.expression_start, connection.expression_end, replacement),
        (insertion, insertion, declaration),
    ]
    modified = apply_edits(prepared.text, edits)

    # Validate the exact inserted block instead of searching near the original
    # insertion offset.  A driver-connection rewrite that occurs before the
    # module-body insertion can change the final offset substantially, especially
    # for hierarchical vector/concatenation connections.  The old bounded-window
    # search therefore produced false failures even though the assignment was
    # present in the edited netlist.
    if declaration not in modified:
        raise Stage5Error(
            f"exact Stage-5 declaration block missing after edit: {fault_id}"
        )

    # The temporary net must occur in both its declaration and the rewritten
    # driver output connection.  This catches a no-op connection edit while
    # remaining independent of offset changes.
    temporary_occurrences = modified.count(temporary_net)
    if temporary_occurrences < 2:
        raise Stage5Error(
            "temporary pre-fault net was not connected after edit: "
            f"{fault_id}; occurrences={temporary_occurrences}"
        )
    modification = {
        "method": "split_unique_driver_output_then_assign_original_site_net_constant",
        "temporary_pre_fault_net": temporary_net,
        "stuck_at_assignment": f"assign {source_net} = 1'b{stuck_at};",
        "driver_connection": resolved_driver,
        "definition_level_semantics": (
            "the selected module-definition site is modified; every elaborated "
            "instance of that module observes the same injected site fault"
        ),
    }
    return modified, modification


# ---------------------------------------------------------------------------
# Preparation and apply
# ---------------------------------------------------------------------------


def preflight_selected_sites(
    prepared: PreparedDesign,
    candidates: Mapping[str, Any],
    selection: Mapping[str, Any],
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    """Validate every selected site before writing any Stage-5 artifact.

    This prevents a late materialization failure from leaving a partially
    populated fault_specs/ or patches/ directory.
    """

    candidate_by_id: dict[str, Mapping[str, Any]] = {
        str(site["site_id"]): site
        for site in candidates["sites"]
        if isinstance(site, dict) and "site_id" in site
    }
    selected_by_id: dict[str, Mapping[str, Any]] = {
        str(site["selection_id"]): site
        for site in selection["selected_sites"]
        if isinstance(site, dict) and "selection_id" in site
    }

    failures: list[str] = []
    checked_site_ids: set[str] = set()
    for selected in selection["selected_sites"]:
        if not isinstance(selected, dict):
            failures.append("selected-site record is not an object")
            continue
        selection_id = str(selected.get("selection_id", ""))
        site_id = str(selected.get("site_id", ""))
        if site_id in checked_site_ids:
            failures.append(f"duplicate selected site_id={site_id}")
            continue
        checked_site_ids.add(site_id)

        site = candidate_by_id.get(site_id)
        if site is None:
            failures.append(
                f"selection_id={selection_id} site_id={site_id}: candidate missing"
            )
            continue
        if site.get("stage4_status") != "classified_candidate":
            failures.append(
                f"selection_id={selection_id} site_id={site_id}: "
                f"stage4_status={site.get('stage4_status')!r}"
            )
            continue

        temporary_net = f"f2a_preflight_{selection_id.lower()}"
        try:
            resolve_driver_edit(prepared, site, temporary_net)
            receivers = resolve_receiver_signals(prepared, site)
            if not receivers:
                raise Stage5Error("no directly observable receiver signals")
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
            "Stage-5 materialization preflight failed before writing artifacts.\n"
            f"Invalid selected sites: {len(failures)}\n"
            f"{preview}{suffix}\n"
            "Review the listed structural-resolution failures before changing Stage 4."
        )

    return candidate_by_id, selected_by_id


def command_prepare(args: argparse.Namespace) -> int:
    candidates_path = args.candidates.resolve()
    selection_path = args.selection.resolve()
    output_root = args.output_root.resolve()
    candidates = load_json(candidates_path, "Stage-4 candidates")
    selection = load_json(selection_path, "Stage-4 selection")
    validate_stage4(candidates, selection)
    prepared = prepare_design(
        candidates, candidates_path, args.site_catalog_tool, args.policy
    )

    # Validate all selected sites first.  Nothing below this point is created
    # until every selected site has a supported unique driver and at least one
    # directly observable receiver signal.
    candidate_by_id, selected_by_id = preflight_selected_sites(
        prepared, candidates, selection
    )

    fault_specs_dir = output_root / "fault_specs"
    patches_dir = output_root / "patches"
    campaign_path = output_root / "stage_05_campaign.json"
    if campaign_path.exists() and not args.force:
        raise Stage5Error(
            f"Stage-5 campaign already exists; use --force intentionally: {campaign_path}"
        )
    fault_specs_dir.mkdir(parents=True, exist_ok=True)
    patches_dir.mkdir(parents=True, exist_ok=True)

    campaign_faults: list[dict[str, Any]] = []
    representative_by_selection: dict[str, str] = {}
    for instance in selection["fault_instances"]:
        fault_id = str(instance["fault_id"])
        selection_id = str(instance["selection_id"])
        site_id = str(instance["site_id"])
        if not FAULT_ID_RE.fullmatch(fault_id):
            raise Stage5Error(f"invalid fault ID: {fault_id}")
        if not SELECTION_ID_RE.fullmatch(selection_id):
            raise Stage5Error(f"invalid selection ID: {selection_id}")
        site = candidate_by_id.get(site_id)
        selected = selected_by_id.get(selection_id)
        if site is None or selected is None:
            raise Stage5Error(f"Stage-4 cross-reference missing for {fault_id}")
        if site.get("stage4_status") != "classified_candidate":
            raise Stage5Error(f"selected Stage-4 site is not classified: {site_id}")
        stuck_at = int(instance["stuck_at"])
        polarity = str(instance["polarity"])
        if stuck_at not in {0, 1} or polarity != f"SA{stuck_at}":
            raise Stage5Error(f"polarity/stuck-at mismatch: {fault_id}")

        temporary_net = f"f2a_pre_{fault_id.lower()}"
        if temporary_net in prepared.text:
            raise Stage5Error(f"temporary-net collision: {temporary_net}")
        modified, modification = build_modified_netlist(
            prepared,
            site,
            fault_id,
            stuck_at,
            temporary_net,
        )
        patch = unified_patch(
            prepared.text,
            modified,
            prepared.netlist_path.name,
            fault_id,
        )
        if not patch.strip():
            raise Stage5Error(f"empty patch generated for {fault_id}")
        receiver_signals = resolve_receiver_signals(prepared, site)

        fault_spec_path = fault_specs_dir / f"{fault_id}.json"
        patch_path = patches_dir / f"{fault_id}.patch"
        spec = {
            "schema_version": SCHEMA_VERSION,
            "program_version": PROGRAM_VERSION,
            "generated_at_utc": utc_now(),
            "stage": STAGE5_FAULT_MARKER,
            "fault_id": fault_id,
            "selection_id": selection_id,
            "selection_rank": int(instance["selection_rank"]),
            "site_id": site_id,
            "site_key": str(site["site_key"]),
            "design": str(selection["design"]),
            "workload": str(selection["workload"]),
            "fault_class": str(instance["fault_class"]),
            "injection_kind": str(instance["injection_kind"]),
            "polarity": polarity,
            "stuck_at": stuck_at,
            "source_stage4": {
                "candidates_path": str(candidates_path),
                "candidates_sha256": sha256_file(candidates_path),
                "candidate_digest_sha256": candidates.get(
                    "candidate_digest_sha256"
                ),
                "selection_path": str(selection_path),
                "selection_sha256": sha256_file(selection_path),
                "selection_digest_sha256": selection.get(
                    "selection_digest_sha256"
                ),
            },
            "mapped_netlist": {
                "path": str(prepared.netlist_path),
                "sha256": prepared.netlist_sha256,
                "storage_policy": (
                    "immutable golden source; a faulty copy exists only in a "
                    "run-local temporary directory"
                ),
            },
            "site": {
                "module": str(site["module"]),
                "source_net": str(site["source_net"]),
                "source_key": str(site["source_key"]),
                "source_kind": str(site["source_kind"]),
                "state_site": bool(site["state_site"]),
                "logic_fanout": int(site.get("logic_fanout", 0)),
                "fanout_bucket": str(site.get("fanout_bucket", "")),
                "classification": site.get("classification"),
                "scores": site.get("scores"),
                "activity": site.get("activity"),
                "detailed_structure": site.get("detailed_structure"),
                "driver": rebuilt_stage1_site(prepared, site)["drivers"][0],
            },
            "receiver_signals": receiver_signals,
            "modification": modification,
            "artifacts": {
                "fault_spec": str(fault_spec_path),
                "patch": str(patch_path),
                "faulty_netlist": "temporary_only",
                "vcd": "not_generated_by_default",
            },
        }
        spec["fault_spec_digest_sha256"] = canonical_json_digest(
            {key: value for key, value in spec.items() if key != "generated_at_utc"}
        )
        write_json(fault_spec_path, spec, force=args.force)
        atomic_write_text(patch_path, patch, force=args.force)
        campaign_faults.append(
            {
                "fault_id": fault_id,
                "selection_id": selection_id,
                "site_id": site_id,
                "fault_class": spec["fault_class"],
                "polarity": polarity,
                "stuck_at": stuck_at,
                "fault_spec": str(fault_spec_path),
                "patch": str(patch_path),
                "fault_spec_digest_sha256": spec["fault_spec_digest_sha256"],
            }
        )
        representative_by_selection.setdefault(selection_id, fault_id)

    campaign_faults.sort(key=lambda item: item["fault_id"])
    selected_sites = []
    for selected in selection["selected_sites"]:
        selection_id = str(selected["selection_id"])
        selected_sites.append(
            {
                **selected,
                "representative_fault_id": representative_by_selection[selection_id],
            }
        )

    campaign = {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "generated_at_utc": utc_now(),
        "stage": STAGE5_CAMPAIGN_MARKER,
        "design": selection["design"],
        "workload": selection["workload"],
        "source_stage4": {
            "candidates_path": str(candidates_path),
            "candidates_sha256": sha256_file(candidates_path),
            "candidate_digest_sha256": candidates.get("candidate_digest_sha256"),
            "selection_path": str(selection_path),
            "selection_sha256": sha256_file(selection_path),
            "selection_digest_sha256": selection.get("selection_digest_sha256"),
        },
        "mapped_netlist": {
            "path": str(prepared.netlist_path),
            "sha256": prepared.netlist_sha256,
        },
        "storage_policy": {
            "durable": [
                "fault_specs/*.json",
                "patches/*.patch",
                "oracles/*.json",
                "reports/*.txt",
                "sva_seeds/*.sva",
                "summary/*",
            ],
            "temporary_and_deleted": [
                "fault_netlist.v",
                "prepared simulation netlist",
                "Xcelium work directory",
                "raw compact TSV traces",
                "VCD fallback files",
            ],
        },
        "campaign_summary": {
            "selected_unique_site_count": len(selected_sites),
            "fault_instance_count": len(campaign_faults),
            "by_fault_class": dict(
                sorted(Counter(item["fault_class"] for item in campaign_faults).items())
            ),
            "by_polarity": dict(
                sorted(Counter(item["polarity"] for item in campaign_faults).items())
            ),
        },
        "selected_sites": selected_sites,
        "faults": campaign_faults,
    }
    campaign["campaign_digest_sha256"] = canonical_json_digest(
        {
            "source_stage4": campaign["source_stage4"],
            "mapped_netlist": campaign["mapped_netlist"],
            "selected_sites": selected_sites,
            "faults": campaign_faults,
        }
    )
    write_json(campaign_path, campaign, force=args.force)
    print(f"Stage-5 campaign      : {campaign_path}")
    print(f"Selected sites        : {len(selected_sites)}")
    print(f"Fault instances       : {len(campaign_faults)}")
    print(f"Fault specs           : {fault_specs_dir}")
    print(f"Patches               : {patches_dir}")
    print("Faulty netlists stored: 0")
    print("Stage-5 prepare       : PASS")
    return 0


def command_apply(args: argparse.Namespace) -> int:
    fault_path = args.fault_json.resolve()
    output = args.output_netlist.resolve()
    spec = load_json(fault_path, "Stage-5 fault spec")
    if spec.get("stage") != STAGE5_FAULT_MARKER:
        raise Stage5Error("fault JSON is not a Stage-5 fault spec")
    source = Path(str(spec["mapped_netlist"]["path"])).resolve()
    expected_sha = str(spec["mapped_netlist"]["sha256"])
    if not source.is_file():
        raise Stage5Error(f"golden mapped netlist not found: {source}")
    actual_sha = sha256_file(source)
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
    modification = spec["modification"]
    driver = modification["driver_connection"]
    start = int(driver["connection_start"])
    end = int(driver["connection_end"])
    original = text[start:end]
    if canonical_signal(original) != canonical_signal(driver["original_connection"]):
        raise Stage5Error(
            f"driver connection changed for {spec['fault_id']}: "
            f"expected={driver['original_connection']!r}, actual={original!r}"
        )
    module_name = str(spec["site"]["module"])
    insertion = module_match_for(text, module_name).end("body")
    temporary_net = str(modification["temporary_pre_fault_net"])
    source_net = str(spec["site"]["source_net"])
    stuck_at = int(spec["stuck_at"])
    declaration = (
        "\n  // Fault2Assertion Stage-5 run-local driver split\n"
        f"  wire {temporary_net};\n"
        f"  assign {sv_expression(source_net)} = 1'b{stuck_at};\n"
    )
    modified = apply_edits(
        text,
        [
            (start, end, str(driver["replacement_connection"])),
            (insertion, insertion, declaration),
        ],
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(modified, encoding="utf-8")
    if sha256_file(source) != expected_sha:
        output.unlink(missing_ok=True)
        raise Stage5Error("immutable golden netlist changed during apply")
    print(f"Fault ID             : {spec['fault_id']}")
    print(f"Golden netlist       : {source}")
    print(f"Run-local netlist    : {output}")
    print(f"Run-local SHA-256    : {sha256_file(output)}")
    return 0


# ---------------------------------------------------------------------------
# Compact monitor generation
# ---------------------------------------------------------------------------


def receiver_concat(receivers: Sequence[Mapping[str, Any]]) -> str:
    expressions = [sv_expression(str(item["expression"])) for item in receivers]
    if len(expressions) == 1:
        return expressions[0]
    return "{" + ", ".join(expressions) + "}"


def trace_writer_package(
    package_name: str,
    trace_path: Path,
    header_fields: Sequence[str],
) -> str:
    """Return one shared trace-writer package for all bound monitor instances.

    Each bound monitor instance shares exactly one file descriptor.  The
    generated monitors do not use SystemVerilog ``final`` blocks.  Every compact
    record is flushed immediately, so a timeout, ``$fatal``, or simulator abort
    does not depend on end-of-simulation cleanup to preserve the trace tail.
    """

    header_literal = "\\t".join(sv_string(str(field)) for field in header_fields)
    return f"""package {package_name};
  integer fd = 0;
  bit opened = 1'b0;

  task automatic ensure_open();
    if (!opened) begin
      fd = $fopen("{sv_string(str(trace_path.resolve()))}", "w");
      if (fd == 0) $fatal(1, "cannot open Stage-5 compact trace");
      opened = 1'b1;
      $fwrite(fd, "{header_literal}\\n");
      $fflush(fd);
    end
  endtask
endpackage
"""


def golden_monitor_module(
    module_name: str,
    records: Sequence[Mapping[str, Any]],
    package_name: str,
) -> str:
    tag = hashlib.sha256(module_name.encode("utf-8")).hexdigest()[:10]
    monitor_name = f"f2a_stage5_g_{tag}"
    instance_name = f"{monitor_name}_i"
    port_lines = ["    input wire f2a_clk_i"]
    bind_lines = [f"    .f2a_clk_i({STAGE5_CLOCK_EXPRESSION})"]
    declarations: list[str] = []
    activity_blocks: list[str] = []
    sample_blocks: list[str] = []

    for record in records:
        selection_id = str(record["selection_id"])
        token = selection_id.lower()
        source = str(record["site"]["source_net"])
        receivers = record["receiver_signals"]
        width = len(receivers)
        if width <= 0:
            raise Stage5Error(f"no receiver signals for {selection_id}")
        src_port = f"src_{token}_i"
        recv_port = f"recv_{token}_i"
        port_lines.append(f"    input wire {src_port}")
        port_lines.append(f"    input wire [{width - 1}:0] {recv_port}")
        bind_lines.append(f"    .{src_port}({sv_expression(source)})")
        bind_lines.append(f"    .{recv_port}({receiver_concat(receivers)})")
        declarations.extend(
            [
                f"  logic prev_valid_{token} = 1'b0;",
                f"  logic prev_src_{token};",
                f"  logic [{width - 1}:0] prev_recv_{token};",
            ]
        )
        activity_blocks.append(
            f"""  always @({src_port}) begin
    if ({src_port} === 1'b0 || {src_port} === 1'b1) begin
      {package_name}::ensure_open();
      $fwrite({package_name}::fd,
              "GA\\t{selection_id}\\t%0d\\t%0t\\t%m\\tSRC\\t%b\\n",
              cycle, $time, {src_port});
      $fflush({package_name}::fd);
    end
  end"""
        )
        sample_blocks.append(
            f"""    if (!prev_valid_{token} ||
        prev_src_{token} !== {src_port} ||
        prev_recv_{token} !== {recv_port}) begin
      {package_name}::ensure_open();
      $fwrite({package_name}::fd,
              "G\\t{selection_id}\\t%0d\\t%0t\\t%m\\t%b\\t%b\\n",
              cycle, $time, {src_port}, {recv_port});
      $fflush({package_name}::fd);
      prev_valid_{token} = 1'b1;
      prev_src_{token} = {src_port};
      prev_recv_{token} = {recv_port};
    end"""
        )

    ports = ",\n".join(port_lines)
    binds = ",\n".join(bind_lines)
    return f"""module {monitor_name} (
{ports}
);
  longint unsigned cycle = 0;
{chr(10).join(declarations)}

  initial begin
    {package_name}::ensure_open();
  end

{chr(10).join(activity_blocks)}

  always @(posedge f2a_clk_i) begin
    cycle = cycle + 1;
{chr(10).join(sample_blocks)}
  end
endmodule

bind {sv_identifier(module_name, 'module')} {monitor_name} {instance_name} (
{binds}
);
"""


def fault_monitor_module(spec: Mapping[str, Any], trace_path: Path) -> str:
    fault_id = str(spec["fault_id"])
    tag = hashlib.sha256(fault_id.encode("utf-8")).hexdigest()[:10]
    package_name = f"f2a_stage5_f_trace_pkg_{tag}"
    monitor_name = f"f2a_stage5_f_{tag}"
    instance_name = f"{monitor_name}_i"
    module_name = str(spec["site"]["module"])
    source = str(spec["site"]["source_net"])
    temporary = str(spec["modification"]["temporary_pre_fault_net"])
    receivers = spec["receiver_signals"]
    width = len(receivers)
    if width <= 0:
        raise Stage5Error(f"no receiver signals for {fault_id}")
    package_text = trace_writer_package(
        package_name,
        trace_path,
        ("H", "FAULT", fault_id),
    )
    return f"""`timescale 1ns/1ps
{package_text}
module {monitor_name} (
    input wire f2a_clk_i,
    input wire pre_fault_i,
    input wire observed_i,
    input wire [{width - 1}:0] receivers_i
);
  longint unsigned cycle = 0;
  logic prev_valid = 1'b0;
  logic prev_pre;
  logic prev_observed;
  logic [{width - 1}:0] prev_receivers;

  initial begin
    {package_name}::ensure_open();
  end

  always @(pre_fault_i) begin
    if (pre_fault_i === 1'b0 || pre_fault_i === 1'b1) begin
      {package_name}::ensure_open();
      $fwrite({package_name}::fd,
              "FA\\t{fault_id}\\t%0d\\t%0t\\t%m\\tPRE\\t%b\\n",
              cycle, $time, pre_fault_i);
      $fflush({package_name}::fd);
    end
  end

  always @(observed_i) begin
    if (observed_i === 1'b0 || observed_i === 1'b1) begin
      {package_name}::ensure_open();
      $fwrite({package_name}::fd,
              "FA\\t{fault_id}\\t%0d\\t%0t\\t%m\\tOBS\\t%b\\n",
              cycle, $time, observed_i);
      $fflush({package_name}::fd);
    end
  end

  always @(posedge f2a_clk_i) begin
    cycle = cycle + 1;
    if (!prev_valid ||
        prev_pre !== pre_fault_i ||
        prev_observed !== observed_i ||
        prev_receivers !== receivers_i) begin
      {package_name}::ensure_open();
      $fwrite({package_name}::fd,
              "F\\t{fault_id}\\t%0d\\t%0t\\t%m\\t%b\\t%b\\t%b\\n",
              cycle, $time, pre_fault_i, observed_i, receivers_i);
      $fflush({package_name}::fd);
      prev_valid = 1'b1;
      prev_pre = pre_fault_i;
      prev_observed = observed_i;
      prev_receivers = receivers_i;
    end
  end
endmodule

bind {sv_identifier(module_name, 'module')} {monitor_name} {instance_name} (
    .f2a_clk_i({STAGE5_CLOCK_EXPRESSION}),
    .pre_fault_i({sv_expression(temporary)}),
    .observed_i({sv_expression(source)}),
    .receivers_i({receiver_concat(receivers)})
);
"""


def validate_generated_monitor_text(text: str, label: str) -> None:
    """Reject monitor text that reintroduces unsupported shutdown logic."""

    if re.search(r"\bfinal\s+(?:begin|:)", text):
        raise Stage5Error(
            f"{label} generation attempted to emit a SystemVerilog final block"
        )
    if "::flush();" in text:
        raise Stage5Error(
            f"{label} generation attempted to call the removed package flush task"
        )


def command_make_golden_monitor(args: argparse.Namespace) -> int:
    campaign = load_json(args.campaign.resolve(), "Stage-5 campaign")
    if campaign.get("stage") != STAGE5_CAMPAIGN_MARKER:
        raise Stage5Error("not a Stage-5 campaign JSON")
    records_by_module: dict[str, list[dict[str, Any]]] = defaultdict(list)
    manifest_sites: list[dict[str, Any]] = []
    fault_by_id = {
        str(item["fault_id"]): item for item in campaign["faults"]
    }
    for selected in campaign["selected_sites"]:
        fault_id = str(selected["representative_fault_id"])
        fault_record = fault_by_id.get(fault_id)
        if fault_record is None:
            raise Stage5Error(
                f"representative fault missing from campaign: {fault_id}"
            )
        spec = load_json(Path(fault_record["fault_spec"]), "fault spec")
        record = {
            "selection_id": selected["selection_id"],
            "fault_id": fault_id,
            "site": spec["site"],
            "receiver_signals": spec["receiver_signals"],
        }
        records_by_module[str(spec["site"]["module"])].append(record)
        manifest_sites.append(record)

    package_name = "f2a_stage5_g_trace_pkg"
    parts = [
        "`timescale 1ns/1ps\n",
        "// Auto-generated Stage-5 comprehensive golden compact monitor.\n",
        trace_writer_package(
            package_name,
            args.trace_output,
            ("H", "GOLDEN"),
        ),
    ]
    for module_name in sorted(records_by_module):
        records = sorted(
            records_by_module[module_name],
            key=lambda item: item["selection_id"],
        )
        parts.append(
            golden_monitor_module(module_name, records, package_name)
        )
    monitor_text = "\n".join(parts)
    validate_generated_monitor_text(monitor_text, "golden monitor")
    atomic_write_text(args.output.resolve(), monitor_text, force=args.force)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": utc_now(),
        "kind": "stage5_comprehensive_golden_monitor",
        "trace_writer": "shared_file_descriptor_immediate_flush_no_final_block",
        "campaign": str(args.campaign.resolve()),
        "campaign_digest_sha256": campaign["campaign_digest_sha256"],
        "trace_output": str(args.trace_output.resolve()),
        "selected_site_count": len(manifest_sites),
        "bound_module_count": len(records_by_module),
        "sites": manifest_sites,
    }
    write_json(args.manifest.resolve(), manifest, force=args.force)
    print(f"Golden monitor       : {args.output.resolve()}")
    print(f"Golden manifest      : {args.manifest.resolve()}")
    print(f"Selected sites       : {len(manifest_sites)}")
    print(f"Bound modules        : {len(records_by_module)}")
    print("Trace writer         : one shared file descriptor")
    return 0


def command_make_fault_monitor(args: argparse.Namespace) -> int:
    spec = load_json(args.fault_json.resolve(), "Stage-5 fault spec")
    if spec.get("stage") != STAGE5_FAULT_MARKER:
        raise Stage5Error("not a Stage-5 fault spec")
    text = fault_monitor_module(spec, args.trace_output)
    validate_generated_monitor_text(text, "fault monitor")
    atomic_write_text(args.output.resolve(), text, force=args.force)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": utc_now(),
        "kind": "stage5_fault_monitor",
        "trace_writer": "shared_file_descriptor_immediate_flush_no_final_block",
        "fault_id": spec["fault_id"],
        "fault_spec": str(args.fault_json.resolve()),
        "fault_spec_digest_sha256": spec["fault_spec_digest_sha256"],
        "trace_output": str(args.trace_output.resolve()),
        "module": spec["site"]["module"],
        "source_net": spec["site"]["source_net"],
        "temporary_pre_fault_net": spec["modification"][
            "temporary_pre_fault_net"
        ],
        "receiver_signals": spec["receiver_signals"],
    }
    write_json(args.manifest.resolve(), manifest, force=args.force)
    print(f"Fault monitor        : {args.output.resolve()}")
    print(f"Fault manifest       : {args.manifest.resolve()}")
    print("Trace writer         : one shared file descriptor")
    return 0


# ---------------------------------------------------------------------------
# Trace parsing, splitting, comparison, and oracle creation
# ---------------------------------------------------------------------------


def open_text_auto(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def normalize_scope(scope: str) -> str:
    value = scope.strip()
    if "." in value:
        value = value.rsplit(".", 1)[0]
    return value


def command_split_golden_trace(args: argparse.Namespace) -> int:
    """Atomically split a comprehensive golden trace into one gzip per TS site.

    The split is first built in a temporary directory.  A malformed/interleaved
    row therefore cannot leave a partially valid cache behind.  Existing cache
    files are replaced only after the entire source trace has parsed cleanly.
    """

    source = args.trace.resolve()
    output_dir = args.output_dir.resolve()
    if not source.is_file():
        raise Stage5Error(f"golden trace not found: {source}")
    output_dir.mkdir(parents=True, exist_ok=True)

    temporary_dir = Path(
        tempfile.mkdtemp(prefix=".stage5_golden_split_", dir=output_dir)
    )
    handles: dict[str, Any] = {}
    counts: Counter[str] = Counter()
    header_count = 0
    source_sha256 = sha256_file(source)

    try:
        with source.open("r", encoding="utf-8", errors="replace") as stream:
            for line_number, line in enumerate(stream, start=1):
                stripped = line.rstrip("\n")
                if not stripped:
                    continue
                fields = stripped.split("\t")
                record_type = fields[0]

                if record_type == "H":
                    header_count += 1
                    continue
                if record_type in {"G", "GA"}:
                    expected_fields = 7
                elif record_type == "GS":
                    # Backward compatibility with Stage-5 v1.0.6 traces.
                    expected_fields = 5
                else:
                    raise Stage5Error(
                        "unrecognized/corrupted golden trace row "
                        f"at line {line_number}: {stripped[:200]!r}"
                    )

                if len(fields) != expected_fields:
                    raise Stage5Error(
                        "malformed golden trace row "
                        f"at line {line_number}: type={record_type!r}, "
                        f"expected_fields={expected_fields}, actual_fields={len(fields)}, "
                        f"row={stripped[:200]!r}"
                    )

                selection_id = fields[1]
                if not SELECTION_ID_RE.fullmatch(selection_id):
                    raise Stage5Error(
                        "invalid selection ID in golden trace "
                        f"at line {line_number}: {selection_id!r}; "
                        "the trace is likely interleaved/corrupted"
                    )

                handle = handles.get(selection_id)
                if handle is None:
                    path = temporary_dir / f"{selection_id}.trace.tsv.gz"
                    handle = gzip.open(path, "wt", encoding="utf-8")
                    handles[selection_id] = handle
                handle.write(line)
                counts[selection_id] += 1

        for handle in handles.values():
            handle.close()
        handles.clear()

        if header_count != 1:
            raise Stage5Error(
                "golden trace must contain exactly one shared header; "
                f"found {header_count}. Multiple headers indicate that bound "
                "monitors opened the trace independently."
            )
        if not counts:
            raise Stage5Error("golden trace contained no G/GA/GS site records")

        final_paths = {
            selection_id: output_dir / f"{selection_id}.trace.tsv.gz"
            for selection_id in counts
        }
        if not args.force:
            existing = [path for path in final_paths.values() if path.exists()]
            if existing:
                raise Stage5Error(
                    f"split golden trace already exists: {existing[0]}"
                )

        if args.force:
            for stale in output_dir.glob("TS*.trace.tsv.gz"):
                stale.unlink()

        for selection_id, final_path in final_paths.items():
            (temporary_dir / final_path.name).replace(final_path)

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "generated_at_utc": utc_now(),
            "source_trace": str(source),
            "source_trace_sha256": source_sha256,
            "shared_header_count": header_count,
            "selection_trace_count": len(counts),
            "row_counts": dict(sorted(counts.items())),
            "split_is_atomic": True,
        }
        write_json(args.manifest.resolve(), manifest, force=args.force)
        if args.delete_source:
            source.unlink()
        print(f"Golden site traces   : {len(counts)}")
        print(f"Shared headers       : {header_count}")
        print(f"Output directory     : {output_dir}")
        print(f"Split manifest       : {args.manifest.resolve()}")
        return 0
    finally:
        for handle in handles.values():
            try:
                handle.close()
            except Exception:
                pass
        shutil.rmtree(temporary_dir, ignore_errors=True)


def parse_golden_trace(
    path: Path,
    selection_id: str,
) -> tuple[dict[str, SampleSeries], dict[str, dict[str, bool]]]:
    series: dict[str, SampleSeries] = defaultdict(SampleSeries)
    summary: dict[str, dict[str, bool]] = {}

    def mark_source(scope: str, value: str) -> None:
        entry = summary.setdefault(scope, {"seen0": False, "seen1": False})
        if value == "0":
            entry["seen0"] = True
        elif value == "1":
            entry["seen1"] = True

    with open_text_auto(path) as stream:
        for raw in stream:
            fields = raw.rstrip("\n").split("\t")
            if not fields:
                continue
            if fields[0] == "G" and len(fields) == 7:
                if fields[1] != selection_id:
                    continue
                cycle = int(fields[2])
                time_value = int(fields[3])
                scope = normalize_scope(fields[4])
                source_value = fields[5].lower()
                series[scope].add(cycle, time_value, (source_value, fields[6]))
                mark_source(scope, source_value)
            elif fields[0] == "GA" and len(fields) == 7:
                if fields[1] != selection_id:
                    continue
                scope = normalize_scope(fields[4])
                channel = fields[5]
                if channel != "SRC":
                    raise Stage5Error(
                        f"invalid golden activity channel {channel!r}: {path}"
                    )
                mark_source(scope, fields[6].lower())
            elif fields[0] == "GS" and len(fields) == 5:
                # Backward compatibility with traces generated by v1.0.6.
                if fields[1] != selection_id:
                    continue
                scope = normalize_scope(fields[2])
                entry = summary.setdefault(
                    scope, {"seen0": False, "seen1": False}
                )
                entry["seen0"] = entry["seen0"] or fields[3] == "1"
                entry["seen1"] = entry["seen1"] or fields[4] == "1"
    if not series:
        raise Stage5Error(f"no golden samples for {selection_id}: {path}")
    return dict(series), summary


def parse_fault_trace(
    path: Path,
    fault_id: str,
) -> tuple[dict[str, SampleSeries], dict[str, dict[str, bool]]]:
    series: dict[str, SampleSeries] = defaultdict(SampleSeries)
    summary: dict[str, dict[str, bool]] = {}

    def entry_for(scope: str) -> dict[str, bool]:
        return summary.setdefault(
            scope,
            {
                "pre_seen0": False,
                "pre_seen1": False,
                "observed_seen0": False,
                "observed_seen1": False,
            },
        )

    def mark(scope: str, channel: str, value: str) -> None:
        entry = entry_for(scope)
        if channel == "PRE":
            if value == "0":
                entry["pre_seen0"] = True
            elif value == "1":
                entry["pre_seen1"] = True
        elif channel == "OBS":
            if value == "0":
                entry["observed_seen0"] = True
            elif value == "1":
                entry["observed_seen1"] = True
        else:
            raise Stage5Error(
                f"invalid fault activity channel {channel!r}: {path}"
            )

    with open_text_auto(path) as stream:
        for raw in stream:
            fields = raw.rstrip("\n").split("\t")
            if not fields:
                continue
            if fields[0] == "F" and len(fields) == 8:
                if fields[1] != fault_id:
                    continue
                cycle = int(fields[2])
                time_value = int(fields[3])
                scope = normalize_scope(fields[4])
                pre_value = fields[5].lower()
                observed_value = fields[6].lower()
                series[scope].add(
                    cycle,
                    time_value,
                    (pre_value, observed_value, fields[7]),
                )
                mark(scope, "PRE", pre_value)
                mark(scope, "OBS", observed_value)
            elif fields[0] == "FA" and len(fields) == 7:
                if fields[1] != fault_id:
                    continue
                scope = normalize_scope(fields[4])
                mark(scope, fields[5], fields[6].lower())
            elif fields[0] == "FS" and len(fields) == 7:
                # Backward compatibility with traces generated by v1.0.6.
                if fields[1] != fault_id:
                    continue
                scope = normalize_scope(fields[2])
                entry = entry_for(scope)
                entry["pre_seen0"] = entry["pre_seen0"] or fields[3] == "1"
                entry["pre_seen1"] = entry["pre_seen1"] or fields[4] == "1"
                entry["observed_seen0"] = (
                    entry["observed_seen0"] or fields[5] == "1"
                )
                entry["observed_seen1"] = (
                    entry["observed_seen1"] or fields[6] == "1"
                )
    if not series:
        raise Stage5Error(f"no fault samples for {fault_id}: {path}")
    return dict(series), summary


def carry_forward_rows(series: SampleSeries) -> dict[int, tuple[int, tuple[str, ...]]]:
    return {cycle: (time_value, values) for cycle, time_value, values in series.rows}


def compare_series(
    golden: SampleSeries,
    fault: SampleSeries,
) -> list[tuple[int, int, tuple[str, ...], tuple[str, ...]]]:
    golden_changes = carry_forward_rows(golden)
    fault_changes = carry_forward_rows(fault)
    cycles = sorted(set(golden_changes) | set(fault_changes))
    golden_value: tuple[str, ...] | None = None
    fault_value: tuple[str, ...] | None = None
    golden_time = 0
    fault_time = 0
    differences: list[tuple[int, int, tuple[str, ...], tuple[str, ...]]] = []
    for cycle in cycles:
        if cycle in golden_changes:
            golden_time, golden_value = golden_changes[cycle]
        if cycle in fault_changes:
            fault_time, fault_value = fault_changes[cycle]
        if golden_value is None or fault_value is None:
            continue
        if golden_value != fault_value:
            differences.append(
                (cycle, max(golden_time, fault_time), golden_value, fault_value)
            )
    return differences


def pad_bits(value: str, width: int) -> str:
    lowered = value.lower()
    if len(lowered) >= width:
        return lowered[-width:]
    pad = lowered[0] if lowered and lowered[0] in "xz" else "0"
    return pad * (width - len(lowered)) + lowered


def receiver_bit_differences(
    golden_value: str,
    fault_value: str,
    width: int,
) -> list[dict[str, Any]]:
    golden_bits = pad_bits(golden_value, width)
    fault_bits = pad_bits(fault_value, width)
    result = []
    for index, (golden_bit, fault_bit) in enumerate(zip(golden_bits, fault_bits)):
        if golden_bit != fault_bit:
            result.append(
                {
                    "receiver_index": index,
                    "golden_value": golden_bit,
                    "fault_value": fault_bit,
                }
            )
    return result


def extract_log_evidence(path: Path) -> list[str]:
    if not path.is_file():
        return []
    patterns = re.compile(
        r"CRC32 PASS|CRC32 FAIL|EXIT SUCCESS|EXIT FAILURE|maximum cycle|"
        r"Simulation aborted|TEST\(S\) FAILED|\*E,|\*F,",
        re.IGNORECASE,
    )
    evidence: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for raw in stream:
            line = raw.strip()
            if line and patterns.search(line):
                evidence.append(line[:1000])
                if len(evidence) >= 40:
                    break
    return evidence


def functional_result(result_path: Path) -> str:
    if not result_path.is_file():
        return "MISSING_RESULT"
    value = result_path.read_text(encoding="utf-8", errors="replace").strip()
    return value or "EMPTY_RESULT"


def render_oracle_report(oracle: Mapping[str, Any]) -> str:
    local = oracle["local_characterization"]
    diagnostic = oracle["diagnostic_oracle"]
    lines = [
        "Fault2Assertion Stage 5 Diagnostic Oracle",
        "=" * 80,
        "",
        f"Fault ID             : {oracle['fault_id']}",
        f"Selection ID         : {oracle['selection_id']}",
        f"Fault class          : {oracle['fault_class']}",
        f"Polarity             : {oracle['polarity']}",
        f"Functional result    : {oracle['functional_result']['classification']}",
        f"Characterization     : {oracle['characterization_class']}",
        f"Oracle confidence    : {diagnostic['confidence']}",
        "",
        "Activation and injection",
        "-" * 80,
        f"Activated            : {local['activation']['activated']}",
        f"Required source value: {local['activation']['required_source_value']}",
        f"Injection effective  : {local['injection']['effective']}",
        f"Matched scopes       : {local['matched_scope_count']}",
        "",
        "Earliest diagnostic divergence",
        "-" * 80,
        f"Cycle                : {diagnostic.get('earliest_cycle')}",
        f"Simulation time      : {diagnostic.get('earliest_time')}",
        f"Scope                : {diagnostic.get('scope')}",
        f"Role                 : {diagnostic.get('signal_role')}",
        f"Expression           : {diagnostic.get('expression')}",
        f"Golden value         : {diagnostic.get('golden_value')}",
        f"Fault value          : {diagnostic.get('fault_value')}",
        "",
        "Storage",
        "-" * 80,
        "Faulty netlist retained: no",
        "VCD retained           : no",
        "Raw fault trace retained: no after oracle generation",
        "",
    ]
    return "\n".join(lines)


def render_sva_seed(oracle: Mapping[str, Any]) -> str:
    diagnostic = oracle["diagnostic_oracle"]
    fault_id = str(oracle["fault_id"])
    property_name = "p_" + fault_id.lower()
    expression = diagnostic.get("expression")
    cycle = diagnostic.get("earliest_cycle")
    expected = diagnostic.get("golden_value")
    if expression is None or cycle is None or expected not in {"0", "1"}:
        return (
            f"// {fault_id}: no cycle-local binary diagnostic target was available.\n"
            "// Use oracle.json as the ground-truth characterization record.\n"
        )
    return f"""// Auto-generated Stage-5 SVA seed for {fault_id}.
// This is an AI-training seed, not a formally validated final assertion.
// Resolve CLK, RESET_N, CYCLE_COUNTER, and TARGET in the assertion integration layer.

property {property_name};
  @(posedge CLK) disable iff (!RESET_N)
    (CYCLE_COUNTER == {cycle}) |-> (TARGET === 1'b{expected});
endproperty

assert property ({property_name});

// Suggested TARGET expression in scope {diagnostic.get('scope')}:
//   {expression}
"""


def command_analyze(args: argparse.Namespace) -> int:
    spec = load_json(args.fault_json.resolve(), "Stage-5 fault spec")
    fault_id = str(spec["fault_id"])
    selection_id = str(spec["selection_id"])
    golden_series, golden_summary = parse_golden_trace(
        args.golden_trace.resolve(), selection_id
    )
    fault_series, fault_summary = parse_fault_trace(
        args.fault_trace.resolve(), fault_id
    )
    common_scopes = sorted(set(golden_series) & set(fault_series))
    stuck_at = int(spec["stuck_at"])
    required = str(1 - stuck_at)
    receiver_signals = spec["receiver_signals"]
    receiver_width = len(receiver_signals)

    scope_reports: list[dict[str, Any]] = []
    global_candidates: list[dict[str, Any]] = []
    any_activated = False
    all_injection_effective = bool(common_scopes)
    any_branch_divergence = False
    any_receiver_divergence = False

    for scope in common_scopes:
        golden = golden_series[scope]
        fault = fault_series[scope]
        g_summary = golden_summary.get(scope, {})
        f_summary = fault_summary.get(scope, {})
        activated = bool(g_summary.get(f"seen{required}", False)) or (
            required in golden.known_values(0)
        )
        any_activated = any_activated or activated
        observed_known = fault.known_values(1)
        if f_summary.get("observed_seen0", False):
            observed_known.add("0")
        if f_summary.get("observed_seen1", False):
            observed_known.add("1")
        injection_effective = (
            bool(observed_known) and observed_known <= {str(stuck_at)}
        )
        all_injection_effective = all_injection_effective and injection_effective

        differences = compare_series(golden, fault)
        branch_differences = [item for item in differences if item[2][0] != item[3][1]]
        receiver_differences = [item for item in differences if item[2][1] != item[3][2]]
        any_branch_divergence = any_branch_divergence or bool(branch_differences)
        any_receiver_divergence = any_receiver_divergence or bool(receiver_differences)

        earliest_branch = branch_differences[0] if branch_differences else None
        earliest_receiver = receiver_differences[0] if receiver_differences else None
        receiver_bits: list[dict[str, Any]] = []
        if earliest_receiver is not None:
            receiver_bits = receiver_bit_differences(
                earliest_receiver[2][1], earliest_receiver[3][2], receiver_width
            )
            for bit in receiver_bits:
                metadata = receiver_signals[bit["receiver_index"]]
                global_candidates.append(
                    {
                        "cycle": earliest_receiver[0],
                        "time": earliest_receiver[1],
                        "scope": scope,
                        "signal_role": metadata["role"],
                        "receiver_index": bit["receiver_index"],
                        "expression": metadata["expression"],
                        "golden_value": bit["golden_value"],
                        "fault_value": bit["fault_value"],
                    }
                )
        if earliest_branch is not None:
            global_candidates.append(
                {
                    "cycle": earliest_branch[0],
                    "time": earliest_branch[1],
                    "scope": scope,
                    "signal_role": "injected_site",
                    "receiver_index": None,
                    "expression": spec["site"]["source_net"],
                    "golden_value": earliest_branch[2][0],
                    "fault_value": earliest_branch[3][1],
                }
            )
        scope_reports.append(
            {
                "scope": scope,
                "activated": activated,
                "injection_effective": injection_effective,
                "golden_source_known_values": sorted(golden.known_values(0)),
                "fault_pre_known_values": sorted(fault.known_values(0)),
                "fault_observed_known_values": sorted(observed_known),
                "earliest_branch_divergence_cycle": (
                    earliest_branch[0] if earliest_branch else None
                ),
                "earliest_receiver_divergence_cycle": (
                    earliest_receiver[0] if earliest_receiver else None
                ),
                "earliest_receiver_bit_differences": receiver_bits,
                "fault_summary": f_summary,
            }
        )

    result_value = functional_result(args.result.resolve())
    evidence = extract_log_evidence(args.xrun_log.resolve())
    if result_value == "OUTPUT_MISMATCH":
        functional_class = "SDC_OUTPUT_MISMATCH"
    elif result_value == "TIMEOUT":
        functional_class = "HANG_TIMEOUT"
    elif result_value in {"ERROR", "UNKNOWN", "MISSING_RESULT", "EMPTY_RESULT"}:
        functional_class = "SIMULATION_ERROR_OR_UNKNOWN"
    elif result_value in {"OUTPUT_MATCH", "PASS"}:
        functional_class = "OUTPUT_MATCH"
    else:
        functional_class = result_value

    if not common_scopes:
        characterization = "TRACE_SCOPE_MISMATCH"
    elif not any_activated:
        characterization = "NOT_ACTIVATED"
    elif not all_injection_effective:
        characterization = "INJECTION_ERROR"
    elif functional_class == "HANG_TIMEOUT":
        characterization = "DETECTED_HANG"
    elif functional_class == "SDC_OUTPUT_MISMATCH":
        characterization = "DETECTED_OUTPUT_CORRUPTION"
    elif any_receiver_divergence:
        characterization = "ARCHITECTURALLY_MASKED_AFTER_LOCAL_PROPAGATION"
    elif any_branch_divergence:
        characterization = "LOCALLY_MASKED_AFTER_SITE_DIVERGENCE"
    else:
        characterization = "FUNCTIONALLY_EQUIVALENT_UNDER_WORKLOAD"

    global_candidates.sort(
        key=lambda item: (
            int(item["cycle"]),
            0 if item["signal_role"] != "injected_site" else 1,
            str(item["scope"]),
            -1 if item["receiver_index"] is None else int(item["receiver_index"]),
        )
    )
    diagnostic = global_candidates[0] if global_candidates else {
        "cycle": None,
        "time": None,
        "scope": None,
        "signal_role": (
            "final_output_signature"
            if functional_class in {"SDC_OUTPUT_MISMATCH", "HANG_TIMEOUT"}
            else None
        ),
        "receiver_index": None,
        "expression": None,
        "golden_value": None,
        "fault_value": None,
    }
    if global_candidates and any_receiver_divergence and all_injection_effective:
        confidence = "high"
    elif global_candidates and all_injection_effective:
        confidence = "medium"
    else:
        confidence = "low"

    oracle = {
        "schema_version": SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "generated_at_utc": utc_now(),
        "stage": STAGE5_ORACLE_MARKER,
        "fault_id": fault_id,
        "selection_id": selection_id,
        "site_id": spec["site_id"],
        "design": spec["design"],
        "workload": spec["workload"],
        "fault_class": spec["fault_class"],
        "injection_kind": spec["injection_kind"],
        "polarity": spec["polarity"],
        "stuck_at": stuck_at,
        "fault_spec_digest_sha256": spec["fault_spec_digest_sha256"],
        "characterization_class": characterization,
        "functional_result": {
            "raw_result": result_value,
            "classification": functional_class,
            "log_evidence": evidence,
        },
        "local_characterization": {
            "matched_scope_count": len(common_scopes),
            "golden_only_scopes": sorted(set(golden_series) - set(fault_series)),
            "fault_only_scopes": sorted(set(fault_series) - set(golden_series)),
            "activation": {
                "activated": any_activated,
                "required_source_value": required,
            },
            "injection": {
                "effective": all_injection_effective,
                "expected_observed_value": str(stuck_at),
            },
            "branch_diverged": any_branch_divergence,
            "receiver_output_diverged": any_receiver_divergence,
            "scopes": scope_reports,
        },
        "diagnostic_oracle": {
            "oracle_kind": (
                "earliest_cycle_local_divergence"
                if global_candidates
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
            "candidate_count": len(global_candidates),
            "candidate_preview": global_candidates[:20],
            "assertion_seed_status": "template_not_formally_validated",
        },
        "storage_confirmation": {
            "faulty_netlist_retained": False,
            "vcd_retained": False,
            "raw_trace_may_be_deleted_after_this_oracle": True,
        },
    }
    oracle["oracle_digest_sha256"] = canonical_json_digest(
        {key: value for key, value in oracle.items() if key != "generated_at_utc"}
    )
    write_json(args.oracle_output.resolve(), oracle, force=args.force)
    atomic_write_text(
        args.report_output.resolve(), render_oracle_report(oracle), force=args.force
    )
    atomic_write_text(
        args.sva_output.resolve(), render_sva_seed(oracle), force=args.force
    )
    print(f"Fault ID             : {fault_id}")
    print(f"Characterization     : {characterization}")
    print(f"Functional result    : {functional_class}")
    print(f"Oracle confidence    : {confidence}")
    print(f"Oracle JSON          : {args.oracle_output.resolve()}")
    return 0


def command_aggregate(args: argparse.Namespace) -> int:
    campaign = load_json(args.campaign.resolve(), "Stage-5 campaign")
    oracle_dir = args.oracle_dir.resolve()
    expected_faults = [str(item["fault_id"]) for item in campaign["faults"]]
    records: list[dict[str, Any]] = []
    missing: list[str] = []
    for fault_id in expected_faults:
        path = oracle_dir / f"{fault_id}.json"
        if not path.is_file():
            missing.append(fault_id)
            continue
        oracle = load_json(path, f"oracle {fault_id}")
        if oracle.get("stage") != STAGE5_ORACLE_MARKER:
            raise Stage5Error(f"invalid oracle stage marker: {path}")
        records.append(oracle)
    records.sort(key=lambda item: item["fault_id"])

    args.output_dir.resolve().mkdir(parents=True, exist_ok=True)
    jsonl_path = args.output_dir.resolve() / "stage_05_oracles.jsonl"
    csv_path = args.output_dir.resolve() / "stage_05_summary.csv"
    report_path = args.output_dir.resolve() / "stage_05_report.txt"
    jsonl_text = "".join(
        json.dumps(item, sort_keys=False, ensure_ascii=False) + "\n"
        for item in records
    )
    atomic_write_text(jsonl_path, jsonl_text, force=args.force)

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "fault_id",
                "selection_id",
                "site_id",
                "fault_class",
                "polarity",
                "characterization_class",
                "functional_class",
                "activated",
                "injection_effective",
                "receiver_diverged",
                "earliest_cycle",
                "diagnostic_scope",
                "diagnostic_expression",
                "oracle_confidence",
                "oracle_digest_sha256",
            ],
        )
        writer.writeheader()
        for item in records:
            local = item["local_characterization"]
            diagnostic = item["diagnostic_oracle"]
            writer.writerow(
                {
                    "fault_id": item["fault_id"],
                    "selection_id": item["selection_id"],
                    "site_id": item["site_id"],
                    "fault_class": item["fault_class"],
                    "polarity": item["polarity"],
                    "characterization_class": item["characterization_class"],
                    "functional_class": item["functional_result"]["classification"],
                    "activated": local["activation"]["activated"],
                    "injection_effective": local["injection"]["effective"],
                    "receiver_diverged": local["receiver_output_diverged"],
                    "earliest_cycle": diagnostic["earliest_cycle"],
                    "diagnostic_scope": diagnostic["scope"],
                    "diagnostic_expression": diagnostic["expression"],
                    "oracle_confidence": diagnostic["confidence"],
                    "oracle_digest_sha256": item["oracle_digest_sha256"],
                }
            )

    characterization_counts = Counter(
        item["characterization_class"] for item in records
    )
    functional_counts = Counter(
        item["functional_result"]["classification"] for item in records
    )
    class_counts = Counter(item["fault_class"] for item in records)
    confidence_counts = Counter(
        item["diagnostic_oracle"]["confidence"] for item in records
    )
    cycles = [
        int(item["diagnostic_oracle"]["earliest_cycle"])
        for item in records
        if item["diagnostic_oracle"]["earliest_cycle"] is not None
    ]
    lines = [
        "Fault2Assertion Stage 5 Campaign Report",
        "=" * 80,
        "",
        f"Design                  : {campaign['design']}",
        f"Workload                : {campaign['workload']}",
        f"Expected fault instances: {len(expected_faults)}",
        f"Generated oracles       : {len(records)}",
        f"Missing oracles         : {len(missing)}",
        f"Campaign complete       : {not missing}",
        "",
        "Characterization counts",
        "-" * 80,
    ]
    lines.extend(f"{name:55s} {count:6d}" for name, count in sorted(characterization_counts.items()))
    lines.extend(["", "Functional outcome counts", "-" * 80])
    lines.extend(f"{name:55s} {count:6d}" for name, count in sorted(functional_counts.items()))
    lines.extend(["", "Fault classes", "-" * 80])
    lines.extend(f"{name:55s} {count:6d}" for name, count in sorted(class_counts.items()))
    lines.extend(["", "Oracle confidence", "-" * 80])
    lines.extend(f"{name:55s} {count:6d}" for name, count in sorted(confidence_counts.items()))
    lines.extend(["", "Earliest diagnostic cycle statistics", "-" * 80])
    if cycles:
        lines.extend(
            [
                f"Count : {len(cycles)}",
                f"Min   : {min(cycles)}",
                f"Median: {statistics.median(cycles)}",
                f"Max   : {max(cycles)}",
            ]
        )
    else:
        lines.append("No cycle-local diagnostic oracle was generated.")
    if missing:
        lines.extend(["", "Missing fault IDs", "-" * 80])
        lines.extend(missing)
    atomic_write_text(report_path, "\n".join(lines) + "\n", force=args.force)
    print(f"Expected faults       : {len(expected_faults)}")
    print(f"Generated oracles     : {len(records)}")
    print(f"Missing oracles       : {len(missing)}")
    print(f"Summary CSV           : {csv_path}")
    print(f"Summary report        : {report_path}")
    return 0 if not missing else 2


def command_validate(args: argparse.Namespace) -> int:
    campaign = load_json(args.campaign.resolve(), "Stage-5 campaign")
    if campaign.get("stage") != STAGE5_CAMPAIGN_MARKER:
        raise Stage5Error("campaign stage marker mismatch")
    faults = campaign.get("faults")
    if not isinstance(faults, list) or not faults:
        raise Stage5Error("campaign contains no faults")
    seen: set[str] = set()
    for record in faults:
        fault_id = str(record["fault_id"])
        if fault_id in seen:
            raise Stage5Error(f"duplicate campaign fault: {fault_id}")
        seen.add(fault_id)
        spec_path = Path(str(record["fault_spec"]))
        patch_path = Path(str(record["patch"]))
        spec = load_json(spec_path, f"fault spec {fault_id}")
        if spec["fault_id"] != fault_id:
            raise Stage5Error(f"fault-spec ID mismatch: {fault_id}")
        if not patch_path.is_file() or patch_path.stat().st_size == 0:
            raise Stage5Error(f"fault patch missing/empty: {patch_path}")
        source = Path(spec["mapped_netlist"]["path"])
        if not source.is_file():
            raise Stage5Error(f"golden netlist missing: {source}")
        if sha256_file(source) != spec["mapped_netlist"]["sha256"]:
            raise Stage5Error(f"golden netlist SHA mismatch for {fault_id}")
    print(f"Campaign             : {args.campaign.resolve()}")
    print(f"Fault specs validated: {len(faults)}")
    print("Stage-5 validation   : PASS")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stage-5 fault characterization and diagnostic-oracle tools"
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {PROGRAM_VERSION}"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser(
        "prepare", help="materialize all TF fault specs and small patches"
    )
    prepare.add_argument("--candidates", type=Path, required=True)
    prepare.add_argument("--selection", type=Path, required=True)
    prepare.add_argument("--site-catalog-tool", type=Path, required=True)
    prepare.add_argument("--policy", type=Path, required=True)
    prepare.add_argument("--output-root", type=Path, required=True)
    prepare.add_argument("--force", action="store_true")
    prepare.set_defaults(func=command_prepare)

    apply_cmd = subparsers.add_parser(
        "apply", help="create one run-local faulty netlist"
    )
    apply_cmd.add_argument("--fault-json", type=Path, required=True)
    apply_cmd.add_argument("--output-netlist", type=Path, required=True)
    apply_cmd.add_argument("--force", action="store_true")
    apply_cmd.set_defaults(func=command_apply)

    golden_monitor = subparsers.add_parser(
        "make-golden-monitor", help="generate one compact monitor for all selected sites"
    )
    golden_monitor.add_argument("--campaign", type=Path, required=True)
    golden_monitor.add_argument("--trace-output", type=Path, required=True)
    golden_monitor.add_argument("--output", type=Path, required=True)
    golden_monitor.add_argument("--manifest", type=Path, required=True)
    golden_monitor.add_argument("--force", action="store_true")
    golden_monitor.set_defaults(func=command_make_golden_monitor)

    fault_monitor = subparsers.add_parser(
        "make-fault-monitor", help="generate one compact fault-local monitor"
    )
    fault_monitor.add_argument("--fault-json", type=Path, required=True)
    fault_monitor.add_argument("--trace-output", type=Path, required=True)
    fault_monitor.add_argument("--output", type=Path, required=True)
    fault_monitor.add_argument("--manifest", type=Path, required=True)
    fault_monitor.add_argument("--force", action="store_true")
    fault_monitor.set_defaults(func=command_make_fault_monitor)

    split = subparsers.add_parser(
        "split-golden-trace", help="split the comprehensive golden trace by TS site"
    )
    split.add_argument("--trace", type=Path, required=True)
    split.add_argument("--output-dir", type=Path, required=True)
    split.add_argument("--manifest", type=Path, required=True)
    split.add_argument("--delete-source", action="store_true")
    split.add_argument("--force", action="store_true")
    split.set_defaults(func=command_split_golden_trace)

    analyze = subparsers.add_parser(
        "analyze", help="compare one fault trace and generate its diagnostic oracle"
    )
    analyze.add_argument("--fault-json", type=Path, required=True)
    analyze.add_argument("--golden-trace", type=Path, required=True)
    analyze.add_argument("--fault-trace", type=Path, required=True)
    analyze.add_argument("--result", type=Path, required=True)
    analyze.add_argument("--xrun-log", type=Path, required=True)
    analyze.add_argument("--oracle-output", type=Path, required=True)
    analyze.add_argument("--report-output", type=Path, required=True)
    analyze.add_argument("--sva-output", type=Path, required=True)
    analyze.add_argument("--force", action="store_true")
    analyze.set_defaults(func=command_analyze)

    aggregate = subparsers.add_parser(
        "aggregate", help="aggregate all per-fault oracle JSON files"
    )
    aggregate.add_argument("--campaign", type=Path, required=True)
    aggregate.add_argument("--oracle-dir", type=Path, required=True)
    aggregate.add_argument("--output-dir", type=Path, required=True)
    aggregate.add_argument("--force", action="store_true")
    aggregate.set_defaults(func=command_aggregate)

    validate = subparsers.add_parser(
        "validate", help="validate campaign metadata and every fault spec/patch"
    )
    validate.add_argument("--campaign", type=Path, required=True)
    validate.set_defaults(func=command_validate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Stage5Error as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: unexpected Stage-5 failure: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
