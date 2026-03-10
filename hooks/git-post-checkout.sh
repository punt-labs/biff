#!/usr/bin/env bash
# Git post-checkout — thin dispatcher (DES-017).
# Installed into .git/hooks/post-checkout by `biff install`.
# Fast gate: skip Python startup in repos without .biff enabled.
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
[[ -f "$REPO_ROOT/.biff" ]] || exit 0
[[ -f "$REPO_ROOT/.biff.local" ]] && grep -qE '^enabled\s*=\s*true' "$REPO_ROOT/.biff.local" || exit 0
biff hook git post-checkout "$1" "$2" "$3" 2>/dev/null || true
