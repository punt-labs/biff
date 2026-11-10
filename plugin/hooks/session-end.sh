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
# Detach the relay call. biff-hook makes a network request that normally
# takes several seconds and can hang on a dead relay; Claude Code cancels any
# SessionEnd hook still running when it exits ("Hook cancelled", issue #471).
# Return at once so the exit is never blocked, and bound the detached call
# with timeout when available so a hung relay cannot leave a process
# lingering. `timeout` (GNU coreutils) is optional — not on a clean macOS —
# so degrade: gtimeout, then no timeout.
if command -v timeout >/dev/null 2>&1; then
    _BIFF_TIMEOUT="timeout 10"
elif command -v gtimeout >/dev/null 2>&1; then
    _BIFF_TIMEOUT="gtimeout 10"
else
    _BIFF_TIMEOUT=""
fi
nohup bash -c "$_BIFF_TIMEOUT biff-hook claude-code session-end >/dev/null 2>&1" >/dev/null 2>&1 &
disown 2>/dev/null || true
exit 0
