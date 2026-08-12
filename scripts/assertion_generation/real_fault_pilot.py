#!/usr/bin/env python3
"""Fault2Assertion Stage-6 real-fault Round-0 pilot.

This tool intentionally implements only the first real-fault engineering
closure:

    completed Stage-5 fault
      -> versioned visible context
      -> OpenAI Responses API
      -> one generated SVA property body
      -> golden Xcelium execution
      -> target-fault Xcelium execution
      -> structured Stage-6 verdict

It does not implement train/dev/test splitting, RAG, or feedback repair.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "1.0"
PROGRAM_VERSION = "0.1.0"
STAGE_NAME = "stage_06_real_fault_pilot"

PASS_STATE = "ORACLE_VALIDATED_CLEANED"

DEFAULT_PROFILE = "PILOT_TRAIN_V1"
DEFAULT_MAXCYCLES = 2_000_000

BEGIN_MARKER = "BEGIN_SVA"
END_MARKER = "END_SVA"


class PilotError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_json(
    path: Path,
    label: str,
) -> dict[str, Any]:

    try:
        value = json.loads(
            path.read_text(
                encoding="utf-8",
            )
        )

    except FileNotFoundError as exc:
        raise PilotError(
            f"{label} not found: {path}"
        ) from exc

    except json.JSONDecodeError as exc:
        raise PilotError(
            f"invalid {label} JSON {path}: {exc}"
        ) from exc

    if not isinstance(value, dict):
        raise PilotError(
            f"{label} must contain one JSON object: {path}"
        )

    return value


def write_json(
    path: Path,
    value: Mapping[str, Any],
    *,
    overwrite: bool = False,
) -> None:

    path = path.expanduser().resolve()

    if path.exists() and not overwrite:
        raise PilotError(
            f"refusing to overwrite existing file: {path}"
        )

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp = path.with_name(
        f".{path.name}.tmp"
    )

    temp.write_text(
        json.dumps(
            value,
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    temp.replace(path)


def write_text(
    path: Path,
    text: str,
    *,
    overwrite: bool = False,
) -> None:

    path = path.expanduser().resolve()

    if path.exists() and not overwrite:
        raise PilotError(
            f"refusing to overwrite existing file: {path}"
        )

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp = path.with_name(
        f".{path.name}.tmp"
    )

    temp.write_text(
        text,
        encoding="utf-8",
    )

    temp.replace(path)


def sha256_file(
    path: Path,
) -> str:

    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for block in iter(
            lambda: handle.read(
                1024 * 1024
            ),
            b"",
        ):
            digest.update(block)

    return digest.hexdigest()


def canonical_digest(
    value: Any,
) -> str:

    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    return hashlib.sha256(
        encoded
    ).hexdigest()


def load_campaign(
    campaign_root: Path,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
]:

    campaign_root = (
        campaign_root.resolve()
    )

    manifest_path = (
        campaign_root
        / "campaign_manifest.json"
    )

    state_path = (
        campaign_root
        / "campaign_state.json"
    )

    manifest = load_json(
        manifest_path,
        "Stage-5 campaign manifest",
    )

    state = load_json(
        state_path,
        "Stage-5 campaign state",
    )

    if (
        manifest.get("kind")
        != "stage5_full_site_campaign_manifest"
    ):
        raise PilotError(
            "unexpected Stage-5 campaign manifest kind"
        )

    if (
        state.get("kind")
        != "stage5_campaign_state"
    ):
        raise PilotError(
            "unexpected Stage-5 campaign state kind"
        )

    recorded = state.get(
        "manifest"
    )

    if not isinstance(
        recorded,
        dict,
    ):
        raise PilotError(
            "campaign state has no manifest provenance"
        )

    if (
        recorded.get(
            "digest_sha256"
        )
        != manifest.get(
            "manifest_digest_sha256"
        )
    ):
        raise PilotError(
            "campaign state/manifest digest mismatch"
        )

    return (
        manifest,
        state,
    )


def target_site_limit(
    state: Mapping[str, Any],
) -> int:

    target = state.get(
        "target"
    )

    if not isinstance(
        target,
        dict,
    ):
        raise PilotError(
            "campaign state has no target object"
        )

    value = target.get(
        "through_site_resolved"
    )

    if (
        not isinstance(
            value,
            int,
        )
        or value <= 0
    ):
        raise PilotError(
            "invalid through_site_resolved "
            "in campaign state"
        )

    return value


def fault_records(
    manifest: Mapping[str, Any],
) -> list[dict[str, Any]]:

    rows = manifest.get(
        "faults"
    )

    if not isinstance(
        rows,
        list,
    ):
        raise PilotError(
            "campaign manifest has no faults array"
        )

    result: list[
        dict[str, Any]
    ] = []

    for row in rows:
        if not isinstance(
            row,
            dict,
        ):
            raise PilotError(
                "campaign fault record "
                "is not an object"
            )

        result.append(
            dict(row)
        )

    return result


def resolve_fault_record(
    manifest: Mapping[str, Any],
    fault_id: str,
) -> dict[str, Any]:

    matches = [
        row
        for row in fault_records(
            manifest
        )
        if row.get(
            "fault_id"
        )
        == fault_id
    ]

    if len(matches) != 1:
        raise PilotError(
            "fault is not unique "
            f"in campaign: {fault_id}"
        )

    return matches[0]


def fault_artifacts(
    record: Mapping[str, Any],
) -> dict[str, Path]:

    fault_root = Path(
        str(
            record[
                "fault_root"
            ]
        )
    ).resolve()

    return {
        "fault_root":
            fault_root,

        "fault_json":
            Path(
                str(
                    record[
                        "fault_json"
                    ]
                )
            ).resolve(),

        "status":
            fault_root
            / "status.json",

        "routing":
            fault_root
            / "routing.json",

        "oracle":
            fault_root
            / "oracle"
            / "oracle.json",

        "prompt_context":
            fault_root
            / "oracle"
            / "prompt_context.json",

        "oracle_validation":
            fault_root
            / "oracle"
            / "validation.json",

        "native_result":
            fault_root
            / "native"
            / "run"
            / "result.json",

        "native_trace":
            fault_root
            / "native"
            / "trace.tsv",
    }


def validate_completed_fault(
    record: Mapping[str, Any],
) -> tuple[
    dict[str, Path],
    dict[str, Any],
    dict[str, Any],
]:

    paths = fault_artifacts(
        record
    )

    for key in (
        "fault_json",
        "status",
        "oracle",
        "prompt_context",
        "oracle_validation",
        "native_result",
    ):
        path = paths[key]

        if (
            not path.is_file()
            or path.stat().st_size
            == 0
        ):
            raise PilotError(
                "required Stage-5 "
                "artifact missing: "
                f"{key}: {path}"
            )

    status = load_json(
        paths["status"],
        "fault status",
    )

    validation = load_json(
        paths[
            "oracle_validation"
        ],
        "oracle validation",
    )

    if (
        status.get("state")
        != PASS_STATE
    ):
        raise PilotError(
            f"fault is not {PASS_STATE}: "
            f"{record.get('fault_id')} "
            f"-> {status.get('state')}"
        )

    if (
        validation.get(
            "status"
        )
        != "PASS"
    ):
        raise PilotError(
            "Stage-5 oracle "
            "validation is not PASS"
        )

    return (
        paths,
        status,
        validation,
    )


def select_candidates(
    campaign_root: Path,
    *,
    max_receivers: int,
) -> list[dict[str, Any]]:

    manifest, state = (
        load_campaign(
            campaign_root
        )
    )

    limit = target_site_limit(
        state
    )

    candidates: list[
        dict[str, Any]
    ] = []

    for record in fault_records(
        manifest
    ):
        if (
            int(
                record.get(
                    "site_order",
                    10**9,
                )
            )
            > limit
        ):
            continue

        paths = fault_artifacts(
            record
        )

        if (
            not paths[
                "status"
            ].is_file()
            or not paths[
                "fault_json"
            ].is_file()
        ):
            continue

        try:
            status = load_json(
                paths["status"],
                "fault status",
            )

            if (
                status.get(
                    "state"
                )
                != PASS_STATE
            ):
                continue

            if (
                status.get(
                    "native_status"
                )
                != "OUTPUT_MISMATCH"
            ):
                continue

            if not paths[
                "oracle_validation"
            ].is_file():
                continue

            validation = load_json(
                paths[
                    "oracle_validation"
                ],
                "oracle validation",
            )

            if (
                validation.get(
                    "status"
                )
                != "PASS"
            ):
                continue

            spec = load_json(
                paths[
                    "fault_json"
                ],
                "fault spec",
            )

        except PilotError:
            continue

        receivers = spec.get(
            "receiver_signals"
        )

        if (
            not isinstance(
                receivers,
                list,
            )
            or not receivers
        ):
            continue

        if (
            len(receivers)
            > max_receivers
        ):
            continue

        site = (
            spec.get("site")
            if isinstance(
                spec.get("site"),
                dict,
            )
            else {}
        )

        candidates.append(
            {
                "fault_id":
                    str(
                        record[
                            "fault_id"
                        ]
                    ),

                "site_order":
                    int(
                        record[
                            "site_order"
                        ]
                    ),

                "selection_id":
                    record.get(
                        "selection_id"
                    ),

                "fault_class":
                    record.get(
                        "fault_class"
                    ),

                "polarity":
                    record.get(
                        "polarity"
                    ),

                "module":
                    site.get(
                        "module"
                    ),

                "source_net":
                    site.get(
                        "source_net"
                    ),

                "source_kind":
                    site.get(
                        "source_kind"
                    ),

                "receiver_count":
                    len(
                        receivers
                    ),

                "oracle_validation":
                    str(
                        paths[
                            "oracle_validation"
                        ]
                    ),
            }
        )

    candidates.sort(
        key=lambda row: (
            int(
                row[
                    "receiver_count"
                ]
            ),
            int(
                row[
                    "site_order"
                ]
            ),
            str(
                row[
                    "fault_id"
                ]
            ),
        )
    )

    return candidates


def print_candidates(
    rows: Sequence[
        Mapping[str, Any]
    ],
    limit: int,
) -> None:

    print(
        "=" * 100
    )

    print(
        "Stage-6 pilot candidates: "
        "completed Stage-5 "
        "OUTPUT_MISMATCH faults"
    )

    print(
        "=" * 100
    )

    print(
        f"{'fault_id':18} "
        f"{'site':>5} "
        f"{'recv':>4} "
        f"{'polarity':8} "
        f"{'class':22} "
        "module"
    )

    print(
        "-" * 100
    )

    for row in rows[:limit]:
        print(
            f"{str(row['fault_id']):18} "
            f"{int(row['site_order']):5d} "
            f"{int(row['receiver_count']):4d} "
            f"{str(row.get('polarity')):8} "
            f"{str(row.get('fault_class'))[:22]:22} "
            f"{row.get('module')}"
        )

    print(
        "=" * 100
    )


def load_stage5_impl(
    root: Path,
) -> Any:

    path = (
        root
        / "scripts"
        / "fault_characterization"
        / "stage5_faults_v107_impl.py"
    )

    if not path.is_file():
        raise PilotError(
            "Stage-5 preserved "
            "implementation not found: "
            f"{path}"
        )

    name = (
        "f2a_stage6_"
        "stage5_helpers"
    )

    spec = (
        importlib.util
        .spec_from_file_location(
            name,
            path,
        )
    )

    if (
        spec is None
        or spec.loader
        is None
    ):
        raise PilotError(
            "cannot load Stage-5 "
            f"helper module: {path}"
        )

    module = (
        importlib.util
        .module_from_spec(
            spec
        )
    )

    sys.modules[
        name
    ] = module

    spec.loader.exec_module(
        module
    )

    return module


def load_policy_files(
    root: Path,
) -> dict[str, Any]:

    base = (
        root
        / "assertion_generation"
    )

    paths = {
        "model":
            base
            / "model_policy.json",

        "generation":
            base
            / "generation_policy.json",

        "visibility":
            base
            / "visibility_profiles.json",

        "sva_knowledge":
            base
            / "sva_knowledge.md",

        "prompt_template":
            base
            / "prompt_v001.txt",

        "engineering_faults":
            base
            / "engineering_faults.json",
    }

    for key, path in (
        paths.items()
    ):
        if not path.is_file():
            raise PilotError(
                "Stage-6 definition "
                "file missing: "
                f"{key}: {path}"
            )

    return {
        "paths":
            paths,

        "model":
            load_json(
                paths["model"],
                "model policy",
            ),

        "generation":
            load_json(
                paths[
                    "generation"
                ],
                "generation policy",
            ),

        "visibility":
            load_json(
                paths[
                    "visibility"
                ],
                "visibility profiles",
            ),

        "sva_knowledge":
            paths[
                "sva_knowledge"
            ].read_text(
                encoding="utf-8",
            ),

        "prompt_template":
            paths[
                "prompt_template"
            ].read_text(
                encoding="utf-8",
            ),
    }


def activity_flag(
    activity: Mapping[str, Any],
    name: str,
) -> Any:

    if name in activity:
        return activity[name]

    for value in activity.values():

        if not isinstance(value, dict):
            continue

        if name in value:
            return value[name]

    return None


def build_visible_context(
    *,
    fault_spec: Mapping[str, Any],
    stage5_prompt: Mapping[str, Any],
    profile_name: str,
    visibility_config: Mapping[str, Any],
) -> dict[str, Any]:

    profiles = visibility_config.get(
        "profiles"
    )

    if (
        not isinstance(profiles, dict)
        or profile_name not in profiles
    ):
        raise PilotError(
            f"unknown visibility profile: {profile_name}"
        )

    profile = profiles[
        profile_name
    ]

    if not isinstance(
        profile,
        dict,
    ):
        raise PilotError(
            f"invalid visibility profile: {profile_name}"
        )

    if (
        profile.get(
            "status",
            "active",
        )
        != "active"
    ):
        raise PilotError(
            f"visibility profile is not active: {profile_name}"
        )

    if profile.get(
        "include_local_divergence"
    ):
        raise PilotError(
            "current Stage-5 batch oracle does not compute "
            "earliest local divergence"
        )

    if profile.get(
        "include_faulty_dynamic_trace"
    ):
        raise PilotError(
            "faulty dynamic trace exposure is not enabled"
        )

    if profile.get(
        "include_existing_detector"
    ):
        raise PilotError(
            "existing-detector information is blocked"
        )

    site = fault_spec.get(
        "site"
    )

    if not isinstance(
        site,
        dict,
    ):
        raise PilotError(
            "fault spec has no site object"
        )

    receivers = fault_spec.get(
        "receiver_signals"
    )

    if (
        not isinstance(receivers, list)
        or not receivers
    ):
        raise PilotError(
            "fault spec has no receiver_signals"
        )

    signals: dict[str, Any] = {
        "site_i": {
            "role": "fault_site",
            "netlist_expression": site.get(
                "source_net"
            ),
        }
    }

    for index, receiver in enumerate(
        receivers
    ):

        if not isinstance(
            receiver,
            dict,
        ):
            raise PilotError(
                "receiver_signals contains a non-object"
            )

        expression = receiver.get(
            "expression"
        )

        if (
            not isinstance(
                expression,
                str,
            )
            or not expression
        ):
            raise PilotError(
                "receiver signal has no expression"
            )

        signals[
            f"recv_{index}_i"
        ] = {
            "role": "direct_receiver",
            "netlist_expression": expression,
        }

    context: dict[str, Any] = {
        "design": fault_spec.get(
            "design"
        ),

        "workload": fault_spec.get(
            "workload"
        ),
    }

    if profile.get(
        "include_exact_fault_site"
    ):

        context[
            "fault"
        ] = {
            "fault_class":
                fault_spec.get(
                    "fault_class"
                ),

            "polarity":
                fault_spec.get(
                    "polarity"
                ),

            "stuck_at":
                fault_spec.get(
                    "stuck_at"
                ),

            "module":
                site.get(
                    "module"
                ),

            "source_kind":
                site.get(
                    "source_kind"
                ),
        }

        context[
            "signals"
        ] = signals

    if profile.get(
        "include_golden_activity"
    ):

        activity = site.get(
            "activity"
        )

        if not isinstance(
            activity,
            dict,
        ):
            raise PilotError(
                "golden activity is unavailable"
            )

        seen_0 = activity_flag(
            activity,
            "seen_0",
        )

        seen_1 = activity_flag(
            activity,
            "seen_1",
        )

        stuck_at = fault_spec.get(
            "stuck_at"
        )

        if stuck_at == 0:
            activatable = (
                seen_1 is True
            )

        elif stuck_at == 1:
            activatable = (
                seen_0 is True
            )

        else:
            raise PilotError(
                f"invalid stuck_at value: {stuck_at}"
            )

        context[
            "golden_activity"
        ] = {
            "site_seen_0":
                seen_0,

            "site_seen_1":
                seen_1,

            "workload_activatable":
                activatable,
        }

    if profile.get(
        "include_training_observed_behavior"
    ):

        observed = stage5_prompt.get(
            "observed_behavior"
        )

        if not isinstance(
            observed,
            dict,
        ):
            raise PilotError(
                "Stage-5 prompt context has no observed_behavior"
            )

        native_status = observed.get(
            "native_status"
        )

        native_completion = observed.get(
            "native_completion"
        )

        context[
            "training_observation"
        ] = {
            "workload_completed":
                (
                    native_completion
                    == "COMPLETED"
                ),

            "architectural_output_corrupted":
                (
                    native_status
                    == "OUTPUT_MISMATCH"
                ),
        }

    return context

def render_prompt(
    template: str,
    sva_knowledge: str,
    visible_context:
        Mapping[str, Any],
) -> str:

    visible_json = (
        json.dumps(
            visible_context,
            indent=2,
            ensure_ascii=False,
        )
    )

    required = (
        "{{SVA_KNOWLEDGE}}",
        "{{VISIBLE_CONTEXT_JSON}}",
    )

    for token in required:
        if token not in template:
            raise PilotError(
                "prompt template is "
                "missing placeholder: "
                f"{token}"
            )

    text = template.replace(
        "{{SVA_KNOWLEDGE}}",
        sva_knowledge.strip(),
    )

    text = text.replace(
        "{{VISIBLE_CONTEXT_JSON}}",
        visible_json,
    )

    return (
        text.rstrip()
        + "\n"
    )


def register_engineering_fault(
    *,
    root: Path,
    fault_id: str,
    record:
        Mapping[str, Any],
    campaign_manifest_digest:
        str,
) -> None:

    path = (
        root
        / "assertion_generation"
        / "engineering_faults.json"
    )

    payload = load_json(
        path,
        "engineering fault registry",
    )

    rows = payload.get(
        "faults"
    )

    if not isinstance(
        rows,
        list,
    ):
        raise PilotError(
            "engineering_faults.json "
            "has no faults array"
        )

    for row in rows:
        if (
            isinstance(
                row,
                dict,
            )
            and row.get(
                "fault_id"
            )
            == fault_id
        ):
            if (
                row.get(
                    "exclude_from_future_scientific_split"
                )
                is not True
            ):
                raise PilotError(
                    "engineering fault "
                    "exists without "
                    "exclusion guardrail: "
                    f"{fault_id}"
                )

            return

    rows.append(
        {
            "fault_id":
                fault_id,

            "selection_id":
                record.get(
                    "selection_id"
                ),

            "site_id":
                record.get(
                    "site_id"
                ),

            "site_order":
                record.get(
                    "site_order"
                ),

            "purpose":
                "stage6_first_real_fault_engineering_closure",

            "exclude_from_future_scientific_split":
                True,

            "source_campaign_manifest_digest_sha256":
                campaign_manifest_digest,

            "registered_at_utc":
                utc_now(),
        }
    )

    payload[
        "updated_at_utc"
    ] = utc_now()

    write_json(
        path,
        payload,
        overwrite=True,
    )


def prepare_pilot(
    *,
    root: Path,
    campaign_root: Path,
    fault_id: str,
    profile_name: str,
) -> Path:

    policies = load_policy_files(
        root
    )

    manifest, state = (
        load_campaign(
            campaign_root
        )
    )

    record = (
        resolve_fault_record(
            manifest,
            fault_id,
        )
    )

    if (
        int(
            record.get(
                "site_order",
                10**9,
            )
        )
        > target_site_limit(
            state
        )
    ):
        raise PilotError(
            "selected fault is "
            "outside the completed "
            "Stage-5 site boundary"
        )

    (
        paths,
        status,
        validation,
    ) = validate_completed_fault(
        record
    )

    if (
        status.get(
            "native_status"
        )
        != "OUTPUT_MISMATCH"
    ):
        raise PilotError(
            "the first real-fault "
            "pilot intentionally "
            "requires OUTPUT_MISMATCH; "
            "got "
            f"{status.get('native_status')}"
        )

    fault_spec = load_json(
        paths[
            "fault_json"
        ],
        "fault spec",
    )

    stage5_prompt = load_json(
        paths[
            "prompt_context"
        ],
        "Stage-5 prompt context",
    )

    visible_context = (
        build_visible_context(
            fault_spec=
                fault_spec,

            stage5_prompt=
                stage5_prompt,

            profile_name=
                profile_name,

            visibility_config=
                policies[
                    "visibility"
                ],
        )
    )

    visible_context_digest = canonical_digest(
        visible_context
    )

    prompt = render_prompt(
        policies[
            "prompt_template"
        ],

        policies[
            "sva_knowledge"
        ],

        visible_context,
    )

    pilot_dir = (
        root
        / "runs"
        / "stage6"
        / f"pilot_{fault_id}"
    )

    if pilot_dir.exists():
        raise PilotError(
            "pilot directory already "
            f"exists: {pilot_dir}\n"
            "Review it first. Remove it "
            "manually only if an "
            "intentional clean rerun "
            "is required."
        )

    pilot_dir.mkdir(
        parents=True,
        exist_ok=False,
    )

    model_path = (
        policies[
            "paths"
        ][
            "model"
        ]
    )

    generation_path = (
        policies[
            "paths"
        ][
            "generation"
        ]
    )

    visibility_path = (
        policies[
            "paths"
        ][
            "visibility"
        ]
    )

    knowledge_path = (
        policies[
            "paths"
        ][
            "sva_knowledge"
        ]
    )

    prompt_template_path = (
        policies[
            "paths"
        ][
            "prompt_template"
        ]
    )

    campaign_manifest_path = (
        campaign_root
        / "campaign_manifest.json"
    )

    campaign_state_path = (
        campaign_root
        / "campaign_state.json"
    )

    golden_info = (
        fault_spec.get(
            "mapped_netlist"
        )
    )

    if not isinstance(
        golden_info,
        dict,
    ):
        raise PilotError(
            "fault spec has no "
            "mapped_netlist provenance"
        )

    golden_netlist = Path(
        str(
            golden_info.get(
                "path",
                "",
            )
        )
    ).resolve()

    if not golden_netlist.is_file():
        raise PilotError(
            "golden mapped "
            "netlist not found: "
            f"{golden_netlist}"
        )

    expected_golden_sha = str(
        golden_info.get(
            "sha256",
            "",
        )
    )

    actual_golden_sha = (
        sha256_file(
            golden_netlist
        )
    )

    if (
        actual_golden_sha
        != expected_golden_sha
    ):
        raise PilotError(
            "golden mapped netlist "
            "SHA no longer matches "
            "fault spec"
        )

    manifest_payload = {
        "schema_version":
            SCHEMA_VERSION,

        "program_version":
            PROGRAM_VERSION,

        "stage":
            STAGE_NAME,

        "kind":
            "stage6_real_fault_pilot_manifest",

        "created_at_utc":
            utc_now(),

        "fault_id":
            fault_id,

        "visibility_profile":
            profile_name,

        "visible_context_digest_sha256":
            visible_context_digest,

        "source_stage5": {
            "campaign_root":
                str(
                    campaign_root.resolve()
                ),

            "campaign_manifest":
                str(
                    campaign_manifest_path.resolve()
                ),

            "campaign_manifest_sha256":
                sha256_file(
                    campaign_manifest_path
                ),

            "campaign_manifest_digest_sha256":
                manifest.get(
                    "manifest_digest_sha256"
                ),

            "campaign_state":
                str(
                    campaign_state_path.resolve()
                ),

            "campaign_state_sha256":
                sha256_file(
                    campaign_state_path
                ),

            "fault_root":
                str(
                    paths[
                        "fault_root"
                    ]
                ),

            "fault_json":
                str(
                    paths[
                        "fault_json"
                    ]
                ),

            "fault_json_sha256":
                sha256_file(
                    paths[
                        "fault_json"
                    ]
                ),

            "fault_status":
                str(
                    paths[
                        "status"
                    ]
                ),

            "fault_status_sha256":
                sha256_file(
                    paths[
                        "status"
                    ]
                ),

            "oracle":
                str(
                    paths[
                        "oracle"
                    ]
                ),

            "oracle_sha256":
                sha256_file(
                    paths[
                        "oracle"
                    ]
                ),

            "prompt_context":
                str(
                    paths[
                        "prompt_context"
                    ]
                ),

            "prompt_context_sha256":
                sha256_file(
                    paths[
                        "prompt_context"
                    ]
                ),

            "oracle_validation":
                str(
                    paths[
                        "oracle_validation"
                    ]
                ),

            "oracle_validation_sha256":
                sha256_file(
                    paths[
                        "oracle_validation"
                    ]
                ),

            "validated_capability":
                validation.get(
                    "validated_capability"
                ),

            "baseline_native_status":
                status.get(
                    "native_status"
                ),
        },

        "golden_netlist": {
            "path":
                str(
                    golden_netlist
                ),

            "sha256":
                actual_golden_sha,
        },

        "stage6_definitions": {
            "model_policy":
                str(
                    model_path.resolve()
                ),

            "model_policy_sha256":
                sha256_file(
                    model_path
                ),

            "generation_policy":
                str(
                    generation_path.resolve()
                ),

            "generation_policy_sha256":
                sha256_file(
                    generation_path
                ),

            "visibility_profiles":
                str(
                    visibility_path.resolve()
                ),

            "visibility_profiles_sha256":
                sha256_file(
                    visibility_path
                ),

            "sva_knowledge":
                str(
                    knowledge_path.resolve()
                ),

            "sva_knowledge_sha256":
                sha256_file(
                    knowledge_path
                ),

            "prompt_template":
                str(
                    prompt_template_path.resolve()
                ),

            "prompt_template_sha256":
                sha256_file(
                    prompt_template_path
                ),
        },

        "prepared_artifacts": {
            "visible_context":
                "visible_context.json",

            "prompt":
                "prompt.txt",
        },

        "round_policy": {
            "round":
                0,

            "feedback_used":
                False,

            "purpose":
                "first_real_fault_engineering_closure",
        },
    }

    manifest_payload[
        "manifest_digest_sha256"
    ] = canonical_digest(
        {
            key: value
            for key, value
            in manifest_payload.items()
            if key
            != "created_at_utc"
        }
    )

    write_json(
        pilot_dir
        / "manifest.json",
        manifest_payload,
    )

    write_json(
        pilot_dir
        / "visible_context.json",
        visible_context,
    )

    write_text(
        pilot_dir
        / "prompt.txt",
        prompt,
    )

    register_engineering_fault(
        root=root,
        fault_id=fault_id,
        record=record,

        campaign_manifest_digest=
            str(
                manifest.get(
                    "manifest_digest_sha256"
                )
            ),
    )

    print(
        "=" * 80
    )

    print(
        "Stage-6 real-fault "
        "pilot preparation: PASS"
    )

    print(
        "=" * 80
    )

    print(
        f"Fault ID           : "
        f"{fault_id}"
    )

    print(
        f"Profile            : "
        f"{profile_name}"
    )

    print(
        f"Baseline status    : "
        f"{status.get('native_status')}"
    )

    print(
        f"Pilot directory    : "
        f"{pilot_dir}"
    )

    print(
        f"Visible context    : "
        f"{pilot_dir / 'visible_context.json'}"
    )

    print(
        f"Prompt             : "
        f"{pilot_dir / 'prompt.txt'}"
    )

    print(
        "Scientific split   : "
        "EXCLUDED "
        "(engineering fault registry)"
    )

    return pilot_dir


def parse_env_file(
    path: Path,
) -> dict[str, str]:

    if not path.is_file():
        raise PilotError(
            "OpenAI credential "
            f"file not found: {path}"
        )

    result: dict[
        str,
        str,
    ] = {}

    for raw in path.read_text(
        encoding="utf-8",
    ).splitlines():

        line = raw.strip()

        if (
            not line
            or line.startswith("#")
        ):
            continue

        if "=" not in line:
            raise PilotError(
                "malformed credential "
                f"line in {path}: {raw!r}"
            )

        key, value = (
            line.split(
                "=",
                1,
            )
        )

        key = key.strip()
        value = value.strip()

        if (
            len(value) >= 2
            and value[0]
            == value[-1]
            and value[0]
            in {
                "'",
                '"',
            }
        ):
            value = value[
                1:-1
            ]

        result[
            key
        ] = value

    return result


def extract_property(
    response_text: str,
) -> str:

    normalized = (
        response_text
        .replace(
            "\r\n",
            "\n",
        )
        .replace(
            "\r",
            "\n",
        )
    )

    if (
        normalized.count(
            BEGIN_MARKER
        )
        != 1
        or normalized.count(
            END_MARKER
        )
        != 1
    ):
        raise PilotError(
            "model response must "
            "contain exactly one "
            "BEGIN_SVA/END_SVA pair"
        )

    pattern = re.compile(
        rf"\A\s*{BEGIN_MARKER}"
        rf"[ \t]*\n"
        rf"(.*?)"
        rf"\n{END_MARKER}\s*\Z",
        re.DOTALL,
    )

    match = pattern.fullmatch(
        normalized
    )

    if match is None:
        raise PilotError(
            "model response violates "
            "the exact marker-delimited "
            "format"
        )

    body = (
        match.group(1)
        .strip()
    )

    if not body:
        raise PilotError(
            "generated property "
            "body is empty"
        )

    if len(body) > 8192:
        raise PilotError(
            "generated property body "
            "exceeds 8192 characters"
        )

    forbidden = (
        "assert property",
        "endproperty",
        "endmodule",
        "disable iff",
        "BEGIN_SVA",
        "END_SVA",
    )

    lower = body.lower()

    for token in forbidden:
        if (
            token.lower()
            in lower
        ):
            raise PilotError(
                "generated body "
                "contains forbidden "
                "wrapper token: "
                f"{token}"
            )

    if (
        "@("
        in body
        or "@( "
        in body
    ):
        raise PilotError(
            "generated body must "
            "not contain a clock event"
        )

    if ";" in body:
        raise PilotError(
            "generated body must "
            "not contain a semicolon"
        )

    return body


def request_openai(
    *,
    model_policy: Mapping[str, Any],
    prompt: str,
    credential_file: Path,
) -> tuple[
    str,
    dict[str, Any],
    dict[str, Any],
]:

    credentials = parse_env_file(
        credential_file
    )

    api_key = credentials.get(
        "OPENAI_API_KEY",
        "",
    ).strip()

    if (
        not api_key
        or api_key == "REPLACE_WITH_REAL_OPENAI_API_KEY"
    ):
        raise PilotError(
            "OPENAI_API_KEY is missing or still a placeholder"
        )

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise PilotError(
            "OpenAI Python SDK is not installed; "
            "install requirements-stage6.txt"
        ) from exc

    model = str(
        model_policy.get(
            "model",
            "",
        )
    ).strip()

    reasoning_effort = str(
        model_policy.get(
            "reasoning_effort",
            "medium",
        )
    ).strip()

    max_output_tokens = int(
        model_policy.get(
            "max_output_tokens",
            32768,
        )
    )

    if not model:
        raise PilotError(
            "model_policy.json has no model"
        )

    if max_output_tokens <= 0:
        raise PilotError(
            "model max_output_tokens must be positive"
        )

    request_record = {
        "schema_version": SCHEMA_VERSION,
        "provider": "openai",
        "api": "responses",
        "model": model,
        "reasoning": {
            "effort": reasoning_effort,
        },
        "max_output_tokens": max_output_tokens,
        "store": False,
        "prompt_sha256": hashlib.sha256(
            prompt.encode("utf-8")
        ).hexdigest(),
        "prompt_bytes": len(
            prompt.encode("utf-8")
        ),
        "requested_at_utc": utc_now(),
    }

    client = OpenAI(
        api_key=api_key,
        timeout=300.0,
        max_retries=2,
    )

    response = client.responses.create(
        model=model,
        reasoning={
            "effort": reasoning_effort,
        },
        input=prompt,
        max_output_tokens=max_output_tokens,
        store=False,
    )

    # IMPORTANT:
    # Serialize the entire Responses API object BEFORE validating output_text.
    # This preserves status, incomplete_details, usage, and output items even
    # when the visible answer is empty.
    response_record = response.model_dump(
        mode="json"
    )

    text = (
        response.output_text
        or ""
    ).strip()

    return (
        text,
        response_record,
        request_record,
    )


def generate_pilot(
    *,
    root: Path,
    fault_id: str,
    credential_file: Path,
) -> int:

    policies = load_policy_files(
        root
    )

    pilot_dir = (
        root
        / "runs"
        / "stage6"
        / f"pilot_{fault_id}"
    )

    if not pilot_dir.is_dir():
        raise PilotError(
            f"pilot is not prepared: {pilot_dir}"
        )

    property_path = (
        pilot_dir
        / "round0_property.sva"
    )

    if property_path.exists():
        raise PilotError(
            "round0_property.sva already exists; "
            "refusing to make a second API call for this prepared pilot"
        )

    manifest = load_json(
        pilot_dir / "manifest.json",
        "pilot manifest",
    )

    visible = load_json(
        pilot_dir / "visible_context.json",
        "visible context",
    )

    prompt_path = (
        pilot_dir
        / "prompt.txt"
    )

    if not prompt_path.is_file():
        raise PilotError(
            f"prompt not found: {prompt_path}"
        )

    prompt = prompt_path.read_text(
        encoding="utf-8"
    )

    # Provenance guard:
    # if a policy changed after prepare, rebuild the pilot before API usage.
    definitions = manifest.get(
        "stage6_definitions"
    )

    if not isinstance(
        definitions,
        dict,
    ):
        raise PilotError(
            "pilot manifest has no stage6_definitions"
        )

    current_model_policy = (
        root
        / "assertion_generation"
        / "model_policy.json"
    )

    current_prompt_template = (
        root
        / "assertion_generation"
        / "prompt_v001.txt"
    )

    current_knowledge = (
        root
        / "assertion_generation"
        / "sva_knowledge.md"
    )

    checks = (
        (
            "model_policy_sha256",
            current_model_policy,
        ),
        (
            "prompt_template_sha256",
            current_prompt_template,
        ),
        (
            "sva_knowledge_sha256",
            current_knowledge,
        ),
    )

    for digest_key, source_path in checks:

        recorded = definitions.get(
            digest_key
        )

        actual = sha256_file(
            source_path
        )

        if recorded != actual:
            raise PilotError(
                "Stage-6 definition changed after pilot preparation: "
                f"{source_path}\n"
                "Delete only this Stage-6 pilot directory and run prepare again."
            )

    actual_context_digest = canonical_digest(
        visible
    )

    recorded_context_digest = manifest.get(
        "visible_context_digest_sha256"
    )

    if (
        actual_context_digest
        != recorded_context_digest
    ):
        raise PilotError(
            "visible_context.json digest mismatch"
        )

    (
        response_text,
        response_record,
        request_record,
    ) = request_openai(
        model_policy=policies["model"],
        prompt=prompt,
        credential_file=(
            credential_file
            .expanduser()
            .resolve()
        ),
    )

    # Persist everything BEFORE output validation.
    write_json(
        pilot_dir
        / "round0_request.json",
        request_record,
    )

    write_json(
        pilot_dir
        / "round0_response.json",
        response_record,
    )

    write_text(
        pilot_dir
        / "round0_response.txt",
        (
            response_text + "\n"
            if response_text
            else ""
        ),
    )

    api_status = {
        "schema_version": SCHEMA_VERSION,
        "fault_id": fault_id,
        "model_requested": request_record.get(
            "model"
        ),
        "model_returned": response_record.get(
            "model"
        ),
        "response_id": response_record.get(
            "id"
        ),
        "response_status": response_record.get(
            "status"
        ),
        "incomplete_details": response_record.get(
            "incomplete_details"
        ),
        "usage": response_record.get(
            "usage"
        ),
        "nonempty_output_text": bool(
            response_text
        ),
        "recorded_at_utc": utc_now(),
    }

    write_json(
        pilot_dir
        / "round0_api_status.json",
        api_status,
    )

    if not response_text:

        raise PilotError(
            "OpenAI response contained no output_text. "
            "The complete response was preserved. "
            "Inspect round0_api_status.json and round0_response.json."
        )

    property_body = extract_property(
        response_text
    )

    write_text(
        property_path,
        property_body + "\n",
    )

    print("=" * 80)
    print(
        "Stage-6 real-fault API generation: PASS"
    )
    print("=" * 80)
    print(
        f"Fault ID          : {fault_id}"
    )
    print(
        f"Model             : {request_record['model']}"
    )
    print(
        "Reasoning effort  : "
        f"{request_record['reasoning']['effort']}"
    )
    print(
        f"Prompt bytes      : {request_record['prompt_bytes']}"
    )
    print(
        f"Response status   : {response_record.get('status')}"
    )
    print(
        f"Property          : {property_path}"
    )
    print()
    print("Generated property:")
    print("-" * 80)
    print(property_body)
    print("-" * 80)

    usage = response_record.get(
        "usage"
    )

    if usage is not None:
        print()
        print("API usage:")
        print(
            json.dumps(
                usage,
                indent=2,
                ensure_ascii=False,
            )
        )

    return 0

def sv_string(
    value: str,
) -> str:

    return (
        value
        .replace(
            "\\",
            "\\\\",
        )
        .replace(
            '"',
            '\\"',
        )
    )


def build_monitor(
    *,
    root: Path,
    fault_spec:
        Mapping[str, Any],
    property_body: str,
    trace_path: Path,
    run_label: str,
) -> str:

    impl = load_stage5_impl(
        root
    )

    fault_id = str(
        fault_spec[
            "fault_id"
        ]
    )

    site = fault_spec.get(
        "site"
    )

    if not isinstance(
        site,
        dict,
    ):
        raise PilotError(
            "fault spec has no site object"
        )

    receivers = (
        fault_spec.get(
            "receiver_signals"
        )
    )

    if (
        not isinstance(
            receivers,
            list,
        )
        or not receivers
    ):
        raise PilotError(
            "fault spec has "
            "no receiver signals"
        )

    module_name = str(
        site[
            "module"
        ]
    )

    source_net = str(
        site[
            "source_net"
        ]
    )

    tag = (
        hashlib.sha256(
            f"{fault_id}:"
            f"{run_label}"
            .encode(
                "utf-8"
            )
        )
        .hexdigest()[:10]
    )

    monitor_name = (
        f"f2a_stage6_assert_{tag}"
    )

    instance_name = (
        f"{monitor_name}_i"
    )

    port_lines = [
        "    input wire f2a_clk_i",
        "    input wire f2a_rst_ni",
        "    input wire [31:0] f2a_cycle_i",
        "    input wire site_i",
    ]

    bind_lines = [
        "    .f2a_clk_i("
        f"{impl.STAGE5_CLOCK_EXPRESSION}"
        ")",

        "    .f2a_rst_ni("
        "$root.tb_top.rst_n"
        ")",

        "    .f2a_cycle_i("
        "$root.tb_top.cycle_cnt_q"
        ")",

        "    .site_i("
        f"{impl.sv_expression(source_net)}"
        ")",
    ]

    for index, raw in enumerate(
        receivers
    ):
        if not isinstance(
            raw,
            dict,
        ):
            raise PilotError(
                "receiver signal "
                "record is invalid"
            )

        expression = str(
            raw[
                "expression"
            ]
        )

        alias = (
            f"recv_{index}_i"
        )

        port_lines.append(
            f"    input wire {alias}"
        )

        bind_lines.append(
            f"    .{alias}("
            f"{impl.sv_expression(expression)}"
            ")"
        )

    ports = ",\n".join(
        port_lines
    )

    binds = ",\n".join(
        bind_lines
    )

    indented_property = (
        "\n".join(
            "      " + line
            for line
            in property_body.splitlines()
        )
    )

    trace = sv_string(
        str(
            trace_path.resolve()
        )
    )

    fid = sv_string(
        fault_id
    )

    return f"""`timescale 1ns/1ps

module {monitor_name} (
{ports}
);
  integer f2a_trace_fd;
  integer f2a_assertion_events = 0;

  // The Stage-5 runner requires a non-empty compact trace artifact.
  // Stage 6 stores only a one-line execution marker here; assertion events
  // remain non-terminating and are parsed from xrun.log.
  initial begin
    f2a_trace_fd = $fopen("{trace}", "w");

    if (f2a_trace_fd == 0) begin
      $fatal(1, "F2A_STAGE6_TRACE_OPEN_FAILED");
    end

    $fwrite(
      f2a_trace_fd,
      "H\\tSTAGE6\\t{fid}\\t{run_label}\\n"
    );

    $fclose(
      f2a_trace_fd
    );
  end

  a_f2a_stage6_generated: assert property (
    @(posedge f2a_clk_i)
    disable iff (!f2a_rst_ni)
    (
{indented_property}
    )
  )
  else begin
    f2a_assertion_events =
      f2a_assertion_events + 1;

    $display(
      "F2A_STAGE6_ASSERT_EVENT fault_id={fid} index=%0d cycle=%0d time=%0t scope=%m",
      f2a_assertion_events,
      f2a_cycle_i,
      $time
    );
  end

endmodule

bind {impl.sv_identifier(module_name, "module")} {monitor_name} {instance_name} (
{binds}
);
"""


def sanitized_environment() -> dict[
    str,
    str,
]:

    env = os.environ.copy()

    for key in list(
        env
    ):
        if (
            key
            == "F2A_OPENAI_ENV"
            or key.startswith(
                "OPENAI_"
            )
        ):
            env.pop(
                key,
                None,
            )

    return env


def run_stage5_wrapper(
    *,
    root: Path,
    kind: str,
    fault_json: Path,
    golden_netlist: Path,
    monitor: Path,
    trace: Path,
    run_dir: Path,
    maxcycles: int,
) -> tuple[
    int,
    dict[str, Any],
]:

    env = (
        sanitized_environment()
    )

    env.update(
        {
            "F2A_ROOT":
                str(
                    root
                ),

            "STAGE5_PHASE":
                "run",

            "STAGE5_RUN_PURPOSE":
                "NATIVE_CHARACTERIZATION",

            "STAGE5_MM_RAM_PROFILE":
                "native",

            "STAGE5_TRACE_OUTPUT":
                str(
                    trace.resolve()
                ),

            "MAXCYCLES":
                str(
                    maxcycles
                ),

            "VCD":
                "0",

            "KEEP_WORK":
                "0",
        }
    )

    if kind == "golden":

        env[
            "GOLDEN_NETLIST"
        ] = str(
            golden_netlist.resolve()
        )

        command = [
            str(
                root
                / "scripts"
                / "run_xrun_stage5_golden.sh"
            ),

            str(
                monitor.resolve()
            ),

            str(
                run_dir.resolve()
            ),
        ]

    elif kind == "faulty":

        command = [
            str(
                root
                / "scripts"
                / "run_xrun_stage5_fault.sh"
            ),

            str(
                fault_json.resolve()
            ),

            str(
                monitor.resolve()
            ),

            str(
                run_dir.resolve()
            ),
        ]

    else:
        raise PilotError(
            f"unsupported run kind: {kind}"
        )

    print(
        "+ "
        + " ".join(
            command
        ),
        flush=True,
    )

    completed = subprocess.run(
        command,
        env=env,
        cwd=root,
        check=False,
    )

    result_path = (
        run_dir
        / "result.json"
    )

    if not result_path.is_file():
        raise PilotError(
            "Stage-5 wrapper "
            "produced no result.json: "
            f"kind={kind}, "
            f"rc={completed.returncode}"
        )

    result = load_json(
        result_path,
        f"{kind} Stage-5 result",
    )

    return (
        int(
            completed.returncode
        ),
        result,
    )


def copy_run_artifacts(
    run_dir: Path,
    pilot_dir: Path,
    prefix: str,
) -> None:

    required = {
        "result.json":
            pilot_dir
            / f"{prefix}_result.json",

        "xrun.log":
            pilot_dir
            / f"{prefix}_xrun.log",
    }

    for name, output in (
        required.items()
    ):
        source = (
            run_dir
            / name
        )

        if not source.is_file():
            raise PilotError(
                "missing run artifact: "
                f"{source}"
            )

        shutil.copy2(
            source,
            output,
        )


def parse_generated_events(
    log_path: Path,
    fault_id: str,
) -> list[dict[str, Any]]:

    pattern = re.compile(
        rf"F2A_STAGE6_ASSERT_EVENT"
        rf"\s+fault_id="
        rf"{re.escape(fault_id)}"
        rf"\s+index=(\d+)"
        rf"\s+cycle=(\d+)"
        rf"\s+time=([^\s]+)"
    )

    events: list[
        dict[str, Any]
    ] = []

    for line in (
        log_path.read_text(
            encoding="utf-8",
            errors="replace",
        )
        .splitlines()
    ):
        match = pattern.search(
            line
        )

        if not match:
            continue

        events.append(
            {
                "index":
                    int(
                        match.group(
                            1
                        )
                    ),

                "cycle":
                    int(
                        match.group(
                            2
                        )
                    ),

                "time":
                    match.group(
                        3
                    ),

                "line":
                    line.strip(),
            }
        )

    return events


def stage5_status_is_golden_valid(
    status: str,
) -> bool:

    return status in {
        "PASS",
        "OUTPUT_MATCH",
    }


def render_summary(
    final: Mapping[str, Any],
) -> str:

    generation = final[
        "generation"
    ]

    execution = final[
        "execution"
    ]

    verdict = final[
        "verdict"
    ]

    lines = [
        "Fault2Assertion Stage-6 Real-Fault Round-0 Pilot",
        "=" * 72,

        f"Fault ID              : {final['fault_id']}",

        f"Model                 : "
        f"{generation['model']}",

        f"Reasoning effort      : "
        f"{generation['reasoning_effort']}",

        f"Visibility profile    : "
        f"{generation['visibility_profile']}",

        f"Golden Stage-5 status : "
        f"{execution['golden']['stage5_status']}",

        f"Golden assertion hits : "
        f"{execution['golden']['assertion_event_count']}",

        f"Faulty Stage-5 status : "
        f"{execution['faulty']['stage5_status']}",

        f"Fault assertion hits  : "
        f"{execution['faulty']['assertion_event_count']}",

        f"Baseline native status: "
        f"{execution['faulty']['expected_stage5_status']}",

        "",

        f"Engineering closure   : "
        f"{verdict['engineering_closure']}",

        f"Candidate verdict     : "
        f"{verdict['candidate_verdict']}",

        f"Accepted              : "
        f"{verdict['accepted']}",

        "",

        "Interpretation:",

        "- Engineering closure PASS means API -> SVA -> Xcelium "
        "golden/faulty executed consistently.",

        "- TARGET_FAULT_DETECTED additionally requires zero "
        "golden events and >=1 faulty event.",
    ]

    first = (
        execution[
            "faulty"
        ].get(
            "first_assertion_event"
        )
    )

    if isinstance(
        first,
        dict,
    ):
        lines.append(
            "- First faulty assertion "
            "event cycle: "
            f"{first.get('cycle')}"
        )

    return (
        "\n".join(
            lines
        )
        + "\n"
    )


def execute_pilot(
    *,
    root: Path,
    campaign_root: Path,
    fault_id: str,
    credential_file: Path,
    maxcycles: int,
) -> int:

    policies = load_policy_files(
        root
    )

    pilot_dir = (
        root
        / "runs"
        / "stage6"
        / f"pilot_{fault_id}"
    )

    if not pilot_dir.is_dir():
        raise PilotError(
            "pilot is not prepared: "
            f"{pilot_dir}\n"
            "Run the prepare "
            "command first."
        )

    if (
        pilot_dir
        / "final_result.json"
    ).exists():
        raise PilotError(
            "final_result.json "
            "already exists; "
            "review before any rerun"
        )

    manifest = load_json(
        pilot_dir
        / "manifest.json",
        "pilot manifest",
    )

    visible = load_json(
        pilot_dir
        / "visible_context.json",
        "visible context",
    )

    prompt = (
        pilot_dir
        / "prompt.txt"
    ).read_text(
        encoding="utf-8",
    )

    actual_context_digest = canonical_digest(
        visible
    )

    recorded_context_digest = manifest.get(
        "visible_context_digest_sha256"
    )

    if (
        actual_context_digest
        != recorded_context_digest
    ):
        raise PilotError(
            "visible_context.json digest mismatch"
        )

    campaign_manifest, _ = (
        load_campaign(
            campaign_root
        )
    )

    record = (
        resolve_fault_record(
            campaign_manifest,
            fault_id,
        )
    )

    (
        paths,
        baseline_status,
        _,
    ) = validate_completed_fault(
        record
    )

    fault_spec = load_json(
        paths[
            "fault_json"
        ],
        "fault spec",
    )

    source_stage5 = (
        manifest.get(
            "source_stage5"
        )
    )

    if not isinstance(
        source_stage5,
        dict,
    ):
        raise PilotError(
            "pilot manifest has "
            "no Stage-5 provenance"
        )

    if (
        source_stage5.get(
            "fault_json_sha256"
        )
        != sha256_file(
            paths[
                "fault_json"
            ]
        )
    ):
        raise PilotError(
            "Stage-5 fault JSON "
            "changed after pilot preparation"
        )

    if (
        source_stage5.get(
            "baseline_native_status"
        )
        != baseline_status.get(
            "native_status"
        )
    ):
        raise PilotError(
            "Stage-5 baseline "
            "status changed after "
            "pilot preparation"
        )

    golden_meta = (
        manifest.get(
            "golden_netlist"
        )
    )

    if not isinstance(
        golden_meta,
        dict,
    ):
        raise PilotError(
            "pilot manifest "
            "has no golden "
            "netlist provenance"
        )

    golden_netlist = Path(
        str(
            golden_meta.get(
                "path",
                "",
            )
        )
    ).resolve()

    if not golden_netlist.is_file():
        raise PilotError(
            "golden netlist "
            f"not found: {golden_netlist}"
        )

    if (
        sha256_file(
            golden_netlist
        )
        != golden_meta.get(
            "sha256"
        )
    ):
        raise PilotError(
            "golden netlist "
            "changed after pilot preparation"
        )

    model_policy = (
        policies[
            "model"
        ]
    )

    (
        response_text,
        response_record,
        request_record,
    ) = request_openai(
        model_policy=
            model_policy,

        prompt=
            prompt,

        credential_file=
            credential_file
            .expanduser()
            .resolve(),
    )

    write_json(
        pilot_dir
        / "round0_request.json",
        request_record,
    )

    write_json(
        pilot_dir
        / "round0_response.json",
        response_record,
    )

    write_text(
        pilot_dir
        / "round0_response.txt",
        response_text
        + "\n",
    )

    property_body = (
        extract_property(
            response_text
        )
    )

    write_text(
        pilot_dir
        / "round0_property.sva",
        property_body
        + "\n",
    )

    golden_trace = (
        pilot_dir
        / "round0_golden.trace.tsv"
    )

    faulty_trace = (
        pilot_dir
        / "round0_faulty.trace.tsv"
    )

    golden_monitor = (
        pilot_dir
        / "round0_golden_monitor.sv"
    )

    faulty_monitor = (
        pilot_dir
        / "round0_faulty_monitor.sv"
    )

    write_text(
        golden_monitor,

        build_monitor(
            root=root,

            fault_spec=
                fault_spec,

            property_body=
                property_body,

            trace_path=
                golden_trace,

            run_label=
                "golden",
        ),
    )

    write_text(
        faulty_monitor,

        build_monitor(
            root=root,

            fault_spec=
                fault_spec,

            property_body=
                property_body,

            trace_path=
                faulty_trace,

            run_label=
                "faulty",
        ),
    )

    scratch_root = (
        root
        / "runs"
        / "stage6"
        / (
            f".scratch_{fault_id}_"
            + datetime.now().strftime(
                "%Y%m%d_%H%M%S"
            )
        )
    )

    if scratch_root.exists():
        raise PilotError(
            "scratch root "
            "already exists: "
            f"{scratch_root}"
        )

    scratch_root.mkdir(
        parents=True,
        exist_ok=False,
    )

    golden_run_dir = (
        scratch_root
        / "golden_run"
    )

    faulty_run_dir = (
        scratch_root
        / "faulty_run"
    )

    infrastructure_error: (
        str
        | None
    ) = None

    scratch_retained = False

    golden_wrapper_rc: (
        int
        | None
    ) = None

    faulty_wrapper_rc: (
        int
        | None
    ) = None

    golden_result: (
        dict[str, Any]
        | None
    ) = None

    faulty_result: (
        dict[str, Any]
        | None
    ) = None

    try:
        (
            golden_wrapper_rc,
            golden_result,
        ) = run_stage5_wrapper(
            root=root,

            kind="golden",

            fault_json=
                paths[
                    "fault_json"
                ],

            golden_netlist=
                golden_netlist,

            monitor=
                golden_monitor,

            trace=
                golden_trace,

            run_dir=
                golden_run_dir,

            maxcycles=
                maxcycles,
        )

        copy_run_artifacts(
            golden_run_dir,
            pilot_dir,
            "round0_golden",
        )

        golden_status = str(
            golden_result.get(
                "status"
            )
        )

        if not (
            stage5_status_is_golden_valid(
                golden_status
            )
        ):
            raise PilotError(
                "golden execution "
                "is not scientifically "
                "valid: "
                f"{golden_status}"
            )

        (
            faulty_wrapper_rc,
            faulty_result,
        ) = run_stage5_wrapper(
            root=root,

            kind="faulty",

            fault_json=
                paths[
                    "fault_json"
                ],

            golden_netlist=
                golden_netlist,

            monitor=
                faulty_monitor,

            trace=
                faulty_trace,

            run_dir=
                faulty_run_dir,

            maxcycles=
                maxcycles,
        )

        copy_run_artifacts(
            faulty_run_dir,
            pilot_dir,
            "round0_faulty",
        )

        expected_faulty_status = str(
            baseline_status.get(
                "native_status"
            )
        )

        actual_faulty_status = str(
            faulty_result.get(
                "status"
            )
        )

        if (
            actual_faulty_status
            != expected_faulty_status
        ):
            raise PilotError(
                "Stage-6 faulty execution "
                "changed the Stage-5 "
                "natural outcome: "
                f"expected="
                f"{expected_faulty_status}, "
                f"actual="
                f"{actual_faulty_status}"
            )

    except Exception as exc:

        infrastructure_error = (
            str(exc)
        )

        scratch_retained = True

    finally:

        if (
            infrastructure_error
            is None
        ):
            shutil.rmtree(
                scratch_root,
                ignore_errors=True,
            )

        else:
            print(
                "Scratch retained "
                "for debugging: "
                f"{scratch_root}",
                file=sys.stderr,
            )

    if (
        infrastructure_error
        is not None
    ):
        failure = {
            "schema_version":
                SCHEMA_VERSION,

            "program_version":
                PROGRAM_VERSION,

            "stage":
                STAGE_NAME,

            "fault_id":
                fault_id,

            "completed_at_utc":
                utc_now(),

            "generation": {
                "model":
                    model_policy.get(
                        "model"
                    ),

                "reasoning_effort":
                    model_policy.get(
                        "reasoning_effort"
                    ),

                "visibility_profile":
                    visible.get(
                        "visibility_profile"
                    ),
            },

            "execution": {
                "golden_wrapper_returncode":
                    golden_wrapper_rc,

                "faulty_wrapper_returncode":
                    faulty_wrapper_rc,

                "scratch_retained":
                    scratch_retained,

                "scratch_root":
                    str(
                        scratch_root
                    ),
            },

            "verdict": {
                "engineering_closure":
                    "FAIL",

                "candidate_verdict":
                    "NOT_EVALUATED",

                "accepted":
                    False,

                "reason":
                    infrastructure_error,
            },
        }

        write_json(
            pilot_dir
            / "final_result.json",
            failure,
        )

        write_text(
            pilot_dir
            / "summary.txt",

            "Fault2Assertion Stage-6 "
            "Real-Fault Round-0 Pilot\n"
            + "=" * 72
            + "\n"
            + f"Fault ID            : "
            f"{fault_id}\n"
            + "Engineering closure : "
            "FAIL\n"
            + f"Reason              : "
            f"{infrastructure_error}\n"
            + f"Scratch retained    : "
            f"{scratch_root}\n",
        )

        print(
            f"ERROR: "
            f"{infrastructure_error}",
            file=sys.stderr,
        )

        return 2

    assert (
        golden_result
        is not None
    )

    assert (
        faulty_result
        is not None
    )

    golden_log = (
        pilot_dir
        / "round0_golden_xrun.log"
    )

    faulty_log = (
        pilot_dir
        / "round0_faulty_xrun.log"
    )

    golden_events = (
        parse_generated_events(
            golden_log,
            fault_id,
        )
    )

    faulty_events = (
        parse_generated_events(
            faulty_log,
            fault_id,
        )
    )

    if golden_events:

        candidate_verdict = (
            "GOLDEN_FALSE_POSITIVE"
        )

    elif faulty_events:

        candidate_verdict = (
            "TARGET_FAULT_DETECTED"
        )

    else:

        candidate_verdict = (
            "TARGET_NOT_DETECTED"
        )

    usage = (
        response_record.get(
            "usage"
        )
        if isinstance(
            response_record,
            dict,
        )
        else None
    )

    final: dict[
        str,
        Any,
    ] = {
        "schema_version":
            SCHEMA_VERSION,

        "program_version":
            PROGRAM_VERSION,

        "stage":
            STAGE_NAME,

        "fault_id":
            fault_id,

        "completed_at_utc":
            utc_now(),

        "generation": {
            "round":
                0,

            "feedback_used":
                False,

            "model":
                model_policy.get(
                    "model"
                ),

            "reasoning_effort":
                model_policy.get(
                    "reasoning_effort"
                ),

            "visibility_profile":
                visible.get(
                    "visibility_profile"
                ),

            "visible_context_digest_sha256":
                manifest.get(
                    "visible_context_digest_sha256"
                ),

            "property_sha256":
                hashlib.sha256(
                    property_body.encode(
                        "utf-8"
                    )
                ).hexdigest(),

            "api_usage":
                usage,
        },

        "execution": {
            "golden": {
                "wrapper_returncode":
                    golden_wrapper_rc,

                "stage5_status":
                    golden_result.get(
                        "status"
                    ),

                "assertion_event_count":
                    len(
                        golden_events
                    ),

                "first_assertion_event":
                    (
                        golden_events[0]
                        if golden_events
                        else None
                    ),
            },

            "faulty": {
                "wrapper_returncode":
                    faulty_wrapper_rc,

                "stage5_status":
                    faulty_result.get(
                        "status"
                    ),

                "expected_stage5_status":
                    baseline_status.get(
                        "native_status"
                    ),

                "assertion_event_count":
                    len(
                        faulty_events
                    ),

                "first_assertion_event":
                    (
                        faulty_events[0]
                        if faulty_events
                        else None
                    ),
            },

            "scratch_retained":
                False,
        },

        "verdict": {
            "engineering_closure":
                "PASS",

            "candidate_verdict":
                candidate_verdict,

            "accepted":
                (
                    candidate_verdict
                    == "TARGET_FAULT_DETECTED"
                ),

            "acceptance_contract": {
                "golden_assertion_event_count":
                    0,

                "faulty_assertion_event_count_min":
                    1,

                "faulty_natural_outcome_must_match_stage5_baseline":
                    True,
            },
        },

        "artifacts": {
            "manifest":
                "manifest.json",

            "visible_context":
                "visible_context.json",

            "prompt":
                "prompt.txt",

            "request":
                "round0_request.json",

            "response":
                "round0_response.json",

            "response_text":
                "round0_response.txt",

            "property":
                "round0_property.sva",

            "golden_result":
                "round0_golden_result.json",

            "faulty_result":
                "round0_faulty_result.json",

            "golden_log":
                "round0_golden_xrun.log",

            "faulty_log":
                "round0_faulty_xrun.log",
        },
    }

    final[
        "result_digest_sha256"
    ] = canonical_digest(
        {
            key: value
            for key, value
            in final.items()
            if key
            not in {
                "completed_at_utc",
                "result_digest_sha256",
            }
        }
    )

    write_json(
        pilot_dir
        / "final_result.json",
        final,
    )

    write_text(
        pilot_dir
        / "summary.txt",
        render_summary(
            final
        ),
    )

    print()

    print(
        render_summary(
            final
        ),
        end="",
    )

    return 0


def build_parser() -> (
    argparse.ArgumentParser
):

    root = repo_root()

    parser = (
        argparse.ArgumentParser(
            description=__doc__,
        )
    )

    parser.add_argument(
        "--campaign-root",
        type=Path,

        default=(
            root
            / "runs"
            / "stage5_campaign_v3"
            / "cv32e40p"
            / "crc32"
            / "sites_all"
        ),

        help=(
            "Completed Stage-5 "
            "campaign root."
        ),
    )

    sub = (
        parser.add_subparsers(
            dest="command",
            required=True,
        )
    )

    p_select = (
        sub.add_parser(
            "select",
            help=(
                "List/select clean "
                "OUTPUT_MISMATCH "
                "pilot faults"
            ),
        )
    )

    p_select.add_argument(
        "--max-receivers",
        type=int,
        default=8,
    )

    p_select.add_argument(
        "--limit",
        type=int,
        default=10,
    )

    p_select.add_argument(
        "--print-id",
        action="store_true",
    )

    p_prepare = (
        sub.add_parser(
            "prepare",
            help=(
                "Materialize pilot "
                "manifest/context/"
                "prompt only"
            ),
        )
    )

    p_prepare.add_argument(
        "--fault-id",
        required=True,
    )

    p_prepare.add_argument(
        "--profile",
        default=DEFAULT_PROFILE,
    )

    p_generate = (
        sub.add_parser(
            "generate",
            help=(
                "Call OpenAI once and materialize the Round-0 "
                "property without running Xcelium"
            ),
        )
    )

    p_generate.add_argument(
        "--fault-id",
        required=True,
    )

    p_generate.add_argument(
        "--credential-file",
        type=Path,
        default=(
            Path.home()
            / ".config"
            / "fault2assertion"
            / "openai.env"
        ),
    )

    p_execute = (
        sub.add_parser(
            "execute",
            help=(
                "Call OpenAI and "
                "run golden/faulty "
                "Xcelium"
            ),
        )
    )

    p_execute.add_argument(
        "--fault-id",
        required=True,
    )

    p_execute.add_argument(
        "--credential-file",
        type=Path,

        default=(
            Path.home()
            / ".config"
            / "fault2assertion"
            / "openai.env"
        ),
    )

    p_execute.add_argument(
        "--maxcycles",
        type=int,
        default=DEFAULT_MAXCYCLES,
    )

    return parser


def main(
    argv: Sequence[str]
    | None = None,
) -> int:

    args = (
        build_parser()
        .parse_args(
            argv
        )
    )

    root = repo_root()

    try:
        if (
            args.command
            == "select"
        ):
            if (
                args.max_receivers
                <= 0
            ):
                raise PilotError(
                    "--max-receivers "
                    "must be positive"
                )

            if args.limit <= 0:
                raise PilotError(
                    "--limit "
                    "must be positive"
                )

            rows = select_candidates(
                args.campaign_root,

                max_receivers=
                    args.max_receivers,
            )

            if not rows:
                raise PilotError(
                    "no eligible completed "
                    "OUTPUT_MISMATCH "
                    "pilot fault found"
                )

            if args.print_id:
                print(
                    rows[0][
                        "fault_id"
                    ]
                )

            else:
                print_candidates(
                    rows,
                    args.limit,
                )

                print(
                    "Recommended "
                    "first pilot: "
                    f"{rows[0]['fault_id']}"
                )

            return 0

        if (
            args.command
            == "prepare"
        ):
            prepare_pilot(
                root=root,

                campaign_root=
                    args.campaign_root,

                fault_id=
                    args.fault_id,

                profile_name=
                    args.profile,
            )

            return 0

        if (
            args.command
            == "generate"
        ):
            return generate_pilot(
                root=root,
                fault_id=args.fault_id,
                credential_file=args.credential_file,
            )

        if (
            args.command
            == "execute"
        ):
            if (
                args.maxcycles
                <= 0
            ):
                raise PilotError(
                    "--maxcycles "
                    "must be positive"
                )

            return execute_pilot(
                root=root,

                campaign_root=
                    args.campaign_root,

                fault_id=
                    args.fault_id,

                credential_file=
                    args.credential_file,

                maxcycles=
                    args.maxcycles,
            )

        raise PilotError(
            "unsupported command: "
            f"{args.command}"
        )

    except PilotError as exc:

        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )

        return 1


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
