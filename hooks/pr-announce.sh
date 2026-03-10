#!/usr/bin/env bash
# PostToolUse GitHub PR — thin dispatcher (DES-017).
# Fast gate: skip Python startup in repos without .biff enabled.
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
[[ -f "$REPO_ROOT/.biff" ]] || exit 0
[[ -f "$REPO_ROOT/.biff.local" ]] && grep -qE '^enabled\s*=\s*true' "$REPO_ROOT/.biff.local" || exit 0
biff hook claude-code post-pr 2>/dev/null || true
