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
# session's biff presence. With mcp.transport: daemon it asks biffd to end the
# session named in the payload, then removes the local files; otherwise it
# writes reap sentinels and removes the local active markers (the relay KV
# delete happens later, when a running relay reaps the sentinel). Its cost is
# biff-hook's interpreter startup/import (several seconds) plus those calls.
# Run synchronously, that cost blocks process exit, and
# Claude Code cancels any SessionEnd hook still running when it tears down
# ("Hook cancelled", issue #471). Return at once, and bound the detached call
# with timeout when available so a wedged startup cannot leave a process
# lingering. `timeout` (GNU coreutils) is optional — not on a clean macOS —
# so degrade: gtimeout, then no timeout. The child configures its own durable
# diagnostics; the model does not consume its stdout or stderr. The limit
# outlasts the call's own budget (15 s counted from its start, in
# biff.plugins.claude_code.hook_daemon), so the call finishes or gives up, and
# cleans up after itself, before this kills it mid-request.
if command -v timeout >/dev/null 2>&1; then
    _BIFF_TIMEOUT=(timeout 20)
elif command -v gtimeout >/dev/null 2>&1; then
    _BIFF_TIMEOUT=(gtimeout 20)
else
    _BIFF_TIMEOUT=()
fi
# The payload names the session that ended; hand it to the detached call. The
# read is bounded: a stdin that never closes must not hold the exit.
IFS= read -r -t 2 -d '' _BIFF_PAYLOAD || true
nohup "${_BIFF_TIMEOUT[@]}" biff-hook claude-code session-end <<<"$_BIFF_PAYLOAD" >/dev/null 2>&1 &
disown 2>/dev/null || true
exit 0
