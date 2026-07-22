#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CV32E40P_HOME="${CV32E40P_HOME:-${ROOT_DIR}/../cv32e40p}"

if [[ ! -d "${CV32E40P_HOME}/example_tb/core/custom" ]]; then
    echo "ERROR: CV32E40P runtime directory was not found:"
    echo "  ${CV32E40P_HOME}/example_tb/core/custom"
    echo
    echo "Set it explicitly, for example:"
    echo "  export CV32E40P_HOME=/raid/spring2026/fwu44/research/cv32e40p"
    exit 1
fi

CV32E40P_HOME="$(cd "${CV32E40P_HOME}" && pwd)"

if [[ -n "${CROSS_COMPILE:-}" ]]; then
    :
elif [[ -n "${RISCV:-}" ]] && \
     [[ -x "${RISCV}/bin/riscv32-unknown-elf-gcc" ]]; then
    CROSS_COMPILE="${RISCV}/bin/riscv32-unknown-elf-"
elif command -v riscv32-unknown-elf-gcc >/dev/null 2>&1; then
    GCC_PATH="$(command -v riscv32-unknown-elf-gcc)"
    CROSS_COMPILE="${GCC_PATH%gcc}"
elif [[ -x "${HOME}/.riscv/bin/riscv32-unknown-elf-gcc" ]]; then
    CROSS_COMPILE="${HOME}/.riscv/bin/riscv32-unknown-elf-"
else
    echo "ERROR: riscv32-unknown-elf-gcc was not found."
    echo
    echo "Supported configurations:"
    echo "  export RISCV=/path/to/riscv/toolchain"
    echo "or"
    echo "  export CROSS_COMPILE=/path/to/riscv32-unknown-elf-"
    exit 1
fi

if [[ ! -x "${CROSS_COMPILE}gcc" ]] && \
   ! command -v "${CROSS_COMPILE}gcc" >/dev/null 2>&1; then
    echo "ERROR: Compiler is not executable:"
    echo "  ${CROSS_COMPILE}gcc"
    exit 1
fi

if [[ "$#" -eq 0 ]]; then
    set -- all
fi

exec make \
    -C "${ROOT_DIR}/platform/cv32e40p" \
    "CV32E40P_HOME=${CV32E40P_HOME}" \
    "CROSS_COMPILE=${CROSS_COMPILE}" \
    "$@"
