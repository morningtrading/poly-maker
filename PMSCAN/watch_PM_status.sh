#!/usr/bin/env bash
# watch_PM_status.sh — re-run show_PM_status.sh on an interval.
#
# Usage: ./watch_PM_status.sh [user|wallet] [hours] [interval_seconds]
#   user|wallet        Polymarket handle or 0x address (default: morningtrading)
#   hours              window for fills/stats (default: empty = all activity)
#   interval_seconds   refresh interval (default: 300 = 5 minutes)
#
# Examples:
#   ./watch_PM_status.sh                            # @morningtrading, all activity, every 5 min
#   ./watch_PM_status.sh morningtrading 4           # last 4h window, every 5 min
#   ./watch_PM_status.sh morningtrading 4 60        # last 4h, refresh every 60 seconds
#
# Press Ctrl+C to stop. Hyperlinks stay clickable because output is written
# straight to the terminal (unlike `watch`, which strips OSC 8 sequences).

set -u

USER_ARG="${1:-morningtrading}"
HOURS_ARG="${2:-}"
INTERVAL="${3:-300}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="$SCRIPT_DIR/show_PM_status.sh"

if [[ ! -x "$TARGET" ]]; then
    echo "cannot find or execute $TARGET" >&2
    exit 1
fi

trap 'echo; echo "stopped."; exit 0' INT

while true; do
    clear
    printf '\033[2m%s — refresh every %ss — Ctrl+C to stop\033[0m\n\n' \
        "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$INTERVAL"
    "$TARGET" "$USER_ARG" ${HOURS_ARG:+"$HOURS_ARG"}
    sleep "$INTERVAL"
done
