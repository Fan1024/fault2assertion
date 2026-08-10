#!/usr/bin/env python3
"""Generate exactly one Stage-6 Round-0 property from a frozen baseline prompt.

Requires `manifest.json`, `visible_context.json`, and `prompt.txt` prepared before
the API call. This script performs no simulation and no repair round.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

BEGIN_MARKER = "BEGIN_SVA"
END_MARKER = "END_SVA"


class GenerationError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


def load_json(
    path: Path,
    label: str,
) -> dict[str, Any]:

    try:
        value = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

    except FileNotFoundError as exc:
        raise GenerationError(
            f"{label} not found: {path}"
        ) from exc

    except json.JSONDecodeError as exc:
        raise GenerationError(
            f"invalid {label} JSON "
            f"{path}: {exc}"
        ) from exc

    if not isinstance(
        value,
        dict,
    ):
        raise GenerationError(
            f"{label} must contain "
            f"one JSON object: {path}"
        )

    return value


def write_json(
    path: Path,
    payload: Mapping[str, Any],
) -> None:

    temporary = path.with_name(
        f".{path.name}.tmp"
    )

    temporary.write_text(
        json.dumps(
            payload,
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    temporary.replace(path)


def write_text(
    path: Path,
    text: str,
) -> None:

    temporary = path.with_name(
        f".{path.name}.tmp"
    )

    temporary.write_text(
        text,
        encoding="utf-8",
    )

    temporary.replace(path)


def sha256_file(path: Path) -> str:
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


def parse_env_file(
    path: Path,
) -> dict[str, str]:

    if not path.is_file():
        raise GenerationError(
            f"credential file not found: {path}"
        )

    result: dict[str, str] = {}

    for line_number, raw in enumerate(
        path.read_text(
            encoding="utf-8"
        ).splitlines(),
        start=1,
    ):
        line = raw.strip()

        if (
            not line
            or line.startswith("#")
        ):
            continue

        if line.startswith(
            "export "
        ):
            line = line[7:].strip()

        if "=" not in line:
            raise GenerationError(
                "invalid credential line "
                f"{line_number} in {path}"
            )

        key, value = line.split(
            "=",
            1,
        )

        key = key.strip()
        value = value.strip()

        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {
                "'",
                '"',
            }
        ):
            value = value[1:-1]

        result[key] = value

    return result


def extract_property(
    response_text: str,
) -> str:

    if (
        response_text.count(
            BEGIN_MARKER
        )
        != 1
        or response_text.count(
            END_MARKER
        )
        != 1
    ):
        raise GenerationError(
            "OpenAI response must contain "
            "exactly one BEGIN_SVA and "
            "one END_SVA"
        )

    begin_marker = (
        response_text.index(
            BEGIN_MARKER
        )
    )

    begin = (
        begin_marker
        + len(BEGIN_MARKER)
    )

    end = response_text.index(
        END_MARKER,
        begin,
    )

    before = (
        response_text[
            :begin_marker
        ].strip()
    )

    after = (
        response_text[
            end + len(END_MARKER):
        ].strip()
    )

    if before or after:
        raise GenerationError(
            "OpenAI response contains "
            "text outside "
            "BEGIN_SVA/END_SVA"
        )

    body = response_text[
        begin:end
    ].strip()

    if not body:
        raise GenerationError(
            "generated property is empty"
        )

    if (
        "assert property"
        in body.lower()
    ):
        raise GenerationError(
            "model returned a complete "
            "assert property statement; "
            "only the body is allowed"
        )

    if body.endswith(";"):
        raise GenerationError(
            "generated property body "
            "must not end with a semicolon"
        )

    return body


def parse_args() -> argparse.Namespace:
    root_default = (
        Path(__file__)
        .resolve()
        .parents[2]
    )

    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--fault-id",
        required=True,
    )

    parser.add_argument(
        "--pilot-dir",
        type=Path,
        default=None,
    )

    parser.add_argument(
        "--credential-file",
        type=Path,
        default=(
            Path.home()
            / ".config"
            / "fault2assertion"
            / "openai.env"
        ),
    )

    parser.add_argument(
        "--model-policy",
        type=Path,
        default=(
            root_default
            / "assertion_generation"
            / "model_policy.json"
        ),
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    root = (
        Path(__file__)
        .resolve()
        .parents[2]
    )

    fault_id = (
        args.fault_id.strip()
    )

    pilot_dir = (
        args.pilot_dir
        .expanduser()
        .resolve()
        if args.pilot_dir is not None
        else (
            root
            / "runs"
            / "stage6"
            / f"pilot_{fault_id}"
        ).resolve()
    )

    if not pilot_dir.is_dir():
        raise GenerationError(
            "pilot directory not found: "
            f"{pilot_dir}"
        )

    manifest_path = (
        pilot_dir
        / "manifest.json"
    )

    prompt_path = (
        pilot_dir
        / "prompt.txt"
    )

    visible_context_path = (
        pilot_dir
        / "visible_context.json"
    )

    manifest = load_json(
        manifest_path,
        "Stage-6 baseline manifest",
    )

    if (
        manifest.get("stage")
        != "stage_06_baseline_ready"
    ):
        raise GenerationError(
            "baseline manifest stage mismatch: "
            f"{manifest.get('stage')!r}"
        )

    if (
        manifest.get("fault_id")
        != fault_id
    ):
        raise GenerationError(
            "baseline manifest "
            "fault_id mismatch"
        )

    if (
        manifest.get("baseline_frozen")
        is not True
    ):
        raise GenerationError(
            "baseline manifest is not frozen"
        )

    if (
        not prompt_path.is_file()
        or not visible_context_path.is_file()
    ):
        raise GenerationError(
            "baseline prompt/context "
            "is incomplete"
        )

    if (
        sha256_file(prompt_path)
        != manifest.get(
            "prompt",
            {},
        ).get(
            "sha256"
        )
    ):
        raise GenerationError(
            "frozen prompt SHA-256 mismatch"
        )

    outputs = {
        "request":
            pilot_dir
            / "round0_request.json",

        "response":
            pilot_dir
            / "round0_response.json",

        "response_text":
            pilot_dir
            / "round0_response.txt",

        "api_status":
            pilot_dir
            / "round0_api_status.json",

        "property":
            pilot_dir
            / "round0_property.sva",
    }

    existing = [
        path
        for path in outputs.values()
        if path.exists()
    ]

    if existing:
        raise GenerationError(
            "refusing to overwrite "
            "existing Round-0 "
            "generation artifacts:\n  "
            + "\n  ".join(
                str(path)
                for path in existing
            )
        )

    model_policy_path = (
        args.model_policy
        .expanduser()
        .resolve()
    )

    model_policy = load_json(
        model_policy_path,
        "model policy",
    )

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

    store = bool(
        model_policy.get(
            "store",
            False,
        )
    )

    if not model:
        raise GenerationError(
            "model_policy.json has no model"
        )

    if max_output_tokens <= 0:
        raise GenerationError(
            "max_output_tokens "
            "must be positive"
        )

    credentials = parse_env_file(
        args.credential_file
        .expanduser()
        .resolve()
    )

    api_key = credentials.get(
        "OPENAI_API_KEY",
        "",
    ).strip()

    if (
        not api_key
        or api_key
        == "REPLACE_WITH_REAL_OPENAI_API_KEY"
    ):
        raise GenerationError(
            "OPENAI_API_KEY is missing "
            "or still a placeholder"
        )

    try:
        from openai import OpenAI

    except ImportError as exc:
        raise GenerationError(
            "OpenAI Python SDK is "
            "not installed; install "
            "requirements-stage6.txt"
        ) from exc

    prompt = (
        prompt_path
        .read_text(
            encoding="utf-8"
        )
    )

    request_record = {
        "schema_version":
            "1.0",

        "stage":
            "stage_06_round0_generation",

        "fault_id":
            fault_id,

        "provider":
            "openai",

        "api":
            "responses",

        "model":
            model,

        "reasoning": {
            "effort":
                reasoning_effort,
        },

        "max_output_tokens":
            max_output_tokens,

        "store":
            store,

        "prompt_path":
            str(prompt_path),

        "prompt_sha256":
            sha256_file(
                prompt_path
            ),

        "model_policy_path":
            str(model_policy_path),

        "model_policy_sha256":
            sha256_file(
                model_policy_path
            ),

        "requested_at_utc":
            utc_now(),
    }

    client = OpenAI(
        api_key=api_key,
        timeout=300.0,
        max_retries=2,
    )

    response = client.responses.create(
        model=model,
        reasoning={
            "effort":
                reasoning_effort,
        },
        input=prompt,
        max_output_tokens=
            max_output_tokens,
        store=store,
    )

    response_record = (
        response.model_dump(
            mode="json"
        )
    )

    response_text = (
        response.output_text
        or ""
    ).strip()

    # Persist before validating output_text.
    write_json(
        outputs["request"],
        request_record,
    )

    write_json(
        outputs["response"],
        response_record,
    )

    write_text(
        outputs["response_text"],
        (
            response_text
            + (
                "\n"
                if response_text
                else ""
            )
        ),
    )

    api_status = {
        "schema_version":
            "1.0",

        "stage":
            "stage_06_round0_api_status",

        "fault_id":
            fault_id,

        "model_requested":
            model,

        "model_returned":
            response_record.get(
                "model"
            ),

        "response_id":
            response_record.get(
                "id"
            ),

        "response_status":
            response_record.get(
                "status"
            ),

        "incomplete_details":
            response_record.get(
                "incomplete_details"
            ),

        "usage":
            response_record.get(
                "usage"
            ),

        "nonempty_output_text":
            bool(response_text),

        "recorded_at_utc":
            utc_now(),
    }

    write_json(
        outputs["api_status"],
        api_status,
    )

    if not response_text:
        raise GenerationError(
            "OpenAI response contained "
            "no output_text; complete "
            "response was preserved"
        )

    property_body = extract_property(
        response_text
    )

    write_text(
        outputs["property"],
        property_body + "\n",
    )

    print("=" * 80)
    print(
        "Stage-6 Round-0 generation: PASS"
    )
    print("=" * 80)

    print(
        f"Fault ID       : {fault_id}"
    )

    print(
        f"Model          : {model}"
    )

    print(
        f"Reasoning      : {reasoning_effort}"
    )

    print(
        "Property       : "
        f"{outputs['property']}"
    )

    print("Round-0 property:")
    print("-" * 80)
    print(property_body)
    print("-" * 80)

    usage = response_record.get(
        "usage"
    )

    if usage is not None:
        print("API usage:")

        print(
            json.dumps(
                usage,
                indent=2,
                ensure_ascii=False,
            )
        )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(
            main()
        )

    except GenerationError as exc:
        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )

        raise SystemExit(2)
