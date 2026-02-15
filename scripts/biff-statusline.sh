#!/usr/bin/env bash
# SUPERSEDED by `biff install-statusline` / `biff uninstall-statusline`.
# Kept as a reference/fallback. The install command points Claude Code's
# statusLine setting at `biff statusline` (a Python CLI command) instead.
#
# Claude Code status line: context usage + biff unread messages.
#
# Output examples:
#   biff(3) | 32%
#   biff | 32%
#   biff
#
# Setup — add to ~/.claude/settings.json:
#
#   { "statusLine": "/path/to/biff-statusline.sh" }
#
# Requires: jq

set -euo pipefail

UNREAD_FILE="${BIFF_UNREAD_PATH:-$HOME/.biff/unread.json}"

# Claude Code pipes session JSON on stdin.
INPUT=$(cat)

# --- Biff + unread (grouped) ---
BIFF="biff"
if [[ -f "$UNREAD_FILE" ]]; then
    COUNT=$(jq -r '.count // 0' "$UNREAD_FILE" 2>/dev/null || echo "0")
    if [[ "$COUNT" -gt 0 ]]; then
        BIFF="biff(${COUNT})"
    fi
fi
STATUS="$BIFF"

# --- Context % ---
USAGE=$(echo "$INPUT" | jq '.context_window.current_usage // null' 2>/dev/null || true)
if [[ "$USAGE" != "null" && -n "$USAGE" ]]; then
    CURRENT=$(echo "$USAGE" | jq '.input_tokens + .cache_creation_input_tokens + .cache_read_input_tokens' 2>/dev/null || echo "0")
    SIZE=$(echo "$INPUT" | jq '.context_window.context_window_size // 0' 2>/dev/null || echo "0")
    if [[ "$SIZE" -gt 0 ]]; then
        PCT=$((CURRENT * 100 / SIZE))
        STATUS="${STATUS} | ${PCT}%"
    fi
fi

echo "$STATUS"
