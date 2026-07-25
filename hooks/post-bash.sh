#!/usr/bin/env bash
[[ -f "$HOME/.punt-hooks-kill" ]] && exit 0
# PostToolUse Bash — thin dispatcher (DES-017).
# Fast gate: skip unless the committed .punt-labs/biff/enabled marker is
# present AND biff is installed — a cloned marker-enabled repo without biff
# must no-op, never error (tool-enable-disable.md §2.7/§2.11).
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
[[ -f "$REPO_ROOT/.punt-labs/biff/enabled" ]] || exit 0
command -v biff-hook >/dev/null 2>&1 || exit 0
biff-hook claude-code post-bash 2>/dev/null || true
