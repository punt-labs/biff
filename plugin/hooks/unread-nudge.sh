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
# Honors `/mesg off` (biff_enabled: false in the unread file): mesg blocks
# unsolicited notification the same way BSD mesg(1) blocks write(1)/wall(1),
# so this hook — a proactive, unsolicited nudge — stays silent while muted.
# `/biff:read`, a deliberate pull the user/model initiates, is not
# "notification" in that sense and is unaffected — it still treats a
# mesg-off statusline reading as "count unknown, pull anyway" (see
# plugin/commands/read.md). The two paths are intentionally asymmetric.
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
#
# The walk itself runs in awk, not bash: `declare -A` (bash 4+) is not
# available in macOS's system /bin/bash (3.2, GPLv2-frozen), so building the
# pid->ppid/comm maps as bash associative arrays silently errored out on
# macOS and disabled the whole hook there (every session fell back to
# $PPID). awk has always had associative arrays and ships on every target
# platform (POSIX-required), so the two-pass build-then-walk moves there
# entirely; bash only captures awk's single-line result.
_session_key=""
if _ps_table=$(ps -eo pid=,ppid=,comm= 2>/dev/null); then
  _session_key=$(printf '%s\n' "$_ps_table" | awk -v start="$$" '
    {
      pid = $1; ppid = $2; comm = $3
      n = split(comm, parts, "/")
      base[pid] = parts[n]
      parent[pid] = ppid
    }
    END {
      walk = start
      found = ""
      for (i = 0; i < 10; i++) {
        if (!(walk in parent)) break
        if (base[walk] == "claude") found = walk
        nxt = parent[walk]
        if (nxt == walk || nxt == "0") break
        walk = nxt
      }
      print found
    }
  ' 2>/dev/null)
fi
[[ -n "$_session_key" ]] || _session_key="$PPID"

_unread_dir="$HOME/.punt-labs/biff/unread"
_unread_file="$_unread_dir/${_session_key}.json"
_nudge_file="$_unread_dir/${_session_key}.nudged"

# A .nudged sidecar with no matching .json is stale — the primary cleanup
# lives in the server's own shutdown path (src/biff/server/app.py removes
# both files together via _remove_unread_files, on every caught signal),
# but nothing can catch SIGKILL, so a PID that outlived a prior session's
# unclean kill and got reused by an unrelated new session could otherwise
# inherit a sidecar whose leftover count happens to match the new
# session's first real count, silently suppressing the nudge that should
# fire for it. Self-heal here too: no .json means nothing to gate, so any
# sidecar is stale regardless of cause.
if [[ ! -f "$_unread_file" ]]; then
  rm -f "$_nudge_file" 2>/dev/null
  exit 0
fi

# `// true` would be wrong here: jq's `//` treats a literal `false` the
# same as `null`/missing, which would silently un-mute a mesg-off session.
# `!= false` is exact: only an explicit boolean `false` yields "false";
# missing (null) or `true` both yield "true" (default-enabled).
_enabled=$(jq -r '(.biff_enabled != false)' "$_unread_file" 2>/dev/null)
if [[ "$_enabled" == "false" ]]; then
  # Clear the sidecar unconditionally while muted, not only at count==0
  # below (unreachable from here — we exit before ever reading $_count).
  # Without this, a stamp written before muting survives the entire muted
  # period untouched; if the count changes one or more times while muted
  # (read to zero, new mail arrives, etc.) and happens to land back on
  # that same stale value by the time mesg comes back on, the rising-
  # change gate below would compare equal and silently swallow a nudge
  # for genuinely unread mail the user was never told about (Bugbot
  # finding hw_9-). Clearing here guarantees the first post-unmute tick
  # always starts from a clean "never nudged" baseline.
  rm -f "$_nudge_file" 2>/dev/null
  exit 0
fi

_count=$(jq -r '.count // 0' "$_unread_file" 2>/dev/null)
[[ "$_count" =~ ^[0-9]+$ ]] || exit 0

# The writing server process's own pid (os.getpid(), src/biff/server/tools/
# _descriptions.py _write_unread_file) — the second half of the sidecar's
# staleness signal below, alongside the .json-existence check above. A
# same-PID-reused-by-an-unrelated-session .json is freshly written by a
# different server process, so its server_pid differs from whatever the
# stale .nudged sidecar last recorded even though the .json itself exists.
_server_pid=$(jq -r '.server_pid // empty' "$_unread_file" 2>/dev/null)

if [[ "$_count" == "0" ]]; then
  # Back to zero (read elsewhere, e.g. `/biff:read` or another tool) —
  # clear the gate so the next arrival nudges again from a clean baseline.
  rm -f "$_nudge_file" 2>/dev/null
  exit 0
fi

# _last_count stays "" (never-nudged baseline) unless the sidecar is both
# new-format (pid:count) AND stamped with THIS json's current server_pid.
# A pid mismatch means a different server process wrote the .json since
# the sidecar was last updated — PID reuse (an unrelated later session)
# or, at minimum, a same-PID subprocess restart — either way the recorded
# count cannot be trusted. A bare old-format sidecar (no colon; predates
# this fix) is likewise distrusted rather than risking a coincidental
# match against a bare integer.
_last_count=""
if [[ -f "$_nudge_file" ]]; then
  _sidecar=$(cat "$_nudge_file" 2>/dev/null)
  if [[ "$_sidecar" == *:* ]]; then
    _last_pid="${_sidecar%%:*}"
    if [[ "$_last_pid" == "$_server_pid" ]]; then
      _last_count="${_sidecar#*:}"
    fi
  fi
fi

# Rising-change gate: only nudge when the count is nonzero AND different
# from the last count we nudged for — not on every tick while it holds
# steady, but again if it grows (or shrinks without reaching zero).
[[ "$_count" == "$_last_count" ]] && exit 0

printf '%s:%s' "$_server_pid" "$_count" >"$_nudge_file" 2>/dev/null

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
