#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(
    cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
    pwd
)"

F2A_ROOT="$(
    cd -- "${SCRIPT_DIR}/../.."
    pwd
)"

CONFIG_FILE="${F2A_OPENAI_ENV:-${HOME}/.config/fault2assertion/openai.env}"

PYTHON_BIN="${PYTHON_BIN:-$(command -v python || true)}"

NEEDS_API=1

for argument in "$@"; do
    case "${argument}" in
        --response-file|--response-file=*)
            NEEDS_API=0
            ;;
    esac
done

if [[ -z "${PYTHON_BIN}" ]]; then
    echo "ERROR: active Python executable was not found." >&2
    exit 2
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "ERROR: Python executable is not executable:" >&2
    echo "  ${PYTHON_BIN}" >&2
    exit 2
fi

#
# Preserve the active f2a Python before loading the Cadence environment.
# Load Cadence/Xcelium only inside this wrapper process.
#
set +u

source "${F2A_ROOT}/scripts/setup_env.sh"

set -u

if ! command -v xrun >/dev/null 2>&1; then
    echo "ERROR: xrun was not found after loading scripts/setup_env.sh." >&2
    exit 3
fi

#
# A private OpenAI configuration is required only for a new API request.
# Offline --response-file runs do not require an API key.
#
if [[ "${NEEDS_API}" -eq 1 ]]; then
    if [[ ! -r "${CONFIG_FILE}" ]]; then
        echo "ERROR: OpenAI configuration file is missing or unreadable:" >&2
        echo "  ${CONFIG_FILE}" >&2
        exit 4
    fi

    set -a

    source "${CONFIG_FILE}"

    set +a

    case "${OPENAI_API_KEY:-}" in
        ""|REPLACE_WITH_REAL_OPENAI_API_KEY|PASTE_REAL_API_KEY_HERE)
            echo "ERROR: OPENAI_API_KEY is missing or still a placeholder." >&2
            exit 5
            ;;
    esac

    if ! "${PYTHON_BIN}" \
        -c 'from openai import OpenAI' \
        >/dev/null 2>&1; then

        echo "ERROR: OpenAI Python SDK is unavailable in:" >&2
        echo "  ${PYTHON_BIN}" >&2
        echo >&2
        echo "Install it in the active f2a environment:" >&2
        echo "  python -m pip install -r requirements-stage6.txt" >&2

        exit 6
    fi
fi

export OPENAI_MODEL="${OPENAI_MODEL:-gpt-4.1-nano}"

if [[ "${NEEDS_API}" -eq 1 ]]; then
    API_MODE="enabled"
else
    API_MODE="skipped"
fi

umask 077

printf '%s\n' \
    "Fault2Assertion assertion/Xcelium engineering smoke" \
    "--------------------------------------------------" \
    "Repository : ${F2A_ROOT}" \
    "Python     : ${PYTHON_BIN}" \
    "Xcelium    : $(command -v xrun)" \
    "Model      : ${OPENAI_MODEL}" \
    "API mode   : ${API_MODE}" \
    ""

exec "${PYTHON_BIN}" \
    "${F2A_ROOT}/scripts/assertion_generation/assertion_smoke.py" \
    --xrun-bin "$(command -v xrun)" \
    "$@"
