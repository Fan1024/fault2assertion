#!/usr/bin/env bash

set -u
set -o pipefail

if [ "$#" -lt 3 ]; then
    echo "Usage:"
    echo "  $0 <rtl|netlist> <run_id> <xrun arguments...>"
    exit 2
fi

MODE="$1"
RUN_ID="$2"
shift 2

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

RUN_DIR="$ROOT/runs/cv32e40p/crc32/$MODE/$RUN_ID"
WORK_DIR="$ROOT/build/xcelium_work/$MODE/$RUN_ID"

rm -rf "$WORK_DIR"

mkdir -p "$RUN_DIR"
mkdir -p "$WORK_DIR"

echo "============================================================"
echo "Mode     : $MODE"
echo "Run ID   : $RUN_ID"
echo "Run dir  : $RUN_DIR"
echo "Work dir : $WORK_DIR"
echo "============================================================"

(
    cd "$WORK_DIR"

    env \
        -u OPENAI_API_KEY \
        -u ANTHROPIC_API_KEY \
        xrun "$@" -l "$RUN_DIR/xrun.log"
)

XRUN_STATUS=$?

for artifact in \
    riscy_tb.vcd \
    result.txt
do
    if [ -f "$WORK_DIR/$artifact" ]; then
        mv "$WORK_DIR/$artifact" "$RUN_DIR/"
    fi
done

if [ "${KEEP_XCELIUM_WORK:-0}" = "1" ]; then
    echo "Keeping Xcelium work directory:"
    echo "  $WORK_DIR"
else
    rm -rf "$WORK_DIR"
fi

echo "============================================================"
echo "Xcelium exit status: $XRUN_STATUS"
echo "Log: $RUN_DIR/xrun.log"
echo "============================================================"

exit "$XRUN_STATUS"
