#!/usr/bin/env bash
# PostToolUse Bash — thin dispatcher (DES-017).
# Fast gate: skip Python startup in repos without .biff enabled.
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
[[ -f "$REPO_ROOT/.biff" ]] || exit 0
[[ -f "$REPO_ROOT/.biff.local" ]] && grep -qE '^enabled[[:space:]]*=[[:space:]]*true' "$REPO_ROOT/.biff.local" || exit 0
biff hook claude-code post-bash 2>/dev/null || true
