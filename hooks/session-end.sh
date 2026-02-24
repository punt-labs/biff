#!/usr/bin/env bash
# SessionEnd — thin dispatcher (DES-017).
# Fast gate: skip Python startup in repos without .biff.
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
[[ -f "$REPO_ROOT/.biff" ]] || exit 0
biff hook claude-code session-end 2>/dev/null || true
