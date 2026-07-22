#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(
    cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1
    pwd
)

F2A_ROOT=$(
    cd -- "$SCRIPT_DIR/.." >/dev/null 2>&1
    pwd
)

CRC32_ROOT="$F2A_ROOT/workloads/crc32"

[[ -d "$CRC32_ROOT" ]] || {
    echo "ERROR: CRC32 workload directory does not exist:"
    echo "       $CRC32_ROOT"
    exit 1
}

echo "============================================================"
echo "CRC32 files"
echo "============================================================"

find "$CRC32_ROOT" \
    -maxdepth 2 \
    -type f \
    | sort

echo
echo "============================================================"
echo "C entry points and CRC functions"
echo "============================================================"

grep -RInE \
    '\bmain[[:space:]]*\(|\bcrc[[:alnum:]_]*[[:space:]]*\(' \
    "$CRC32_ROOT" \
    --include='*.c' \
    --include='*.h' \
    || true

echo
echo "============================================================"
echo "Possible host or operating-system dependencies"
echo "============================================================"

grep -RInE \
    '\b(fopen|fclose|fread|fwrite|fseek|fprintf|printf|malloc|calloc|realloc|free|clock|gettimeofday|exit)[[:space:]]*\(' \
    "$CRC32_ROOT" \
    --include='*.c' \
    --include='*.h' \
    || true

echo
echo "============================================================"
echo "Command-line argument usage"
echo "============================================================"

grep -RInE \
    '\bargc\b|\bargv\b' \
    "$CRC32_ROOT" \
    --include='*.c' \
    --include='*.h' \
    || true

echo
echo "============================================================"
echo "Included headers"
echo "============================================================"

grep -RInE \
    '^[[:space:]]*#[[:space:]]*include' \
    "$CRC32_ROOT" \
    --include='*.c' \
    --include='*.h' \
    || true

echo
echo "============================================================"
echo "Input and data file sizes"
echo "============================================================"

find "$CRC32_ROOT" \
    -maxdepth 2 \
    -type f \
    ! -name '*.c' \
    ! -name '*.h' \
    ! -name 'Makefile' \
    -printf '%10s bytes  %p\n' \
    | sort -n
