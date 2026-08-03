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

PYTHON_BIN="${PYTHON_BIN:-python}"

if [[ ! -f "${CONFIG_FILE}" ]]; then
    echo "ERROR: OpenAI configuration file not found:" >&2
    echo "  ${CONFIG_FILE}" >&2
    echo >&2
    echo "Create it from the tracked template:" >&2
    echo "  mkdir -p ~/.config/fault2assertion" >&2
    echo "  cp ${F2A_ROOT}/.env.stage6.example \\" >&2
    echo "     ~/.config/fault2assertion/openai.env" >&2
    exit 2
fi

if [[ ! -r "${CONFIG_FILE}" ]]; then
    echo "ERROR: OpenAI configuration file is not readable:" >&2
    echo "  ${CONFIG_FILE}" >&2
    exit 2
fi

# Export variables from the private configuration only inside this
# wrapper process and its Python child process.
set -a

# shellcheck disable=SC1090
source "${CONFIG_FILE}"

set +a

case "${OPENAI_API_KEY:-}" in
    ""|REPLACE_WITH_REAL_OPENAI_API_KEY|PASTE_*)
        echo "ERROR: OPENAI_API_KEY is missing or still a placeholder." >&2
        echo "Edit:" >&2
        echo "  ${CONFIG_FILE}" >&2
        exit 3
        ;;
esac

export OPENAI_MODEL="${OPENAI_MODEL:-gpt-4.1-nano}"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    echo "ERROR: Python command not found:" >&2
    echo "  ${PYTHON_BIN}" >&2
    exit 4
fi

if ! "${PYTHON_BIN}" -c 'import openai' >/dev/null 2>&1; then
    echo "ERROR: The OpenAI Python package is not installed." >&2
    echo "Current Python:" >&2
    echo "  $(command -v "${PYTHON_BIN}" || true)" >&2
    echo >&2
    echo "Install it in the active environment with:" >&2
    echo "  python -m pip install -r requirements-stage6.txt" >&2
    exit 5
fi

umask 077

echo "Fault2Assertion Stage-6 API smoke"
echo "--------------------------------"
echo "Repository : ${F2A_ROOT}"
echo "Config     : ${CONFIG_FILE}"
echo "Python     : $(command -v "${PYTHON_BIN}")"
echo "Model      : ${OPENAI_MODEL}"
echo

exec "${PYTHON_BIN}" \
    "${F2A_ROOT}/scripts/assertion_generation/api_smoke.py" \
    "$@"
