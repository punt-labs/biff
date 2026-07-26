#!/usr/bin/env bash
# PreToolUse Edit|Write — thin dispatcher (DES-017).
# NOTE: No kill-switch here — this is a security boundary (authz gate).
# Fast gate: skip unless the committed .punt-labs/biff/enabled marker is a
# regular file (not a symlink) AND the biff-hook entry point is on PATH — a
# clone of a marker-enabled repo without biff-hook installed must no-op,
# never error (tool-enable-disable.md §2.7/§2.11).
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
MARKER="$REPO_ROOT/.punt-labs/biff/enabled"
[[ -f "$MARKER" && ! -L "$MARKER" ]] || exit 0
command -v biff-hook >/dev/null 2>&1 || exit 0
# Fast gate: skip if no active biff MCP server session.
BIFF_ACTIVE="$HOME/.punt-labs/biff/active"
if [[ -d "$BIFF_ACTIVE" ]]; then
    set -- "$BIFF_ACTIVE"/*
    [[ -e "$1" ]] || exit 0
else
    exit 0
fi
biff-hook claude-code pre-tool-use 2>>"$HOME/.punt-labs/biff/hook-errors.log" || true
