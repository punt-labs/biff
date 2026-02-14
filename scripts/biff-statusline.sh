#!/usr/bin/env bash
# Claude Code status line script for biff unread messages.
#
# Reads ~/.biff/data/unread.json and prints an unread count.
# When there are no unread messages, prints nothing.
#
# Setup — add to ~/.claude/settings.json:
#
#   { "statusLine": "/path/to/biff-statusline.sh" }
#
# Requires: jq

set -euo pipefail

UNREAD_FILE="${BIFF_UNREAD_PATH:-$HOME/.biff/data/unread.json}"

# Drain stdin (Claude Code pipes session JSON; unused here).
cat > /dev/null

if [[ ! -f "$UNREAD_FILE" ]]; then
    exit 0
fi

COUNT=$(jq -r '.count // 0' "$UNREAD_FILE" 2>/dev/null || echo "0")

if [[ "$COUNT" -gt 0 ]]; then
    PREVIEW=$(jq -r '.preview // ""' "$UNREAD_FILE" 2>/dev/null || echo "")
    if [[ -n "$PREVIEW" ]]; then
        echo "biff: $COUNT unread — $PREVIEW"
    else
        echo "biff: $COUNT unread"
    fi
fi
