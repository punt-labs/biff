#!/usr/bin/env bash
[[ -f "$HOME/.punt-hooks-kill" ]] && exit 0
# Git post-commit — thin dispatcher (DES-017).
# Installed into .git/hooks/post-commit by `biff install`.
# Fast gate: skip Python startup in repos without .biff enabled.
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
[[ -f "$REPO_ROOT/.biff" ]] || exit 0
[[ -f "$REPO_ROOT/.biff.local" ]] && grep -qE '^enabled[[:space:]]*=[[:space:]]*true' "$REPO_ROOT/.biff.local" || exit 0
biff-hook git post-commit 2>/dev/null || true
