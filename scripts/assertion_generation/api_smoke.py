#!/usr/bin/env python3
"""Minimal OpenAI API connectivity smoke for Fault2Assertion Stage 6."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import openai
from openai import OpenAI


SCHEMA_VERSION = "1.0"
STAGE_NAME = "stage_06_assertion_generation"
EXPERIMENT_NAME = "api_smoke"

DEFAULT_MESSAGE = (
    "Hello from the Fault2Assertion Stage 6 API smoke test. "
    "Reply with one short sentence confirming that you received this message."
)

DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_MAX_OUTPUT_TOKENS = 128


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, payload: Any) -> None:
    """Write a JSON artifact through a temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(
            payload,
            indent=2,
            ensure_ascii=False,
            sort_keys=False,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def safe_component(value: str) -> str:
    """Convert a value into a safe directory-name component."""
    cleaned = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        value,
    ).strip("._-")

    return cleaned or "unknown"


def create_run_directory(
    output_root: Path,
    model: str,
) -> Path:
    """Create one unique timestamped run directory."""
    output_root.mkdir(
        parents=True,
        exist_ok=True,
    )

    timestamp = datetime.now(
        timezone.utc
    ).strftime("%Y%m%dT%H%M%SZ")

    base_name = (
        f"{timestamp}_{safe_component(model)}"
    )

    candidate = output_root / base_name
    counter = 1

    while candidate.exists():
        candidate = output_root / (
            f"{base_name}_{counter:02d}"
        )
        counter += 1

    candidate.mkdir(
        parents=False,
        exist_ok=False,
    )

    return candidate


def parse_arguments() -> argparse.Namespace:
    """Parse the intentionally small smoke-test interface."""
    repository_root = (
        Path(__file__)
        .resolve()
        .parents[2]
    )

    parser = argparse.ArgumentParser(
        description=(
            "Run one minimal OpenAI Responses API "
            "connectivity test."
        )
    )

    parser.add_argument(
        "--message",
        default=DEFAULT_MESSAGE,
        help="User text sent to the model.",
    )

    parser.add_argument(
        "--model",
        default=os.environ.get(
            "OPENAI_MODEL",
            DEFAULT_MODEL,
        ),
        help=(
            "OpenAI model ID. Defaults to "
            "OPENAI_MODEL or gpt-5.6-luna."
        ),
    )

    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=DEFAULT_MAX_OUTPUT_TOKENS,
        help="Maximum output tokens for the smoke response.",
    )

    parser.add_argument(
        "--output-root",
        type=Path,
        default=(
            repository_root
            / "runs"
            / "stage6"
            / "smoke"
            / "api"
        ),
        help=(
            "Root directory for timestamped API-smoke "
            "run directories."
        ),
    )

    return parser.parse_args()


def validate_arguments(
    args: argparse.Namespace,
) -> None:
    """Reject malformed local arguments before making an API call."""
    if not isinstance(args.message, str):
        raise ValueError(
            "--message must be a string"
        )

    if not args.message.strip():
        raise ValueError(
            "--message must not be empty"
        )

    if not isinstance(args.model, str):
        raise ValueError(
            "--model must be a string"
        )

    if not args.model.strip():
        raise ValueError(
            "--model must not be empty"
        )

    if args.max_output_tokens <= 0:
        raise ValueError(
            "--max-output-tokens must be positive"
        )


def api_key_is_validly_configured(
    api_key: str,
) -> bool:
    """Check only that a non-placeholder key has been supplied."""
    normalized = api_key.strip()

    if not normalized:
        return False

    if normalized == (
        "REPLACE_WITH_REAL_OPENAI_API_KEY"
    ):
        return False

    if normalized.startswith("PASTE_"):
        return False

    return True


def main() -> int:
    args = parse_arguments()

    try:
        validate_arguments(args)
    except ValueError as exc:
        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )
        return 2

    output_root = (
        args.output_root
        .expanduser()
        .resolve()
    )

    run_directory = create_run_directory(
        output_root=output_root,
        model=args.model,
    )

    started_at = utc_now()

    request_record = {
        "schema_version": SCHEMA_VERSION,
        "stage": STAGE_NAME,
        "experiment": EXPERIMENT_NAME,
        "model": args.model,
        "input": args.message,
        "max_output_tokens": (
            args.max_output_tokens
        ),
        "store": False,
        "started_at_utc": started_at,
    }

    write_json(
        run_directory / "request.json",
        request_record,
    )

    runtime_record = {
        "schema_version": SCHEMA_VERSION,
        "python_version": sys.version,
        "python_executable": sys.executable,
        "openai_python_version": getattr(
            openai,
            "__version__",
            "unknown",
        ),
        "platform": platform.platform(),
        "hostname": socket.gethostname(),
        "working_directory": str(
            Path.cwd().resolve()
        ),
    }

    write_json(
        run_directory / "runtime.json",
        runtime_record,
    )

    api_key = os.environ.get(
        "OPENAI_API_KEY",
        "",
    )

    if not api_key_is_validly_configured(
        api_key
    ):
        result_record = {
            "schema_version": SCHEMA_VERSION,
            "stage": STAGE_NAME,
            "experiment": EXPERIMENT_NAME,
            "status": "FAIL",
            "failure_stage": (
                "environment_validation"
            ),
            "error_type": "MissingAPIKey",
            "error_message": (
                "OPENAI_API_KEY is missing or still "
                "contains the placeholder value."
            ),
            "model_requested": args.model,
            "run_directory": str(
                run_directory
            ),
            "started_at_utc": started_at,
            "completed_at_utc": utc_now(),
        }

        write_json(
            run_directory / "result.json",
            result_record,
        )

        print(
            "ERROR: OPENAI_API_KEY is not "
            "configured with a real key.",
            file=sys.stderr,
        )
        print(
            f"Run directory: {run_directory}",
            file=sys.stderr,
        )

        return 3

    try:
        client = OpenAI(
            timeout=60.0,
            max_retries=2,
        )

        response = client.responses.create(
            model=args.model,
            input=args.message,
            max_output_tokens=(
                args.max_output_tokens
            ),
            store=False,
        )

        response_record = response.model_dump(
            mode="json"
        )

        write_json(
            run_directory / "response.json",
            response_record,
        )

        output_text = (
            response.output_text or ""
        ).strip()

        (
            run_directory / "response.txt"
        ).write_text(
            output_text
            + ("\n" if output_text else ""),
            encoding="utf-8",
        )

        passed = bool(output_text)

        result_record = {
            "schema_version": SCHEMA_VERSION,
            "stage": STAGE_NAME,
            "experiment": EXPERIMENT_NAME,
            "status": (
                "PASS"
                if passed
                else "FAIL"
            ),
            "failure_stage": (
                None
                if passed
                else "response_validation"
            ),
            "error_type": None,
            "error_message": (
                None
                if passed
                else (
                    "The API request completed but "
                    "response.output_text was empty."
                )
            ),
            "model_requested": args.model,
            "model_returned": (
                response_record.get("model")
            ),
            "response_id": (
                response_record.get("id")
            ),
            "response_status": (
                response_record.get("status")
            ),
            "nonempty_output_text": passed,
            "usage": response_record.get(
                "usage"
            ),
            "run_directory": str(
                run_directory
            ),
            "started_at_utc": started_at,
            "completed_at_utc": utc_now(),
        }

        write_json(
            run_directory / "result.json",
            result_record,
        )

        if not passed:
            print(
                "ERROR: The API call completed but "
                "returned no text output.",
                file=sys.stderr,
            )
            print(
                f"Run directory: {run_directory}",
                file=sys.stderr,
            )

            return 4

        print("=" * 72)
        print(
            "Fault2Assertion Stage 6 "
            "API smoke: PASS"
        )
        print("=" * 72)
        print(
            "Requested model : "
            f"{args.model}"
        )
        print(
            "Returned model  : "
            f"{result_record['model_returned']}"
        )
        print(
            "Response ID     : "
            f"{result_record['response_id']}"
        )
        print(
            "Response text   : "
            f"{output_text}"
        )
        print(
            "Saved directory : "
            f"{run_directory}"
        )

        return 0

    except Exception as exc:
        result_record = {
            "schema_version": SCHEMA_VERSION,
            "stage": STAGE_NAME,
            "experiment": EXPERIMENT_NAME,
            "status": "FAIL",
            "failure_stage": "api_request",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "model_requested": args.model,
            "run_directory": str(
                run_directory
            ),
            "started_at_utc": started_at,
            "completed_at_utc": utc_now(),
        }

        write_json(
            run_directory / "result.json",
            result_record,
        )

        print(
            "ERROR: OpenAI API smoke failed.",
            file=sys.stderr,
        )
        print(
            f"Error type: {type(exc).__name__}",
            file=sys.stderr,
        )
        print(
            f"Message   : {exc}",
            file=sys.stderr,
        )
        print(
            f"Saved to  : {run_directory}",
            file=sys.stderr,
        )

        return 1


if __name__ == "__main__":
    raise SystemExit(main())
