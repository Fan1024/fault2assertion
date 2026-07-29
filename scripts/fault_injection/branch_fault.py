#!/usr/bin/env python3
"""Create and apply CV32E40P branch stuck-at fault definitions.

Persistent campaign layout:

faults/cv32e40p/branchfault/
+-- population.json
+-- selection.json
+-- BF0001_SA0/
¦   +-- fault.json
¦   +-- fault.patch
+-- BF0001_SA1/
¦   +-- fault.json
¦   +-- fault.patch
+-- ...

A complete faulty netlist is intentionally NOT stored in each BF directory.
The ``apply`` command reconstructs one run-local faulty netlist from:

    immutable golden netlist + fault.json

This script uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import math
import random
import re
import shutil
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any, Iterable, Sequence


# -----------------------------------------------------------------------------
# Current experiment policy
# -----------------------------------------------------------------------------

SCHEMA_VERSION = "1.3"
FAULT_MODEL = "branch_stuck_at"
CONFIDENCE_LEVEL = 0.95
MARGIN_OF_ERROR = 0.05
CONSERVATIVE_P = 0.5
RANDOM_SEED = 20260724
MINIMUM_PER_NONEMPTY_STRATUM = 2
STRICT_BRANCH_MIN_FANOUT = 2
PAIR_POLARITIES = True

# Clock/reset/scan pins are excluded from the current functional branch-fault
# population. These criteria can be revised when campaign-v2 is defined.
EXCLUDED_SINK_PINS = {"CK", "RN", "SN", "SE", "SI"}

# Nangate-style standard-cell output pins. S is an output only for adder cells;
# it is a select input for mux cells and must not be globally treated as output.
DEFAULT_LOGIC_OUTPUT_PINS = {"Z", "ZN", "Y"}

DESIGN_PROFILES: dict[str, dict[str, Any]] = {
    "cv32e40p": {
        "region_rules": [
            (r"cs_registers", "csr_debug"),
            (r"load_store_unit", "lsu"),
            (
                r"if_stage|prefetch|obi_interface|aligner|compressed_decoder|fifo",
                "if_prefetch",
            ),
            (
                r"register_file|id_stage|decoder|controller|int_controller",
                "id_control_regfile",
            ),
            (r"alu_div|alu|mult|ex_stage|ff_one|popcnt", "execute"),
            (r"sleep_unit|clock_gate|core|top", "core_glue_sleep"),
        ],
        "default_region": "unclassified",
    }
}

FAULT_DIR_PATTERN = re.compile(r"^(?:BF\d{5}|BF\d{4}_SA[01])$")
FAULT_ID_PATTERN = re.compile(r"^BF\d{4}_SA[01]$")


# -----------------------------------------------------------------------------
# Generic helpers
# -----------------------------------------------------------------------------


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"ERROR: file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ERROR: invalid JSON in {path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise SystemExit(f"ERROR: expected a JSON object in {path}")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def ensure_design_supported(design: str) -> dict[str, Any]:
    try:
        return DESIGN_PROFILES[design]
    except KeyError as exc:
        supported = ", ".join(sorted(DESIGN_PROFILES))
        raise SystemExit(
            f"ERROR: unsupported design '{design}'. Supported designs: {supported}"
        ) from exc


def canonical_signal(expression: str) -> str:
    """Canonical key for connectivity comparison only."""
    return re.sub(r"\s+", "", expression.strip())


def is_constant(expression: str) -> bool:
    value = canonical_signal(expression).lower()
    if value == "":
        return True
    if value in {"0", "1", "1'b0", "1'b1", "1'bx", "1'bz"}:
        return True
    return bool(
        re.fullmatch(r"(?:\d+)?'[s]?[bBoOdDhH][0-9a-fA-FxXzZ?_]+", value)
    )


def is_simple_signal(expression: str) -> bool:
    """Accept one scalar/bit-select net; reject operators and concatenations."""
    value = expression.strip()
    if not value or is_constant(value):
        return False

    normal = r"[A-Za-z_$][A-Za-z0-9_$]*(?:\s*\[[^\]]+\])?"
    escaped = r"\\[^\s,()]+(?:\s*\[[^\]]+\])?"
    return bool(re.fullmatch(rf"(?:{normal}|{escaped})", value))


def is_standard_cell(cell_type: str) -> bool:
    """Match Nangate-style names such as NAND2_X1 and SDFFR_X1."""
    return bool(re.fullmatch(r"[A-Z][A-Z0-9_]*_X\d+", cell_type))


def cell_kind(cell_type: str) -> str:
    upper = cell_type.upper()
    if upper.startswith(("DFF", "SDFF")):
        return "flipflop"
    if upper.startswith("FA_"):
        return "full_adder"
    if upper.startswith("HA_"):
        return "half_adder"
    if upper.startswith(("CLKGATE", "CLKGATETST")):
        return "clock_gate"
    return "logic"


def output_pins(cell_type: str) -> set[str]:
    kind = cell_kind(cell_type)
    if kind == "flipflop":
        return {"Q", "QN"}
    if kind in {"full_adder", "half_adder"}:
        return {"S", "CO"}
    if kind == "clock_gate":
        return {"GCK"}
    return DEFAULT_LOGIC_OUTPUT_PINS


def fanout_bucket(fanout: int) -> str:
    if fanout == 2:
        return "2"
    if fanout <= 4:
        return "3_4"
    if fanout <= 8:
        return "5_8"
    return "gt_8"


def sink_role(cell_type: str, pin: str) -> str:
    kind = cell_kind(cell_type)
    upper = cell_type.upper()
    if kind == "flipflop" and pin == "D":
        return "sequential_data_input"
    if upper.startswith("MUX") and pin == "S":
        return "mux_select"
    if kind in {"full_adder", "half_adder"}:
        return "arithmetic_input"
    if kind == "clock_gate" and pin in {"E", "EN"}:
        return "clock_gate_enable"
    return "combinational_input"


def classify_region(module_name: str, profile: dict[str, Any]) -> str:
    for pattern, region in profile["region_rules"]:
        if re.search(pattern, module_name, flags=re.IGNORECASE):
            return str(region)
    return str(profile["default_region"])


# -----------------------------------------------------------------------------
# Verilog parsing
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Connection:
    pin: str
    expression: str
    expression_start: int
    expression_end: int


@dataclass(frozen=True)
class CellInstance:
    module: str
    cell_type: str
    instance: str
    statement_start: int
    statement_end: int
    connections: tuple[Connection, ...]


def mask_comments_keep_length(text: str) -> str:
    """Replace comment characters with spaces while preserving all indices."""
    chars = list(text)

    for match in re.finditer(r"/\*.*?\*/", text, flags=re.DOTALL):
        for index in range(match.start(), match.end()):
            if chars[index] != "\n":
                chars[index] = " "

    masked = "".join(chars)
    chars = list(masked)
    for match in re.finditer(r"//[^\n]*", masked):
        for index in range(match.start(), match.end()):
            chars[index] = " "

    return "".join(chars)


def iter_semicolon_statements(
    masked_text: str,
    start: int,
    end: int,
) -> Iterable[tuple[int, int]]:
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
        if not match:
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
            raise ValueError(f"unbalanced connection for pin {pin}")

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


def parse_standard_cells(netlist_text: str) -> tuple[list[str], list[CellInstance]]:
    masked = mask_comments_keep_length(netlist_text)
    modules: list[str] = []
    cells: list[CellInstance] = []

    module_pattern = re.compile(
        r"\bmodule\s+([^\s(]+)(?P<header>.*?)\s*;(?P<body>.*?)\bendmodule\b",
        flags=re.DOTALL,
    )

    for module_match in module_pattern.finditer(masked):
        module_name = module_match.group(1)
        modules.append(module_name)

        body_start = module_match.start("body")
        body_end = module_match.end("body")

        for statement_start, statement_end in iter_semicolon_statements(
            masked, body_start, body_end
        ):
            masked_statement = masked[statement_start:statement_end]
            original_statement = netlist_text[statement_start:statement_end]

            instance_match = re.match(
                r"\s*([A-Za-z][A-Za-z0-9_]*)\s+([^\s(]+)\s*\((.*)\)\s*;\s*$",
                masked_statement,
                flags=re.DOTALL,
            )
            if not instance_match:
                continue

            cell_type = instance_match.group(1)
            instance_name = instance_match.group(2)
            if not is_standard_cell(cell_type):
                continue

            connection_start, connection_end = instance_match.span(3)
            original_connections = original_statement[connection_start:connection_end]
            masked_connections = masked_statement[connection_start:connection_end]

            try:
                connections = parse_named_connections(
                    original_connections,
                    masked_connections,
                    statement_start + connection_start,
                )
            except ValueError as exc:
                raise SystemExit(
                    f"ERROR: cannot parse {module_name}/{instance_name}: {exc}"
                ) from exc

            if not connections:
                raise SystemExit(
                    "ERROR: no named connections found for "
                    f"{module_name}/{instance_name}"
                )

            cells.append(
                CellInstance(
                    module=module_name,
                    cell_type=cell_type,
                    instance=instance_name,
                    statement_start=statement_start,
                    statement_end=statement_end,
                    connections=connections,
                )
            )

    if not modules:
        raise SystemExit("ERROR: no Verilog modules were parsed")
    if not cells:
        raise SystemExit("ERROR: no standard-cell instances were parsed")

    return modules, cells


def connection_index(
    cells: Sequence[CellInstance],
) -> dict[tuple[str, str, str], Connection]:
    result: dict[tuple[str, str, str], Connection] = {}
    for cell in cells:
        for connection in cell.connections:
            key = (cell.module, cell.instance, connection.pin)
            if key in result:
                raise SystemExit(
                    "ERROR: duplicate module/instance/pin connection key: "
                    + "/".join(key)
                )
            result[key] = connection
    return result


# -----------------------------------------------------------------------------
# Population inventory
# -----------------------------------------------------------------------------


def build_population(netlist_path: Path, design: str) -> dict[str, Any]:
    profile = ensure_design_supported(design)
    netlist_text = netlist_path.read_text(encoding="utf-8", errors="strict")
    modules, cells = parse_standard_cells(netlist_text)

    module_regions = {
        module: classify_region(module, profile)
        for module in modules
    }
    unclassified = sorted(
        module
        for module, region in module_regions.items()
        if region == profile["default_region"]
    )
    if unclassified:
        raise SystemExit(
            "ERROR: unclassified modules must be mapped before sampling:\n  "
            + "\n  ".join(unclassified)
        )

    drivers: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    sink_candidates: list[dict[str, Any]] = []
    exclusions: Counter[str] = Counter()

    for cell in cells:
        outputs = output_pins(cell.cell_type)
        for connection in cell.connections:
            key = (cell.module, canonical_signal(connection.expression))

            if connection.pin in outputs:
                if is_simple_signal(connection.expression):
                    drivers[key].append(
                        {
                            "cell_type": cell.cell_type,
                            "instance": cell.instance,
                            "pin": connection.pin,
                        }
                    )
                continue

            if connection.pin in EXCLUDED_SINK_PINS:
                exclusions[f"excluded_sink_pin:{connection.pin}"] += 1
                continue
            if is_constant(connection.expression):
                exclusions["constant_or_open_connection"] += 1
                continue
            if not is_simple_signal(connection.expression):
                exclusions["non_simple_expression"] += 1
                continue

            sink_candidates.append(
                {
                    "module": cell.module,
                    "region": module_regions[cell.module],
                    "source_net": connection.expression.strip(),
                    "source_key": canonical_signal(connection.expression),
                    "sink_instance": cell.instance,
                    "sink_cell_type": cell.cell_type,
                    "sink_pin": connection.pin,
                    "sink_role": sink_role(cell.cell_type, connection.pin),
                }
            )

    fanout_counts: Counter[tuple[str, str]] = Counter(
        (item["module"], item["source_key"])
        for item in sink_candidates
    )

    sites: list[dict[str, Any]] = []
    for item in sink_candidates:
        driver_key = (item["module"], item["source_key"])
        fanout = fanout_counts[driver_key]

        if fanout < STRICT_BRANCH_MIN_FANOUT:
            exclusions["fanout_less_than_2"] += 1
            continue

        source_drivers = drivers.get(driver_key, [])
        if len(source_drivers) == 1:
            driver: dict[str, str] | None = source_drivers[0]
            source_class = (
                "sequential_output"
                if cell_kind(driver["cell_type"]) == "flipflop"
                else "combinational_output"
            )
        elif len(source_drivers) == 0:
            driver = None
            source_class = "hierarchy_boundary"
        else:
            exclusions["multiple_local_drivers"] += 1
            continue

        bucket = fanout_bucket(fanout)
        stratum = "|".join([item["region"], source_class, bucket])
        site_key = "|".join(
            [item["module"], item["sink_instance"], item["sink_pin"]]
        )

        sites.append(
            {
                **item,
                "site_key": site_key,
                "source_fanout": fanout,
                "source_class": source_class,
                "source_driver": driver,
                "fanout_bucket": bucket,
                "stratum": stratum,
            }
        )

    sites.sort(
        key=lambda site: (
            site["region"],
            site["module"],
            site["source_key"],
            site["sink_instance"],
            site["sink_pin"],
        )
    )

    for index, site in enumerate(sites, start=1):
        site["site_id"] = f"BS{index:06d}"

    if len({site["site_key"] for site in sites}) != len(sites):
        raise SystemExit("ERROR: duplicate eligible site_key detected")
    if not sites:
        raise SystemExit("ERROR: no eligible branch-fault locations were found")

    by_region = Counter(site["region"] for site in sites)
    by_module = Counter(site["module"] for site in sites)
    by_source_class = Counter(site["source_class"] for site in sites)
    by_fanout = Counter(site["fanout_bucket"] for site in sites)
    by_sink_role = Counter(site["sink_role"] for site in sites)
    by_stratum = Counter(site["stratum"] for site in sites)
    population_size = len(sites)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": utc_now(),
        "design": design,
        "fault_model": FAULT_MODEL,
        "source_netlist": str(netlist_path.resolve()),
        "source_netlist_sha256": sha256_file(netlist_path),
        "definitions": {
            "population_unit": (
                "one unique source-net to sink-instance.pin branch location"
            ),
            "strict_branch_min_fanout": STRICT_BRANCH_MIN_FANOUT,
            "excluded_sink_pins": sorted(EXCLUDED_SINK_PINS),
            "stratum": "functional_region|source_class|fanout_bucket",
        },
        "parser_summary": {
            "module_count": len(modules),
            "standard_cell_instance_count": len(cells),
            "module_regions": module_regions,
            "exclusion_counts": dict(sorted(exclusions.items())),
        },
        "population_summary": {
            "unique_branch_locations": population_size,
            "possible_fault_instances_with_sa0_sa1": 2 * population_size,
            "by_region": dict(sorted(by_region.items())),
            "by_module": dict(sorted(by_module.items())),
            "by_source_class": dict(sorted(by_source_class.items())),
            "by_fanout_bucket": dict(sorted(by_fanout.items())),
            "by_sink_role": dict(sorted(by_sink_role.items())),
            "by_stratum": dict(sorted(by_stratum.items())),
        },
        "sites": sites,
    }


# -----------------------------------------------------------------------------
# Current statistical sampling policy
# -----------------------------------------------------------------------------


def required_sample_size(population_size: int) -> tuple[int, float]:
    if population_size <= 0:
        raise ValueError("population_size must be positive")

    z_score = NormalDist().inv_cdf(
        1.0 - (1.0 - CONFIDENCE_LEVEL) / 2.0
    )
    pq = CONSERVATIVE_P * (1.0 - CONSERVATIVE_P)
    numerator = population_size * (z_score**2) * pq
    denominator = (
        (MARGIN_OF_ERROR**2) * (population_size - 1)
        + (z_score**2) * pq
    )
    return math.ceil(numerator / denominator), z_score


def allocate_strata(
    stratum_sizes: dict[str, int],
    sample_size: int,
) -> dict[str, int]:
    """Minimum quota plus proportional allocation using largest remainders."""
    if sample_size > sum(stratum_sizes.values()):
        raise ValueError("sample size exceeds the fault population")

    allocation = {
        stratum: min(size, MINIMUM_PER_NONEMPTY_STRATUM)
        for stratum, size in stratum_sizes.items()
    }
    base_total = sum(allocation.values())
    if base_total > sample_size:
        raise ValueError(
            f"minimum stratum quotas require {base_total} samples, "
            f"but the statistical budget is {sample_size}"
        )

    remaining = sample_size - base_total
    if remaining == 0:
        return allocation

    capacities = {
        stratum: stratum_sizes[stratum] - allocation[stratum]
        for stratum in stratum_sizes
    }
    total_capacity = sum(capacities.values())
    if total_capacity < remaining:
        raise ValueError("insufficient residual population capacity")

    exact_extra = {
        stratum: remaining * capacities[stratum] / total_capacity
        for stratum in stratum_sizes
    }
    floor_extra = {
        stratum: math.floor(value)
        for stratum, value in exact_extra.items()
    }

    for stratum, extra in floor_extra.items():
        allocation[stratum] += extra

    leftover = sample_size - sum(allocation.values())
    order = sorted(
        stratum_sizes,
        key=lambda stratum: (
            exact_extra[stratum] - floor_extra[stratum],
            stratum_sizes[stratum],
            stratum,
        ),
        reverse=True,
    )

    for stratum in order:
        if leftover == 0:
            break
        if allocation[stratum] < stratum_sizes[stratum]:
            allocation[stratum] += 1
            leftover -= 1

    if sum(allocation.values()) != sample_size:
        raise RuntimeError("stratum allocation failed to reach the sample size")
    if any(allocation[name] > stratum_sizes[name] for name in allocation):
        raise RuntimeError("stratum allocation exceeds a population")

    return allocation


def build_selection(
    population: dict[str, Any],
    random_seed: int,
) -> dict[str, Any]:
    sites = population.get("sites")
    if not isinstance(sites, list) or not sites:
        raise SystemExit("ERROR: population.json contains no sites")

    population_size = len(sites)
    required, z_score = required_sample_size(population_size)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for site in sites:
        grouped[str(site["stratum"])].append(site)

    stratum_sizes = {
        stratum: len(items)
        for stratum, items in grouped.items()
    }
    allocation = allocate_strata(stratum_sizes, required)

    rng = random.Random(random_seed)
    selected_sites: list[dict[str, Any]] = []
    for stratum in sorted(grouped):
        selected_sites.extend(
            rng.sample(grouped[stratum], allocation[stratum])
        )

    selected_sites.sort(key=lambda site: site["site_id"])
    if len({site["site_id"] for site in selected_sites}) != required:
        raise RuntimeError("selected sites are not unique")

    selected_locations: list[dict[str, Any]] = []
    for location_number, site in enumerate(selected_sites, start=1):
        location_id = f"BL{location_number:05d}"
        fault_pair_id = f"BF{location_number:04d}"
        sa0_id = f"{fault_pair_id}_SA0"
        sa1_id = f"{fault_pair_id}_SA1"
        stratum_population = stratum_sizes[site["stratum"]]
        stratum_sample = allocation[site["stratum"]]

        selected_locations.append(
            {
                "location_id": location_id,
                "fault_pair_id": fault_pair_id,
                "site_id": site["site_id"],
                "site_key": site["site_key"],
                "module": site["module"],
                "region": site["region"],
                "source_net": site["source_net"],
                "source_class": site["source_class"],
                "source_driver": site["source_driver"],
                "source_fanout": site["source_fanout"],
                "fanout_bucket": site["fanout_bucket"],
                "sink_instance": site["sink_instance"],
                "sink_cell_type": site["sink_cell_type"],
                "sink_pin": site["sink_pin"],
                "sink_role": site["sink_role"],
                "stratum": site["stratum"],
                "stratum_population": stratum_population,
                "stratum_sample": stratum_sample,
                "selection_probability": (
                    stratum_sample / stratum_population
                ),
                "analysis_weight": (
                    stratum_population / stratum_sample
                ),
                "faults": [
                    {
                        "fault_id": sa0_id,
                        "stuck_at": 0,
                        "paired_fault_id": sa1_id,
                    },
                    {
                        "fault_id": sa1_id,
                        "stuck_at": 1,
                        "paired_fault_id": sa0_id,
                    },
                ],
            }
        )

    allocation_payload = {
        stratum: {
            "population": stratum_sizes[stratum],
            "sample": allocation[stratum],
            "population_weight": (
                stratum_sizes[stratum] / population_size
            ),
            "selection_probability": (
                allocation[stratum] / stratum_sizes[stratum]
            ),
            "analysis_weight": (
                stratum_sizes[stratum] / allocation[stratum]
            ),
        }
        for stratum in sorted(stratum_sizes)
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": utc_now(),
        "design": population["design"],
        "fault_model": FAULT_MODEL,
        "source_netlist": population["source_netlist"],
        "source_netlist_sha256": population["source_netlist_sha256"],
        "statistical_target": {
            "population_unit": "unique branch location before polarity",
            "population_size": population_size,
            "confidence_level": CONFIDENCE_LEVEL,
            "z_score": z_score,
            "margin_of_error": MARGIN_OF_ERROR,
            "conservative_p": CONSERVATIVE_P,
            "required_unique_locations": required,
            "formula": (
                "ceil(N*z^2*p*(1-p) / "
                "(e^2*(N-1) + z^2*p*(1-p)))"
            ),
        },
        "sampling_policy": {
            "method": "stratified_random_without_replacement",
            "stratum_definition": [
                "functional_region",
                "source_class",
                "fanout_bucket",
            ],
            "minimum_per_nonempty_stratum": MINIMUM_PER_NONEMPTY_STRATUM,
            "random_seed": random_seed,
            "pair_sa0_sa1_at_each_location": PAIR_POLARITIES,
            "selected_unique_locations": required,
            "planned_fault_directories": required * 2,
        },
        "allocation_by_stratum": allocation_payload,
        "selected_locations": selected_locations,
    }


# -----------------------------------------------------------------------------
# Metadata materialization and run-local fault application
# -----------------------------------------------------------------------------


def flatten_faults(
    selection: dict[str, Any],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    result: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for location in selection.get("selected_locations", []):
        for fault in location.get("faults", []):
            result.append((location, fault))
    return result


def inject_branch_fault(
    original_text: str,
    connection: Connection,
    stuck_at: int,
) -> tuple[str, str]:
    if stuck_at not in {0, 1}:
        raise ValueError("stuck_at must be 0 or 1")

    original_expression = original_text[
        connection.expression_start:connection.expression_end
    ]
    if original_expression != connection.expression:
        raise RuntimeError("connection expression span does not match source text")

    replacement = f"1'b{stuck_at}"
    modified = (
        original_text[:connection.expression_start]
        + replacement
        + original_text[connection.expression_end:]
    )
    return modified, original_expression


def make_patch(
    original_text: str,
    modified_text: str,
    source_name: str,
    fault_id: str,
) -> str:
    lines = difflib.unified_diff(
        original_text.splitlines(keepends=True),
        modified_text.splitlines(keepends=True),
        fromfile=f"a/{source_name}",
        tofile=f"b/run-local/{fault_id}/fault_netlist.v",
        n=3,
    )
    return "".join(lines)


def validate_output_root(output_root: Path, force: bool) -> None:
    if not output_root.exists():
        output_root.mkdir(parents=True, exist_ok=True)
        return

    existing_faults = sorted(
        path
        for path in output_root.iterdir()
        if FAULT_DIR_PATTERN.fullmatch(path.name)
    )
    if existing_faults and not force:
        raise SystemExit(
            f"ERROR: {output_root} already contains BF directories. "
            "Use --force only when intentionally regenerating them."
        )

    if force:
        for path in existing_faults:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()


def validate_campaign_pair(
    population: dict[str, Any],
    selection: dict[str, Any],
) -> tuple[Path, str]:
    source_path = Path(str(population["source_netlist"])).resolve()
    if not source_path.is_file():
        raise SystemExit(f"ERROR: source netlist not found: {source_path}")

    expected_sha = str(population["source_netlist_sha256"])
    actual_sha = sha256_file(source_path)
    if actual_sha != expected_sha:
        raise SystemExit(
            "ERROR: source netlist SHA-256 changed after population scan.\n"
            f"  expected: {expected_sha}\n"
            f"  actual:   {actual_sha}"
        )

    if str(selection.get("source_netlist_sha256")) != expected_sha:
        raise SystemExit("ERROR: selection.json does not match population.json")
    if selection.get("design") != population.get("design"):
        raise SystemExit("ERROR: selection design does not match population design")

    return source_path, expected_sha


def materialize_faults(
    population: dict[str, Any],
    selection: dict[str, Any],
    output_root: Path,
    requested_fault_ids: set[str] | None,
    force: bool,
) -> tuple[int, int]:
    """Create only fault.json and fault.patch for selected faults."""
    source_path, expected_sha = validate_campaign_pair(population, selection)
    original_text = source_path.read_text(encoding="utf-8", errors="strict")
    _, cells = parse_standard_cells(original_text)
    connections = connection_index(cells)

    all_faults = flatten_faults(selection)
    known_ids = {str(fault["fault_id"]) for _, fault in all_faults}
    if not known_ids:
        raise SystemExit("ERROR: selection.json contains no faults")

    if requested_fault_ids is not None:
        invalid_format = sorted(
            item for item in requested_fault_ids
            if not FAULT_ID_PATTERN.fullmatch(item)
        )
        if invalid_format:
            raise SystemExit(
                "ERROR: invalid fault ID format: " + ", ".join(invalid_format)
            )
        unknown = sorted(requested_fault_ids - known_ids)
        if unknown:
            raise SystemExit("ERROR: unknown fault IDs: " + ", ".join(unknown))
        worklist = [
            pair
            for pair in all_faults
            if pair[1]["fault_id"] in requested_fault_ids
        ]
    else:
        worklist = all_faults

    if requested_fault_ids is None:
        validate_output_root(output_root, force=force)
    else:
        output_root.mkdir(parents=True, exist_ok=True)

    created = 0
    skipped = 0

    for location, fault in worklist:
        fault_id = str(fault["fault_id"])
        fault_dir = output_root / fault_id

        if fault_dir.exists():
            if force:
                shutil.rmtree(fault_dir)
            else:
                skipped += 1
                print(f"SKIP: {fault_dir} already exists", file=sys.stderr)
                continue

        key = (
            str(location["module"]),
            str(location["sink_instance"]),
            str(location["sink_pin"]),
        )
        try:
            connection = connections[key]
        except KeyError as exc:
            raise SystemExit(
                "ERROR: selected connection was not found during materialization: "
                + "/".join(key)
            ) from exc

        if canonical_signal(connection.expression) != canonical_signal(
            str(location["source_net"])
        ):
            raise SystemExit(
                f"ERROR: source expression mismatch for {fault_id}:\n"
                f"  selection: {location['source_net']}\n"
                f"  netlist:   {connection.expression}"
            )

        stuck_at = int(fault["stuck_at"])
        modified_text, original_expression = inject_branch_fault(
            original_text,
            connection,
            stuck_at,
        )
        patch_text = make_patch(
            original_text,
            modified_text,
            source_path.name,
            fault_id,
        )
        if not patch_text.strip():
            raise RuntimeError(f"empty patch generated for {fault_id}")

        replacement = f"1'b{stuck_at}"
        fault_dir.mkdir(parents=True, exist_ok=False)
        patch_output = fault_dir / "fault.patch"
        json_output = fault_dir / "fault.json"
        patch_output.write_text(patch_text, encoding="utf-8")

        metadata = {
            "schema_version": SCHEMA_VERSION,
            "generated_at_utc": utc_now(),
            "fault_id": fault_id,
            "fault_pair_id": location["fault_pair_id"],
            "location_id": location["location_id"],
            "paired_fault_id": fault["paired_fault_id"],
            "design": selection["design"],
            "fault_model": FAULT_MODEL,
            "stuck_at": stuck_at,
            "source_netlist": str(source_path),
            "source_netlist_sha256": expected_sha,
            "site": {
                "site_id": location["site_id"],
                "site_key": location["site_key"],
                "module": location["module"],
                "functional_region": location["region"],
                "source_net": location["source_net"],
                "source_class": location["source_class"],
                "source_driver": location["source_driver"],
                "source_fanout": location["source_fanout"],
                "fanout_bucket": location["fanout_bucket"],
                "sink_instance": location["sink_instance"],
                "sink_cell_type": location["sink_cell_type"],
                "sink_pin": location["sink_pin"],
                "sink_role": location["sink_role"],
            },
            "sampling": {
                "stratum": location["stratum"],
                "stratum_population": location["stratum_population"],
                "stratum_sample": location["stratum_sample"],
                "selection_probability": location["selection_probability"],
                "analysis_weight": location["analysis_weight"],
                "confidence_level": CONFIDENCE_LEVEL,
                "margin_of_error": MARGIN_OF_ERROR,
                "random_seed": selection["sampling_policy"]["random_seed"],
            },
            "modification": {
                "original_connection": original_expression,
                "replacement_connection": replacement,
                "method": (
                    "replace only the selected sink-pin branch expression"
                ),
            },
            "artifacts": {
                "metadata": "fault.json",
                "patch": "fault.patch",
                "fault_netlist_policy": (
                    "generated temporarily from the immutable golden netlist "
                    "when a simulation run starts"
                ),
            },
            "apply_command": (
                "python3 scripts/fault_injection/branch_fault.py apply "
                f"--fault-json {fault_id}/fault.json "
                "--output-netlist <run-dir>/work/fault_netlist.v"
            ),
            "results": {
                "status": "not_run",
                "path_template": "results/<workload>/<run_name>/",
                "note": (
                    "Simulation output directories are created only by the "
                    "fault simulation runner."
                ),
            },
        }
        write_json(json_output, metadata)

        if sha256_file(source_path) != expected_sha:
            raise RuntimeError("golden source netlist was modified unexpectedly")
        if replacement not in modified_text[
            max(0, connection.expression_start - 40):
            min(len(modified_text), connection.expression_start + 80)
        ]:
            raise RuntimeError(f"replacement validation failed for {fault_id}")

        created += 1

    return created, skipped


def apply_fault_metadata(
    fault_json_path: Path,
    output_netlist: Path,
    force: bool,
) -> Path:
    """Generate one run-local faulty netlist from golden + fault.json."""
    metadata = read_json(fault_json_path.resolve())

    try:
        source_path = Path(str(metadata["source_netlist"])).resolve()
        expected_sha = str(metadata["source_netlist_sha256"])
        fault_id = str(metadata["fault_id"])
        stuck_at = int(metadata["stuck_at"])
        site = metadata["site"]
        modification = metadata["modification"]
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(
            f"ERROR: incomplete or invalid fault metadata in "
            f"{fault_json_path}: {exc}"
        ) from exc

    if not FAULT_ID_PATTERN.fullmatch(fault_id):
        raise SystemExit(f"ERROR: invalid fault_id in metadata: {fault_id}")
    if stuck_at not in {0, 1}:
        raise SystemExit(f"ERROR: invalid stuck_at value in metadata: {stuck_at}")
    if not source_path.is_file():
        raise SystemExit(f"ERROR: source netlist not found: {source_path}")

    resolved_output = output_netlist.resolve()
    if resolved_output == source_path:
        raise SystemExit(
            "ERROR: output netlist must not overwrite the immutable golden netlist"
        )
    if resolved_output.exists() and not force:
        raise SystemExit(
            f"ERROR: output netlist already exists: {resolved_output}; "
            "use --force to replace it"
        )

    actual_sha = sha256_file(source_path)
    if actual_sha != expected_sha:
        raise SystemExit(
            "ERROR: golden source netlist SHA-256 does not match fault.json.\n"
            f"  expected: {expected_sha}\n"
            f"  actual:   {actual_sha}"
        )

    original_text = source_path.read_text(encoding="utf-8", errors="strict")
    _, cells = parse_standard_cells(original_text)
    connections = connection_index(cells)

    try:
        key = (
            str(site["module"]),
            str(site["sink_instance"]),
            str(site["sink_pin"]),
        )
        expected_source_net = str(site["source_net"])
        expected_original = str(modification["original_connection"])
        expected_replacement = str(modification["replacement_connection"])
    except (KeyError, TypeError) as exc:
        raise SystemExit(
            f"ERROR: malformed site/modification data in {fault_json_path}: {exc}"
        ) from exc

    try:
        connection = connections[key]
    except KeyError as exc:
        raise SystemExit(
            "ERROR: fault connection not found in golden netlist: "
            + "/".join(key)
        ) from exc

    if canonical_signal(connection.expression) != canonical_signal(
        expected_source_net
    ):
        raise SystemExit(
            f"ERROR: source expression mismatch for {fault_id}:\n"
            f"  fault.json: {expected_source_net}\n"
            f"  netlist:    {connection.expression}"
        )

    modified_text, original_expression = inject_branch_fault(
        original_text,
        connection,
        stuck_at,
    )
    actual_replacement = f"1'b{stuck_at}"

    if canonical_signal(original_expression) != canonical_signal(
        expected_original
    ):
        raise SystemExit(
            f"ERROR: original connection mismatch for {fault_id}:\n"
            f"  fault.json: {expected_original}\n"
            f"  netlist:    {original_expression}"
        )
    if canonical_signal(expected_replacement) != canonical_signal(
        actual_replacement
    ):
        raise SystemExit(
            f"ERROR: replacement mismatch for {fault_id}:\n"
            f"  fault.json: {expected_replacement}\n"
            f"  expected:   {actual_replacement}"
        )

    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    resolved_output.write_text(modified_text, encoding="utf-8")

    if sha256_file(source_path) != expected_sha:
        resolved_output.unlink(missing_ok=True)
        raise RuntimeError("golden source netlist was modified unexpectedly")
    if actual_replacement not in modified_text[
        max(0, connection.expression_start - 40):
        min(len(modified_text), connection.expression_start + 80)
    ]:
        resolved_output.unlink(missing_ok=True)
        raise RuntimeError(f"replacement validation failed for {fault_id}")

    print(f"Fault ID: {fault_id}")
    print(f"Golden netlist: {source_path}")
    print(f"Golden SHA-256: {expected_sha}")
    print(f"Wrote run-local fault netlist: {resolved_output}")
    print(f"Fault netlist SHA-256: {sha256_file(resolved_output)}")
    return resolved_output


# -----------------------------------------------------------------------------
# Reporting and commands
# -----------------------------------------------------------------------------


def print_population_summary(population: dict[str, Any]) -> None:
    summary = population["population_summary"]
    print(f"Design: {population['design']}")
    print(
        "Eligible unique branch locations: "
        f"{summary['unique_branch_locations']}"
    )
    print(
        "Complete SA0+SA1 fault population: "
        f"{summary['possible_fault_instances_with_sa0_sa1']}"
    )
    print("By functional region:")
    for region, count in summary["by_region"].items():
        print(f"  {region:24s} {count:6d}")


def print_selection_summary(selection: dict[str, Any]) -> None:
    target = selection["statistical_target"]
    policy = selection["sampling_policy"]
    print(f"Population size: {target['population_size']}")
    print(
        "Strict required unique locations "
        f"(95% confidence, +/-5%): {target['required_unique_locations']}"
    )
    print(f"Selected unique locations: {policy['selected_unique_locations']}")
    print(f"Planned BF directories: {policy['planned_fault_directories']}")
    print(f"Random seed: {policy['random_seed']}")


def resolve_root(output_root: Path) -> Path:
    return output_root.resolve()


def command_scan(args: argparse.Namespace) -> int:
    netlist = args.netlist.resolve()
    if not netlist.is_file():
        raise SystemExit(f"ERROR: netlist not found: {netlist}")

    output_root = resolve_root(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    output = output_root / "population.json"

    if output.exists() and not args.force:
        raise SystemExit(
            f"ERROR: {output} already exists; use --force to replace it"
        )

    population = build_population(netlist, args.design)
    write_json(output, population)
    print_population_summary(population)
    print(f"Wrote: {output}")
    return 0


def command_select(args: argparse.Namespace) -> int:
    output_root = resolve_root(args.output_root)
    population_path = output_root / "population.json"
    selection_path = output_root / "selection.json"

    if selection_path.exists() and not args.force:
        raise SystemExit(
            f"ERROR: {selection_path} already exists; use --force to replace it"
        )

    population = read_json(population_path)
    selection = build_selection(population, random_seed=args.seed)
    write_json(selection_path, selection)
    print_selection_summary(selection)
    print(f"Wrote: {selection_path}")
    return 0


def command_materialize(args: argparse.Namespace) -> int:
    output_root = resolve_root(args.output_root)
    population = read_json(output_root / "population.json")
    selection = read_json(output_root / "selection.json")
    requested = set(args.fault_id) if args.fault_id else None

    created, skipped = materialize_faults(
        population=population,
        selection=selection,
        output_root=output_root,
        requested_fault_ids=requested,
        force=args.force,
    )
    print(f"Created BF metadata directories: {created}")
    print(f"Skipped existing BF directories: {skipped}")
    print(f"Output root: {output_root}")
    return 0


def command_apply(args: argparse.Namespace) -> int:
    apply_fault_metadata(
        fault_json_path=args.fault_json,
        output_netlist=args.output_netlist,
        force=args.force,
    )
    return 0


def command_all(args: argparse.Namespace) -> int:
    scan_args = argparse.Namespace(
        netlist=args.netlist,
        design=args.design,
        output_root=args.output_root,
        force=args.force,
    )
    select_args = argparse.Namespace(
        output_root=args.output_root,
        seed=args.seed,
        force=args.force,
    )
    materialize_args = argparse.Namespace(
        output_root=args.output_root,
        fault_id=None,
        force=args.force,
    )

    command_scan(scan_args)
    command_select(select_args)
    command_materialize(materialize_args)
    return 0


def add_seed_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--seed",
        type=int,
        default=RANDOM_SEED,
        help=(
            "random seed for reproducible stratified sampling "
            f"(default: {RANDOM_SEED})"
        ),
    )


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Enumerate, sample, and materialize metadata for paired "
            "BF0001_SA0/BF0001_SA1 branch stuck-at faults."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser(
        "scan",
        help="enumerate the complete eligible strict branch population",
    )
    scan.add_argument("--netlist", type=Path, required=True)
    scan.add_argument(
        "--design",
        choices=sorted(DESIGN_PROFILES),
        default="cv32e40p",
    )
    scan.add_argument("--output-root", type=Path, required=True)
    scan.add_argument("--force", action="store_true")
    scan.set_defaults(function=command_scan)

    select = subparsers.add_parser(
        "select",
        help="compute the current +/-5%% sample and assign paired BF IDs",
    )
    select.add_argument("--output-root", type=Path, required=True)
    add_seed_argument(select)
    select.add_argument("--force", action="store_true")
    select.set_defaults(function=command_select)

    materialize = subparsers.add_parser(
        "materialize",
        help="generate BFxxxx_SAx/fault.json and fault.patch only",
    )
    materialize.add_argument("--output-root", type=Path, required=True)
    materialize.add_argument(
        "--fault-id",
        action="append",
        help=(
            "materialize only this BF ID; repeat for multiple IDs. "
            "Omit to generate the complete selected set."
        ),
    )
    materialize.add_argument("--force", action="store_true")
    materialize.set_defaults(function=command_materialize)

    apply_parser = subparsers.add_parser(
        "apply",
        help="generate one run-local fault_netlist.v from fault.json",
    )
    apply_parser.add_argument("--fault-json", type=Path, required=True)
    apply_parser.add_argument("--output-netlist", type=Path, required=True)
    apply_parser.add_argument("--force", action="store_true")
    apply_parser.set_defaults(function=command_apply)

    all_command = subparsers.add_parser(
        "all",
        help="run scan, select, and metadata materialization in sequence",
    )
    all_command.add_argument("--netlist", type=Path, required=True)
    all_command.add_argument(
        "--design",
        choices=sorted(DESIGN_PROFILES),
        default="cv32e40p",
    )
    all_command.add_argument("--output-root", type=Path, required=True)
    add_seed_argument(all_command)
    all_command.add_argument("--force", action="store_true")
    all_command.set_defaults(function=command_all)

    return parser


def main() -> int:
    args = make_parser().parse_args()
    return int(args.function(args))


if __name__ == "__main__":
    raise SystemExit(main())
