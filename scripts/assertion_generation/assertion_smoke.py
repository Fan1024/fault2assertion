#!/usr/bin/env python3
"""Generate, extract, and execute one candidate SVA with Xcelium.

This smoke test intentionally does not judge whether the generated
assertion is semantically correct.

Responsibilities:

- OpenAI:
  Generate one marker-delimited property-expression body.

- Python:
  Save the response, extract the marker block, generate a minimal
  SystemVerilog harness, launch Xcelium, and record raw execution facts.

- Xcelium:
  Decide whether the generated SystemVerilog/SVA compiles, elaborates,
  and runs.

- Result:
  Record whether simulation started, whether it reached normal completion,
  and how many assertion events were observed.

The assertion failure action is non-terminating and log-only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BEGIN_MARKER = "BEGIN_SVA"
END_MARKER = "END_SVA"

DEFAULT_MODEL = "gpt-4.1-nano"
DEFAULT_MAX_OUTPUT_TOKENS = 256
DEFAULT_XRUN_TIMEOUT_SECONDS = 180

INSTRUCTIONS = """Generate one candidate SystemVerilog Assertion property expression body.

Return exactly this format:

BEGIN_SVA
<property expression body>
END_SVA

Output rules:
- Return exactly one BEGIN_SVA/END_SVA block.
- Do not use Markdown code fences.
- Do not add explanations outside the markers.
- Do not include a trailing semicolon.
- Do not include assert property.
- Do not include property/endproperty.
- Do not include module/endmodule.
- Do not include a clock event.
- Do not include disable iff.
- Use only the signals req and ack.

The surrounding clock, reset, assertion statement, action block, and
testbench will be added by the experiment infrastructure.
"""

REQUIREMENT = (
    "Generate a candidate assertion for this requirement: "
    "when req is observed high, ack should be observed high on the "
    "following clock cycle."
)


def utc_now() -> str:
    """Return the current UTC time in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def write_json(
    path: Path,
    payload: Any,
) -> None:
    """Write a readable JSON artifact through a temporary file."""
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

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


def safe_component(
    value: str,
) -> str:
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
    source_mode: str,
) -> Path:
    """Create one unique timestamped run directory."""
    output_root.mkdir(
        parents=True,
        exist_ok=True,
    )

    timestamp = datetime.now(
        timezone.utc
    ).strftime("%Y%m%dT%H%M%SZ")

    if source_mode == "api":
        suffix = safe_component(model)
    else:
        suffix = "offline_response"

    base_name = (
        f"{timestamp}_{suffix}"
    )

    candidate = output_root / base_name
    index = 1

    while candidate.exists():
        candidate = output_root / (
            f"{base_name}_{index:02d}"
        )
        index += 1

    candidate.mkdir(
        parents=False,
        exist_ok=False,
    )

    return candidate


def extract_marker_block(
    response_text: str,
) -> tuple[str | None, dict[str, Any]]:
    """Extract exactly one marker-delimited block.

    This function does not parse SVA grammar and does not judge assertion
    semantics. Xcelium remains responsible for language validation.
    """
    normalized = (
        response_text
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )

    begin_count = normalized.count(
        BEGIN_MARKER
    )

    end_count = normalized.count(
        END_MARKER
    )

    pattern = re.compile(
        rf"\A\s*{BEGIN_MARKER}[ \t]*\n"
        rf"(.*?)"
        rf"\n{END_MARKER}\s*\Z",
        re.DOTALL,
    )

    match = pattern.fullmatch(
        normalized
    )

    errors: list[str] = []

    if begin_count != 1:
        errors.append(
            "response must contain exactly one "
            "BEGIN_SVA marker"
        )

    if end_count != 1:
        errors.append(
            "response must contain exactly one "
            "END_SVA marker"
        )

    if match is None:
        errors.append(
            "response does not match the required "
            "marker-delimited output format"
        )

        return None, {
            "status": "FAIL",
            "begin_marker_count": begin_count,
            "end_marker_count": end_count,
            "property_length": 0,
            "errors": errors,
            "syntax_checked_by_python": False,
            "semantics_checked": False,
        }

    body = match.group(1).strip()

    if not body:
        errors.append(
            "extracted property body is empty"
        )

    if len(body) > 4096:
        errors.append(
            "extracted property body exceeds "
            "the 4096-character transport limit"
        )

    if errors:
        return None, {
            "status": "FAIL",
            "begin_marker_count": begin_count,
            "end_marker_count": end_count,
            "property_length": len(body),
            "errors": errors,
            "syntax_checked_by_python": False,
            "semantics_checked": False,
        }

    return body, {
        "status": "PASS",
        "begin_marker_count": begin_count,
        "end_marker_count": end_count,
        "property_length": len(body),
        "errors": [],
        "syntax_checked_by_python": False,
        "semantics_checked": False,
    }


def indent_text(
    text: str,
    spaces: int,
) -> str:
    """Indent every line of generated text."""
    prefix = " " * spaces

    return "\n".join(
        prefix + line
        for line in text.splitlines()
    )


def build_testbench(
    property_body: str,
) -> str:
    """Create one self-contained synthetic SystemVerilog testbench."""
    indented_property = indent_text(
        property_body,
        12,
    )

    return f"""`timescale 1ns/1ps

module f2a_assertion_execution_smoke_tb;

    logic clk = 1'b0;
    logic rst_n = 1'b0;
    logic req = 1'b0;
    logic ack = 1'b0;

    integer assertion_events = 0;

    always #5 clk = ~clk;

    /*
     * The generated candidate is inserted only as the property body.
     *
     * The failure action is deliberately non-terminating:
     *
     * - no $fatal;
     * - no $error;
     * - no $stop.
     *
     * Therefore an assertion failure is recorded but does not
     * intentionally terminate this smoke simulation.
     */
    a_ai_generated_candidate: assert property (
        @(posedge clk)
        disable iff (!rst_n)
        (
{indented_property}
        )
    )
    else begin
        assertion_events =
            assertion_events + 1;

        $display(
            "F2A_ASSERTION_EVENT index=%0d time=%0t",
            assertion_events,
            $time
        );
    end

    initial begin
        $display(
            "F2A_SIMULATION_STARTED time=%0t",
            $time
        );

        /*
         * Keep reset active for two clock cycles.
         */
        repeat (2) @(negedge clk);

        rst_n = 1'b1;

        /*
         * Provide a small amount of deterministic signal activity.
         *
         * This stimulus is not a semantic oracle. The current stage
         * does not classify the generated assertion as correct or
         * incorrect based on whether it triggers.
         */
        @(negedge clk);
        req = 1'b0;
        ack = 1'b0;

        @(negedge clk);
        req = 1'b1;
        ack = 1'b0;

        @(negedge clk);
        req = 1'b0;
        ack = 1'b1;

        @(negedge clk);
        req = 1'b1;
        ack = 1'b1;

        @(negedge clk);
        req = 1'b0;
        ack = 1'b0;

        @(negedge clk);
        req = 1'b1;
        ack = 1'b0;

        @(negedge clk);
        req = 1'b0;
        ack = 1'b0;

        /*
         * Give pending assertion threads several additional sampled
         * clock edges before ending the smoke test.
         */
        repeat (4) @(posedge clk);

        #1;

        $display(
            "F2A_ASSERTION_EVENTS=%0d",
            assertion_events
        );

        $display(
            "F2A_SIMULATION_COMPLETED time=%0t",
            $time
        );

        $finish;
    end

endmodule
"""


def sanitized_xrun_environment() -> dict[str, str]:
    """Remove OpenAI credentials before launching Xcelium."""
    environment = os.environ.copy()

    for variable_name in list(
        environment
    ):
        if (
            variable_name == "F2A_OPENAI_ENV"
            or variable_name.startswith(
                "OPENAI_"
            )
        ):
            environment.pop(
                variable_name,
                None,
            )

    return environment


def cleanup_xrun_transients(
    run_directory: Path,
) -> list[str]:
    """Remove transient Xcelium work files after normal completion."""
    removed: list[str] = []

    transient_names = (
        "xcelium.d",
        "INCA_libs",
        "waves.shm",
        "xrun.key",
        "xrun.history",
    )

    for name in transient_names:
        path = run_directory / name

        if path.is_dir():
            shutil.rmtree(path)
            removed.append(name)

        elif path.exists():
            path.unlink()
            removed.append(name)

    return removed


def extract_diagnostic_lines(
    log_text: str,
) -> list[str]:
    """Extract a compact set of basic tool diagnostic lines."""
    diagnostic_lines: list[str] = []

    diagnostic_patterns = (
        "*E,",
        "*F,",
        "ERROR:",
        "FATAL:",
    )

    for line in log_text.splitlines():
        if any(
            pattern in line
            for pattern in diagnostic_patterns
        ):
            diagnostic_lines.append(
                line
            )

        if len(diagnostic_lines) >= 100:
            break

    return diagnostic_lines


def classify_execution(
    *,
    timed_out: bool,
    simulation_started: bool,
    simulation_completed: bool,
) -> str:
    """Classify execution progress without judging assertion correctness."""
    if timed_out:
        return "XRUN_TIMEOUT"

    if simulation_completed:
        return "SIMULATION_COMPLETED"

    if simulation_started:
        return "SIMULATION_STARTED_NOT_COMPLETED"

    return "COMPILE_OR_ELABORATION_NOT_COMPLETED"


def classify_termination_observation(
    *,
    simulation_started: bool,
    simulation_completed: bool,
    assertion_event_count: int | None,
) -> str:
    """Record only observable correlation, not causal semantics."""
    if not simulation_started:
        return "NOT_APPLICABLE_SIMULATION_NOT_STARTED"

    if simulation_completed:
        if (
            assertion_event_count is not None
            and assertion_event_count > 0
        ):
            return (
                "SIMULATION_COMPLETED_AFTER_ASSERTION_EVENTS"
            )

        return (
            "SIMULATION_COMPLETED_WITHOUT_RECORDED_ASSERTION_EVENT"
        )

    if (
        assertion_event_count is not None
        and assertion_event_count > 0
    ):
        return (
            "SIMULATION_INCOMPLETE_AFTER_ASSERTION_EVENT_"
            "CAUSE_NOT_INFERRED"
        )

    return (
        "SIMULATION_INCOMPLETE_WITHOUT_RECORDED_"
        "ASSERTION_EVENT"
    )


def read_existing_response(
    response_file: Path,
) -> tuple[str, dict[str, Any]]:
    """Read a previous response without making another API request."""
    resolved = (
        response_file
        .expanduser()
        .resolve()
    )

    if not resolved.is_file():
        raise FileNotFoundError(
            f"response file not found: {resolved}"
        )

    response_text = resolved.read_text(
        encoding="utf-8",
    )

    response_record = {
        "source": "existing_response_file",
        "source_path": str(resolved),
        "output_text": response_text,
    }

    return (
        response_text,
        response_record,
    )


def request_openai_response(
    *,
    model: str,
    max_output_tokens: int,
) -> tuple[str, dict[str, Any]]:
    """Request one fixed-format candidate from the OpenAI Responses API."""
    api_key = os.environ.get(
        "OPENAI_API_KEY",
        "",
    ).strip()

    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not configured"
        )

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "OpenAI Python SDK is not installed"
        ) from exc

    client = OpenAI(
        timeout=60.0,
        max_retries=2,
    )

    response = client.responses.create(
        model=model,
        instructions=INSTRUCTIONS,
        input=REQUIREMENT,
        max_output_tokens=max_output_tokens,
        store=False,
    )

    response_text = (
        response.output_text or ""
    ).strip()

    response_record = response.model_dump(
        mode="json",
    )

    return (
        response_text,
        response_record,
    )


def run_xcelium(
    *,
    xrun_binary: str,
    testbench_path: Path,
    run_directory: Path,
    timeout_seconds: int,
    keep_xrun_work: bool,
) -> dict[str, Any]:
    """Launch Xcelium once and record basic execution facts."""
    log_path = run_directory / "xrun.log"
    console_path = (
        run_directory / "console.txt"
    )

    command = [
        xrun_binary,
        "-64bit",
        "-sv",
        "-timescale",
        "1ns/1ps",
        "-top",
        "f2a_assertion_execution_smoke_tb",
        str(testbench_path),
        "-l",
        str(log_path),
    ]

    started_at = utc_now()

    timed_out = False
    returncode: int | None = None
    console_text = ""

    try:
        completed = subprocess.run(
            command,
            cwd=run_directory,
            env=sanitized_xrun_environment(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=timeout_seconds,
        )

        returncode = completed.returncode
        console_text = completed.stdout or ""

    except subprocess.TimeoutExpired as exc:
        timed_out = True

        if isinstance(exc.stdout, bytes):
            console_text = exc.stdout.decode(
                "utf-8",
                errors="replace",
            )

        elif isinstance(exc.stdout, str):
            console_text = exc.stdout

        else:
            console_text = ""

        console_text += (
            "\nF2A_XRUN_TIMEOUT="
            f"{timeout_seconds}\n"
        )

    console_path.write_text(
        console_text,
        encoding="utf-8",
    )

    if log_path.is_file():
        log_text = log_path.read_text(
            encoding="utf-8",
            errors="replace",
        )

        log_source = "xrun.log"

    else:
        log_text = console_text
        log_source = "console.txt"

    simulation_started = (
        "F2A_SIMULATION_STARTED"
        in log_text
    )

    simulation_completed = (
        "F2A_SIMULATION_COMPLETED"
        in log_text
    )

    event_count_matches = re.findall(
        r"F2A_ASSERTION_EVENTS=(\d+)",
        log_text,
    )

    if event_count_matches:
        assertion_event_count = int(
            event_count_matches[-1]
        )

    else:
        event_lines = re.findall(
            r"F2A_ASSERTION_EVENT\b",
            log_text,
        )

        if event_lines:
            assertion_event_count = len(
                event_lines
            )

        elif simulation_started:
            assertion_event_count = 0

        else:
            assertion_event_count = None

    diagnostic_lines = (
        extract_diagnostic_lines(
            log_text
        )
    )

    diagnostics_path = (
        run_directory / "diagnostics.txt"
    )

    diagnostics_path.write_text(
        (
            "\n".join(diagnostic_lines)
            + ("\n" if diagnostic_lines else "")
        ),
        encoding="utf-8",
    )

    execution_status = (
        classify_execution(
            timed_out=timed_out,
            simulation_started=(
                simulation_started
            ),
            simulation_completed=(
                simulation_completed
            ),
        )
    )

    if simulation_started:
        compile_elaboration_status = "PASS"
    else:
        compile_elaboration_status = (
            "FAILED_OR_NOT_REACHED"
        )

    termination_observation = (
        classify_termination_observation(
            simulation_started=(
                simulation_started
            ),
            simulation_completed=(
                simulation_completed
            ),
            assertion_event_count=(
                assertion_event_count
            ),
        )
    )

    removed_transients: list[str] = []

    if (
        simulation_completed
        and not keep_xrun_work
    ):
        removed_transients = (
            cleanup_xrun_transients(
                run_directory
            )
        )

    return {
        "xrun_invoked": True,
        "xrun_command": command,
        "xrun_started_at_utc": (
            started_at
        ),
        "xrun_completed_at_utc": (
            utc_now()
        ),
        "xrun_returncode": returncode,
        "xrun_timed_out": timed_out,
        "xrun_timeout_seconds": (
            timeout_seconds
        ),
        "log_source": log_source,
        "xrun_log_exists": (
            log_path.is_file()
        ),
        "console_log_exists": (
            console_path.is_file()
        ),
        "compile_elaboration_status": (
            compile_elaboration_status
        ),
        "execution_status": (
            execution_status
        ),
        "simulation_started": (
            simulation_started
        ),
        "simulation_completed": (
            simulation_completed
        ),
        "assertion_action_policy": (
            "NON_TERMINATING_LOG_ONLY"
        ),
        "assertion_event_count": (
            assertion_event_count
        ),
        "assertion_triggered": (
            (
                assertion_event_count > 0
            )
            if assertion_event_count
            is not None
            else None
        ),
        "termination_observation": (
            termination_observation
        ),
        "semantic_verdict": (
            "NOT_EVALUATED"
        ),
        "diagnostic_line_count": len(
            diagnostic_lines
        ),
        "removed_transients": (
            removed_transients
        ),
    }


def parse_arguments() -> argparse.Namespace:
    """Parse the smoke-test interface."""
    repository_root = (
        Path(__file__)
        .resolve()
        .parents[2]
    )

    parser = argparse.ArgumentParser(
        description=(
            "Generate or reuse one assertion response "
            "and execute it with Xcelium."
        )
    )

    parser.add_argument(
        "--model",
        default=os.environ.get(
            "OPENAI_MODEL",
            DEFAULT_MODEL,
        ),
        help=(
            "Model used when an API request is made."
        ),
    )

    parser.add_argument(
        "--response-file",
        type=Path,
        help=(
            "Reuse an existing response.txt and "
            "skip the API request."
        ),
    )

    parser.add_argument(
        "--xrun-bin",
        default=os.environ.get(
            "XRUN_BIN",
            "xrun",
        ),
    )

    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=(
            DEFAULT_MAX_OUTPUT_TOKENS
        ),
    )

    parser.add_argument(
        "--xrun-timeout-seconds",
        type=int,
        default=(
            DEFAULT_XRUN_TIMEOUT_SECONDS
        ),
    )

    parser.add_argument(
        "--keep-xrun-work",
        action="store_true",
    )

    parser.add_argument(
        "--output-root",
        type=Path,
        default=(
            repository_root
            / "runs"
            / "stage6"
            / "smoke"
            / "xcelium"
        ),
    )

    return parser.parse_args()


def main() -> int:
    """Run the complete engineering smoke pipeline."""
    args = parse_arguments()

    if not args.model.strip():
        print(
            "ERROR: model must not be empty",
            file=sys.stderr,
        )
        return 2

    if args.max_output_tokens <= 0:
        print(
            "ERROR: max-output-tokens must be positive",
            file=sys.stderr,
        )
        return 2

    if args.xrun_timeout_seconds <= 0:
        print(
            "ERROR: xrun-timeout-seconds must be positive",
            file=sys.stderr,
        )
        return 2

    source_mode = (
        "response_file"
        if args.response_file
        else "api"
    )

    run_directory = (
        create_run_directory(
            output_root=(
                args.output_root
                .expanduser()
                .resolve()
            ),
            model=args.model,
            source_mode=source_mode,
        )
    )

    result: dict[str, Any] = {
        "schema_version": "1.0",
        "stage": (
            "stage_06_assertion_generation"
        ),
        "experiment": (
            "assertion_xcelium_smoke"
        ),
        "pipeline_status": "INCOMPLETE",
        "failure_stage": None,
        "response_source": source_mode,
        "model_requested": args.model,
        "started_at_utc": utc_now(),
        "run_directory": str(
            run_directory
        ),
        "syntax_checked_by_python": False,
        "semantics_checked": False,
        "semantic_verdict": (
            "NOT_EVALUATED"
        ),
    }

    request_record = {
        "source": source_mode,
        "model": args.model,
        "instructions": (
            INSTRUCTIONS
            if source_mode == "api"
            else None
        ),
        "input": (
            REQUIREMENT
            if source_mode == "api"
            else None
        ),
        "max_output_tokens": (
            args.max_output_tokens
            if source_mode == "api"
            else None
        ),
        "store": (
            False
            if source_mode == "api"
            else None
        ),
        "response_file": (
            str(
                args.response_file
                .expanduser()
                .resolve()
            )
            if args.response_file
            else None
        ),
    }

    write_json(
        run_directory / "request.json",
        request_record,
    )

    try:
        if args.response_file:
            (
                response_text,
                response_record,
            ) = read_existing_response(
                args.response_file
            )

        else:
            (
                response_text,
                response_record,
            ) = request_openai_response(
                model=args.model,
                max_output_tokens=(
                    args.max_output_tokens
                ),
            )

        write_json(
            run_directory / "response.json",
            response_record,
        )

        (
            run_directory / "response.txt"
        ).write_text(
            response_text.rstrip() + "\n",
            encoding="utf-8",
        )

        result["response_status"] = "PASS"

        result["model_returned"] = (
            response_record.get("model")
        )

        result["response_id"] = (
            response_record.get("id")
        )

    except Exception as exc:
        result.update(
            {
                "pipeline_status": (
                    "INCOMPLETE"
                ),
                "failure_stage": (
                    "api_or_response_read"
                ),
                "response_status": "FAIL",
                "error_type": (
                    type(exc).__name__
                ),
                "error_message": str(exc),
                "completed_at_utc": (
                    utc_now()
                ),
            }
        )

        write_json(
            run_directory / "result.json",
            result,
        )

        print(
            "ERROR: response acquisition failed:",
            file=sys.stderr,
        )

        print(
            f"  {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )

        print(
            f"Run directory: {run_directory}",
            file=sys.stderr,
        )

        return 1

    (
        property_body,
        extraction_result,
    ) = extract_marker_block(
        response_text
    )

    write_json(
        run_directory
        / "extraction_result.json",
        extraction_result,
    )

    result[
        "extraction_status"
    ] = extraction_result["status"]

    if property_body is None:
        result.update(
            {
                "pipeline_status": (
                    "INCOMPLETE"
                ),
                "failure_stage": (
                    "response_format_extraction"
                ),
                "xrun_invoked": False,
                "completed_at_utc": (
                    utc_now()
                ),
            }
        )

        write_json(
            run_directory / "result.json",
            result,
        )

        print(
            "ERROR: response format extraction failed.",
            file=sys.stderr,
        )

        for error in extraction_result[
            "errors"
        ]:
            print(
                f"  - {error}",
                file=sys.stderr,
            )

        print(
            f"Run directory: {run_directory}",
            file=sys.stderr,
        )

        return 1

    extracted_path = (
        run_directory
        / "extracted_property.sva"
    )

    extracted_path.write_text(
        property_body + "\n",
        encoding="utf-8",
    )

    testbench_path = (
        run_directory / "smoke_tb.sv"
    )

    testbench_path.write_text(
        build_testbench(
            property_body
        ),
        encoding="utf-8",
    )

    xrun_binary = shutil.which(
        args.xrun_bin
    )

    if xrun_binary is None:
        result.update(
            {
                "pipeline_status": (
                    "INCOMPLETE"
                ),
                "failure_stage": (
                    "xcelium_setup"
                ),
                "xrun_invoked": False,
                "error_type": (
                    "XrunNotFound"
                ),
                "error_message": (
                    "Xcelium executable not found: "
                    f"{args.xrun_bin}"
                ),
                "completed_at_utc": (
                    utc_now()
                ),
            }
        )

        write_json(
            run_directory / "result.json",
            result,
        )

        print(
            "ERROR: Xcelium executable not found:",
            file=sys.stderr,
        )

        print(
            f"  {args.xrun_bin}",
            file=sys.stderr,
        )

        print(
            f"Run directory: {run_directory}",
            file=sys.stderr,
        )

        return 1

    try:
        xrun_result = run_xcelium(
            xrun_binary=xrun_binary,
            testbench_path=(
                testbench_path.resolve()
            ),
            run_directory=(
                run_directory
            ),
            timeout_seconds=(
                args.xrun_timeout_seconds
            ),
            keep_xrun_work=(
                args.keep_xrun_work
            ),
        )

    except Exception as exc:
        result.update(
            {
                "pipeline_status": (
                    "INCOMPLETE"
                ),
                "failure_stage": (
                    "xcelium_launch"
                ),
                "xrun_invoked": False,
                "error_type": (
                    type(exc).__name__
                ),
                "error_message": str(exc),
                "completed_at_utc": (
                    utc_now()
                ),
            }
        )

        write_json(
            run_directory / "result.json",
            result,
        )

        print(
            "ERROR: Xcelium could not be launched:",
            file=sys.stderr,
        )

        print(
            f"  {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )

        print(
            f"Run directory: {run_directory}",
            file=sys.stderr,
        )

        return 1

    result.update(
        xrun_result
    )

    result.update(
        {
            "pipeline_status": "COMPLETE",
            "failure_stage": None,
            "completed_at_utc": (
                utc_now()
            ),
        }
    )

    write_json(
        run_directory / "result.json",
        result,
    )

    print("=" * 72)
    print(
        "Fault2Assertion assertion/Xcelium "
        "engineering smoke: COMPLETE"
    )
    print("=" * 72)

    print(
        "Response source      : "
        f"{source_mode}"
    )

    print(
        "Model                : "
        f"{result.get('model_returned') or args.model}"
    )

    print(
        "Extracted property   : "
        f"{property_body}"
    )

    print(
        "Xcelium invoked      : "
        f"{result['xrun_invoked']}"
    )

    print(
        "Xcelium return code  : "
        f"{result['xrun_returncode']}"
    )

    print(
        "Execution status     : "
        f"{result['execution_status']}"
    )

    print(
        "Simulation started   : "
        f"{result['simulation_started']}"
    )

    print(
        "Simulation completed : "
        f"{result['simulation_completed']}"
    )

    print(
        "Assertion events     : "
        f"{result['assertion_event_count']}"
    )

    print(
        "Semantic verdict     : "
        "NOT_EVALUATED"
    )

    print(
        "Saved directory      : "
        f"{run_directory}"
    )

    # A nonzero Xcelium return code or compile failure is a recorded
    # candidate/tool result, not a Python infrastructure crash.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
