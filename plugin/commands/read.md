---
description: "Check your inbox for new messages"
argument-hint: "[duration | n | status | force]"
allowed-tools: ["Bash(biff statusline:*)", "ToolSearch", "mcp__plugin_biff_tty__talk_read", "mcp__plugin_biff_tty__read_messages", "mcp__plugin_biff_tty__set_poll_interval", "mcp__plugin_biff_tty__get_poll_status", "CronCreate", "CronList", "CronDelete"]
---
<!-- markdownlint-disable MD041 -->

## Input

Arguments: $ARGUMENTS

## Task

biff is passive/pull. Incoming talk (invites + real-time messages) and /write
mail are held on the biff server and surface by MUTATING the descriptions of the
`talk` and `read_messages` tools. This command manages polling and checks for
new activity.

**Two complementary delivery paths.** `plugin/hooks/unread-nudge.sh` covers
*active* sessions: it fires on every `UserPromptSubmit` and `PostToolUse`,
reads the same per-session unread file section C reads below, and injects an
`additionalContext` nudge the moment the count rises — the model sees it on
its very next turn, with no `/biff:read` invocation required. This command
(the cron loop in section A, and the manual/forced check in section C) covers
the *idle* gap the nudge hook cannot: a session sitting with no prompt
submitted and no tool called has nothing to fire the hook on, so the cron's
periodic `/biff:read` invocation is still what surfaces mail during an idle
stretch. Running section C by hand also remains the reliable way to
force-check right now, independent of whether the nudge already fired.

### Argument routing

First, check if `$ARGUMENTS` matches a **polling config** command:

- A duration like `1m`, `2m`, `5m`, `10m`, `15m`, `30m`, `1h`, `2h` → start
  polling (section A)
- Exactly `n`, `off`, or `stop` → disable polling (section B)
- Exactly `status` → show polling config (section D)
- Exactly `force` → force-check now (section C, unconditional)

If none of the above match (empty or unrecognized), fall through to **check
now** (section C).

### A. `/biff:read <duration>` — start polling every `<duration>`

1. Call `mcp__plugin_biff_tty__set_poll_interval` with `interval` set to
   `<duration>` — this sets the server-side poll cadence.
2. Delete every existing `/biff:read` auto-poll cron job (see "Managing the cron
   job" below). This must happen **before** creating the new job, so two
   back-to-back `/biff:read 10m` calls leave exactly one cron job, not two.
3. Create a new cron job:

   ```text
   CronCreate(
     cron: "<expression>",   // from the table below
     recurring: true,
     durable: true,          // persists across session restarts
     prompt: "/biff:read"
   )
   ```

   Cron expressions: `1m` → `*/1 * * * *`, `2m` → `*/2 * * * *`,
   `5m` → `*/5 * * * *`, `10m` → `*/10 * * * *`, `15m` → `*/15 * * * *`,
   `30m` → `*/30 * * * *`, `1h` → `0 * * * *`, `2h` → `0 */2 * * *`.
   Sub-minute intervals use `*/1 * * * *`, the 1-minute cron floor. All
   auto-poll jobs must set `durable: true` — without it, the cron dies on
   session exit and the autonomous loop silently stops.
4. Report a single confirmation line combining the `set_poll_interval` response
   and the new cron job ID. Example: `polling set to 10m; cron job 12c9c370
   created (durable, fires */10 * * * *)`.
5. Then fall through and run one check now (section C).

### B. `/biff:read n` — stop polling

Trigger when `$ARGUMENTS` is `n`, `off`, or `stop`.

1. Call `mcp__plugin_biff_tty__set_poll_interval` with `interval` set to `n` —
   this disables the server-side poll cadence.
2. Delete every existing `/biff:read` auto-poll cron job (see "Managing the cron
   job" below).
3. Confirm in one line: polling disabled and the loop job removed. Do NOT fall
   through to a check.

### C. `/biff:read` (no argument) — check now

Also the path when `$ARGUMENTS` is empty or `force`. `talk_read` and
`read_messages` mark-read/consume and can be slow, so do not call them
blindly — gate each on a real signal first. If `$ARGUMENTS` is `force`, run
both pulls unconditionally and skip straight to steps 2-3.

**Why mail no longer gates on the tool description.** The biff server mutates
the `read_messages` description and fires `tools/list_changed` when mail
arrives, and Claude Code does re-fetch `ListTools` within seconds — but the
*model's* view of that description is a session-start snapshot that the
re-fetch does not update; nothing wakes the model to look again until its
next turn, and even then it sees the stale text (see
`docs/design-nats-push.md`, "client-wake gap"). A canary run left a message
sitting unread for 3+ minutes with the status bar correctly showing `(1)` the
whole time, because the marker this command used to key off (`unread)`) was
never visible to the model. Mail now gates on ground truth instead: the
per-session unread file the biff server writes on every change (DES-011a),
read via `biff statusline` — a different code path than the tool-description
mutation, immune to the same staleness. Talk has no equivalent persistent
ground-truth count (see step 2), so it keeps the description-based gate,
supplemented by a best-effort statusline peek.

1. Run `biff statusline` via Bash (e.g. `biff statusline < /dev/null`; no
   stdin needs to be piped in — an empty/closed stdin is read instantly). This
   invokes the plugin's own `biff` binary, which reads this session's on-disk
   unread-status file directly — it does not go through, and is not subject
   to the staleness of, the MCP tool-description path.
   - The output is two lines. Line 1 ends with the biff segment:
     `user:tty(N)` or `user(N)` (no tty name in some setups) — plain text when
     `N` is `0`, wrapped in `\033[1;33m...\033[0m` (bold yellow ANSI) when `N`
     is nonzero. Strip ANSI escape sequences (`\x1b[...m`) and read the
     trailing `(N)` — that integer is the ground-truth unread mail count for
     this session.
   - **Mesg off is a distinct, literal shape, not a number.** When this
     session has run `/mesg off`, the segment renders the bare letter
     `(n)` — plain text, unconditionally, regardless of the real count
     (`_biff_segment` in `src/biff/statusline.py`; `SessionUnread.biff_enabled`
     in `src/biff/unread.py`). The count is deliberately hidden, not zero —
     do not coerce it to `0`. Doing so would silently stop mail delivery to a
     mesg-off session, which would otherwise be the *only* remaining delivery
     path: `plugin/hooks/unread-nudge.sh` (the other, proactive path) also
     honors mesg and stays silent while muted, matching the statusline's own
     count-hiding — only this command's own deliberate pull is unaffected.
     Treat a literal `(n)` as "count unknown, possibly nonzero" — see step
     3's mail gate.
   - Line 2 is either the idle marker `▶` alone, or `▶ <text>` wrapped bold
     red (a wall item) or bold yellow (a talk item) — see step 2.
   - If the command errors, times out, or the output doesn't match either
     shape above (for example the dim "`/biff enable to turn on team
     communication`" line, meaning biff isn't enabled for this session) treat
     `N` as `0` and continue — a parse failure here must never block the rest
     of the command.
2. **Talk** — pull if EITHER signal fires:
   - The live `talk` tool description begins with `[TALK]` AND signals *new
     activity* — it contains `wants to talk` (a pending invite) or
     `new message` (queued messages). Do NOT pull on the bare connected form
     (`[TALK] connected to …`): that marks an already-open session with
     nothing new to read. The base description (no `[TALK]`) starts with
     "Start a real-time conversation". This check carries the same
     staleness risk as the old mail check did — it is kept only because
     there is no better alternative for talk (see below) — OR
   - Line 2 of the step 1 output is a talk item: `▶ <text>` in bold yellow
     (distinct from a wall item's bold red). This is a best-effort peek, not
     authoritative — the display queue shows a talk item once for a single
     ~15s rotation turn and then drops it even if it was never read, so its
     *absence* does not mean there is nothing to read, only that neither this
     check nor a prior one caught it in the window. There is no persistent
     talk-unread count in the statusline output the way there is for mail
     (`SessionUnread.count` in `src/biff/unread.py` is mail-only), which is
     why talk cannot fully adopt mail's ground-truth gate.
   - Call `mcp__plugin_biff_tty__talk_read`.
   - If it reports a pending invite (a line with "wants to talk"), tell the user
     who wants to talk and that `/biff:talk <user>:<tty>` accepts it — use the
     session-scoped `<user>:<tty>` address `talk_read` prints (talk is
     session-scoped, so a bare `<user>` can fail to resolve).
   - If it returns talk messages, surface them.
   - Emit the tool output verbatim — no reformatting, code fences, tables, or
     boxes.
3. **Mail** — pull if `N > 0`, OR step 1's biff segment showed the literal
   `(n)` mesg-off shape (count unknown, possibly nonzero — pull rather than
   silently withholding mail the way the nudge hook never does):
   - Call `mcp__plugin_biff_tty__read_messages`. The tool retries a transport
     error once internally, per inbox, and never raises for one.
   - If the result starts with "Could not check ", surface it plainly — this
     is the automated path, so a silently-swallowed failure here persists the
     longest (biff-brn). It can appear standalone or as a leading line before
     rendered messages (one inbox failed, another succeeded) — either way, do
     not treat it as, or report it like, an empty inbox, and do not drop it
     to make the output look clean if messages follow it.
   - Otherwise emit the result exactly as returned — character for character,
     including the leading ▶ unicode character. Do not reformat, add commentary,
     wrap in code fences, convert to markdown tables, or add boxes.
4. If neither signal fires, emit nothing and call nothing.

Mail's gate is the parenthesized integer `biff statusline` prints — the exact
count the biff server's `_write_unread_file` (`src/biff/server/tools/
_descriptions.py`) writes to the per-session file on every change, not a
string embedded in a tool description — except when mesg is off, where the
segment prints the literal `(n)` in place of the count and the gate falls
back to pulling unconditionally rather than treating that as zero. Talk's
gate is the marker `[TALK]`
plus `wants to talk` / `new message`, the exact strings the biff server
writes into the live tool description (`_descriptions._talk_description`),
supplemented by the bold-yellow `▶ <text>` talk item on statusline line 2. If
any of these — the description markers, the `(N)` shape, or the bold-yellow
(talk) vs. bold-red (wall) line-2 color convention — change, this command
must change with them.

### D. `/biff:read status` — show polling config

1. Call `mcp__plugin_biff_tty__get_poll_status`.
2. Report the returned values: interval, active, last check time.

### Managing the cron job

To delete existing auto-poll cron jobs:

1. Call `CronList`.
2. For every line whose prompt suffix is exactly `: /biff:read` OR exactly
   `: /biff:poll` — that is, the text after the final colon-space separator is
   the literal string `/biff:read` or the legacy string `/biff:poll`, with no
   trailing space and no argument — extract the job ID (the first
   whitespace-separated token on the line). `/biff:poll` predates #414, which
   folded polling into `/biff:read`; a durable cron job created before that
   merge still fires the now-deleted `/biff:poll` command, so it must be swept
   alongside `/biff:read` jobs or it strands as a dead loop.
3. Call `CronDelete` for each matching ID.

Lines whose prompt is `/biff:read <something>` or `/biff:poll <something>` (an
argument-bearing invocation, not a bare auto-poll job) must not be deleted.
Match on the exact `: /biff:read` or `: /biff:poll` suffix only.
