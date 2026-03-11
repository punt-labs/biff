#!/usr/bin/env bash
[[ -f "$HOME/.punt-hooks-kill" ]] && exit 0
# Stop — thin dispatcher (DES-017).
# Fast gate: skip Python startup in repos without .biff enabled.
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
[[ -f "$REPO_ROOT/.biff" ]] || exit 0
[[ -f "$REPO_ROOT/.biff.local" ]] && grep -qE '^enabled[[:space:]]*=[[:space:]]*true' "$REPO_ROOT/.biff.local" || exit 0
biff-hook claude-code stop 2>/dev/null || true
