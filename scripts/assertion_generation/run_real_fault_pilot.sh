#!/usr/bin/env bash
set -euo pipefail

F2A_ROOT="${F2A_ROOT:-/raid/spring2026/fwu44/research/fault2assertion}"

CAMPAIGN_ROOT="${CAMPAIGN_ROOT:-$F2A_ROOT/runs/stage5_campaign_v3/cv32e40p/crc32/sites_all}"

CONFIG_FILE="${F2A_OPENAI_ENV:-${HOME}/.config/fault2assertion/openai.env}"

ACTION="${ACTION:-select}"

PY="$F2A_ROOT/scripts/assertion_generation/real_fault_pilot.py"

case "$ACTION" in

    select)
        python3 "$PY" \
            --campaign-root "$CAMPAIGN_ROOT" \
            select \
            "$@"
        ;;

    prepare)
        : "${FAULT_ID:?Set FAULT_ID before ACTION=prepare}"

        python3 "$PY" \
            --campaign-root "$CAMPAIGN_ROOT" \
            prepare \
            --fault-id "$FAULT_ID" \
            "$@"
        ;;

    generate)
        : "${FAULT_ID:?Set FAULT_ID before ACTION=generate}"

        [[ -r "$CONFIG_FILE" ]] || {
            echo "ERROR: OpenAI config file is missing or unreadable:" >&2
            echo "  $CONFIG_FILE" >&2
            exit 4
        }

        python3 "$PY" \
            --campaign-root "$CAMPAIGN_ROOT" \
            generate \
            --fault-id "$FAULT_ID" \
            --credential-file "$CONFIG_FILE" \
            "$@"
        ;;

    execute)
        : "${FAULT_ID:?Set FAULT_ID before ACTION=execute}"

        [[ -r "$CONFIG_FILE" ]] || {
            echo "ERROR: OpenAI config file is missing or unreadable:" >&2
            echo "  $CONFIG_FILE" >&2
            exit 4
        }

        python3 "$PY" \
            --campaign-root "$CAMPAIGN_ROOT" \
            execute \
            --fault-id "$FAULT_ID" \
            --credential-file "$CONFIG_FILE" \
            "$@"
        ;;

    *)
        echo \
            "ERROR: ACTION must be select, prepare, generate, or execute; got $ACTION" \
            >&2
        exit 2
        ;;
esac
