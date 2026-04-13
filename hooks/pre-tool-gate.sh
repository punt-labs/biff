#!/usr/bin/env bash
# PreToolUse Edit|Write — thin dispatcher (DES-017).
# NOTE: No kill-switch here — this is a security boundary (authz gate).
# Fast gate: skip Python startup in repos without .punt-labs/biff/config.local.yaml enabled.
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
CONFIG_LOCAL="$REPO_ROOT/.punt-labs/biff/config.local.yaml"
[[ -f "$CONFIG_LOCAL" ]] || exit 0
grep -qiE '^[[:space:]]*enabled[[:space:]]*:[[:space:]]*(true|yes|on)[[:space:]]*$' "$CONFIG_LOCAL" || exit 0
# Fast gate: skip if no active biff MCP server session.
BIFF_ACTIVE="$HOME/.punt-labs/biff/active"
if [[ -d "$BIFF_ACTIVE" ]]; then
    set -- "$BIFF_ACTIVE"/*
    [[ -e "$1" ]] || exit 0
else
    exit 0
fi
biff-hook claude-code pre-tool-use 2>>"$HOME/.punt-labs/biff/hook-errors.log" || true
