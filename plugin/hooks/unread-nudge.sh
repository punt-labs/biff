#!/usr/bin/env bash
[[ -f "$HOME/.punt-hooks-kill" ]] && exit 0
# UserPromptSubmit + PostToolUse — nudge the model to check unread biff mail.
#
# The MCP tool-description mutation channel (`read_messages` growing an
# "(N unread)" suffix) never reaches the model mid-session: Claude Code
# re-fetches the tool list on tools/list_changed, but the model's own view of
# a tool's description is a session-start snapshot that re-fetch does not
# update. additionalContext is the one channel proven to reach the model on
# every turn — see docs/design-nats-push.md, "the client-wake gap", and
# plugin/commands/read.md §C, which carries the same asymmetry for talk.
#
# Reads this session's per-session unread file directly (DES-011a) and
# nudges once per *change* in the unread count — not once per tool call —
# by tracking the last-nudged count in a sibling ".nudged" file. No repo
# marker check: unlike PreCompact/post-bash, this hook makes no `biff-hook`
# call to gate, and the unread file's own absence is already the cheapest
# possible no-op for "biff isn't in play for this session."
#
# Pure shell + jq, no Python startup: this fires on every prompt submit and
# every tool call, so it must stay well inside Claude Code's <100ms
# UserPromptSubmit budget (docs/hook-lifecycle.md §5) — a Python CLI's
# interpreter+import cost alone (~0.3s, per that doc's measurements) would
# blow the budget on its own.

_stdin=$(cat)

command -v jq >/dev/null 2>&1 || exit 0

_event=$(printf '%s' "$_stdin" | jq -r '.hook_event_name // empty' 2>/dev/null)
[[ -n "$_event" ]] || exit 0

# --- Resolve this session's unread-file key: the topmost `claude` ancestor
# PID, mirroring src/biff/session_key.py's find_session_key() in pure shell
# (one `ps` call, not a Python subprocess). Falls back to $PPID, matching
# that function's own os.getppid() fallback.
_session_key=""
if _ps_table=$(ps -eo pid=,ppid=,comm= 2>/dev/null); then
  declare -A _ppid_of=() _comm_of=()
  while read -r _pid _ppid _comm; do
    [[ -n "$_pid" ]] || continue
    _ppid_of["$_pid"]="$_ppid"
    _comm_of["$_pid"]="$_comm"
  done <<<"$_ps_table"

  _walk="$$"
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [[ -n "${_ppid_of[$_walk]+x}" ]] || break
    _base="${_comm_of[$_walk]##*/}"
    [[ "$_base" == "claude" ]] && _session_key="$_walk"
    _next="${_ppid_of[$_walk]}"
    [[ "$_next" == "$_walk" || "$_next" == "0" ]] && break
    _walk="$_next"
  done
fi
[[ -n "$_session_key" ]] || _session_key="$PPID"

_unread_dir="$HOME/.punt-labs/biff/unread"
_unread_file="$_unread_dir/${_session_key}.json"
_nudge_file="$_unread_dir/${_session_key}.nudged"

[[ -f "$_unread_file" ]] || exit 0

_count=$(jq -r '.count // 0' "$_unread_file" 2>/dev/null)
[[ "$_count" =~ ^[0-9]+$ ]] || exit 0

if [[ "$_count" == "0" ]]; then
  # Back to zero (read elsewhere, e.g. `/biff:read` or another tool) —
  # clear the gate so the next arrival nudges again from a clean baseline.
  rm -f "$_nudge_file" 2>/dev/null
  exit 0
fi

_last=""
[[ -f "$_nudge_file" ]] && _last=$(cat "$_nudge_file" 2>/dev/null)

# Rising-change gate: only nudge when the count is nonzero AND different
# from the last count we nudged for — not on every tick while it holds
# steady, but again if it grows (or shrinks without reaching zero).
[[ "$_count" == "$_last" ]] && exit 0

printf '%s' "$_count" >"$_nudge_file" 2>/dev/null

_plural="s"
[[ "$_count" == "1" ]] && _plural=""
_msg="You have ${_count} unread biff message${_plural} — call read_messages now."

jq -n --arg event "$_event" --arg ctx "$_msg" '{
  hookSpecificOutput: {
    hookEventName: $event,
    additionalContext: $ctx
  }
}'
exit 0
