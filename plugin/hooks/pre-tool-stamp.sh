#!/usr/bin/env bash
[[ -f "$HOME/.punt-hooks-kill" ]] && exit 0
# PreToolUse on biff's own MCP tools: declare the caller (_caller) on the call.
# No repo-enabled gate: an unstamped biff call is refused by the server.
# No-op, never an error, when biff-hook is not installed.
command -v biff-hook >/dev/null 2>&1 || exit 0
# The log directory may not exist yet on a fresh machine; a failed redirect
# would otherwise fail the hook before it ran.
mkdir -p "$HOME/.punt-labs/biff"
# A real hook failure keeps its exit status, so Claude Code reports it instead
# of the call proceeding unstamped without a trace.
biff-hook claude-code pre-tool-stamp 2>>"$HOME/.punt-labs/biff/hook-errors.log"
