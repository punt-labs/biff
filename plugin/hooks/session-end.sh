#!/usr/bin/env bash
[[ -f "$HOME/.punt-hooks-kill" ]] && exit 0
# SessionEnd — thin dispatcher (DES-017).
# Fast gate: skip unless the committed .punt-labs/biff/enabled marker is a
# regular file (not a symlink) AND the biff-hook entry point is on PATH — a
# clone of a marker-enabled repo without biff-hook installed must no-op,
# never error (tool-enable-disable.md §2.7/§2.11).
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
MARKER="$REPO_ROOT/.punt-labs/biff/enabled"
[[ -f "$MARKER" && ! -L "$MARKER" ]] || exit 0
command -v biff-hook >/dev/null 2>&1 || exit 0
# Detach the cleanup call. `biff-hook claude-code session-end` clears this
# session's biff presence — it writes reap sentinels and removes the local
# active markers (all local filesystem; the relay KV delete happens later,
# when a running relay reaps the sentinel). Its cost is biff-hook's
# interpreter startup/import (several seconds) plus those local writes, not a
# relay network call. Run synchronously, that cost blocks process exit, and
# Claude Code cancels any SessionEnd hook still running when it tears down
# ("Hook cancelled", issue #471). Return at once, and bound the detached call
# with timeout when available so a wedged startup cannot leave a process
# lingering. `timeout` (GNU coreutils) is optional — not on a clean macOS —
# so degrade: gtimeout, then no timeout. The call's output is not consumed by
# the model, but its stderr goes to the shared hook-errors log (as
# pre-tool-gate.sh does) so a failed detached cleanup is still debuggable.
if command -v timeout >/dev/null 2>&1; then
    _BIFF_TIMEOUT="timeout 10"
elif command -v gtimeout >/dev/null 2>&1; then
    _BIFF_TIMEOUT="gtimeout 10"
else
    _BIFF_TIMEOUT=""
fi
_BIFF_LOG="$HOME/.punt-labs/biff/hook-errors.log"
mkdir -p "$HOME/.punt-labs/biff" 2>/dev/null || true
nohup bash -c "$_BIFF_TIMEOUT biff-hook claude-code session-end >/dev/null 2>>\"$_BIFF_LOG\"" >/dev/null 2>&1 &
disown 2>/dev/null || true
exit 0
