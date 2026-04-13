#!/usr/bin/env bash
[[ -f "$HOME/.punt-hooks-kill" ]] && exit 0
# PostToolUse Bash — thin dispatcher (DES-017).
# Fast gate: skip Python startup in repos without .punt-labs/biff/config.local.yaml enabled.
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
CONFIG_LOCAL="$REPO_ROOT/.punt-labs/biff/config.local.yaml"
[[ -f "$CONFIG_LOCAL" ]] || exit 0
grep -qiE '^enabled[[:space:]]*:[[:space:]]*(true|yes|on)[[:space:]]*$' "$CONFIG_LOCAL" || exit 0
biff-hook claude-code post-bash 2>/dev/null || true
