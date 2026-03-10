#!/usr/bin/env bash
# Git pre-push — thin dispatcher (DES-017).
# Installed into .git/hooks/pre-push by `biff install`.
# Fast gate: skip Python startup in repos without .biff enabled.
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
[[ -f "$REPO_ROOT/.biff" ]] || exit 0
[[ -f "$REPO_ROOT/.biff.local" ]] && grep -qE '^enabled\s*=\s*true' "$REPO_ROOT/.biff.local" || exit 0
biff hook git pre-push "$1" 2>/dev/null || true
