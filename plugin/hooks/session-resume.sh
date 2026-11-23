#!/usr/bin/env bash
[[ -f "$HOME/.punt-hooks-kill" ]] && exit 0
# SessionStart(resume|compact) — thin dispatcher (DES-017).
# Fast gate: skip unless the committed .punt-labs/biff/enabled marker is a
# regular file (not a symlink) AND the biff-hook entry point is on PATH — a
# clone of a marker-enabled repo without biff-hook installed must no-op,
# never error (tool-enable-disable.md §2.7/§2.11).
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
MARKER="$REPO_ROOT/.punt-labs/biff/enabled"
[[ -f "$MARKER" && ! -L "$MARKER" ]] || exit 0
command -v biff-hook >/dev/null 2>&1 || exit 0
# The loaded plugin's name picks the poll command the warning names:
# /biff:read for "biff", /biff-dev:read-dev for the --plugin-dir "biff-dev".
BIFF_PLUGIN_NAME=$("$(dirname "$0")/plugin-name.sh" 2>/dev/null)
export BIFF_PLUGIN_NAME
biff-hook claude-code session-resume 2>/dev/null || true
