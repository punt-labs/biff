---
description: Poll for talk/mail — "/biff-dev:poll-dev 5m" starts polling every 5m; "/biff-dev:poll-dev" checks now
argument-hint: "[duration | n | force]"
allowed-tools: ["ToolSearch", "mcp__plugin_biff-dev_tty__talk_read", "mcp__plugin_biff-dev_tty__read_messages", "mcp__plugin_biff-dev_tty__set_poll_interval", "CronCreate", "CronList", "CronDelete"]
---
<!-- markdownlint-disable MD041 -->

## Input

Arguments: $ARGUMENTS

## Task

biff is passive/pull. Incoming talk (invites + real-time messages) and /write
mail are held on the biff server and surface by MUTATING the descriptions of the
`talk` and `read_messages` tools. One command, three forms:

### A. `/biff-dev:poll-dev <duration>` — start polling every `<duration>`

Trigger when `$ARGUMENTS` is a duration like `1m`, `5m`, `10m` (parse the
`{N}m`/`{N}s` interval the way `/loop` does).

1. Call `mcp__plugin_biff-dev_tty__set_poll_interval` with `interval` set to
   `<duration>` — this sets the server-side poll cadence.
2. Schedule the recurring model check with a `/loop` job at the same cadence:
   - Call `CronList`; if any job's `prompt` field exactly matches
     `/biff-dev:poll-dev`, call `CronDelete` on it first (avoid duplicate loops).
   - Call `CronCreate` with:
     - `cron`: the interval as a cron expression (`1m` → `*/1 * * * *`,
       `2m` → `*/2 * * * *`, `5m` → `*/5 * * * *`, `10m` → `*/10 * * * *`;
       sub-minute intervals use `*/1 * * * *`, the 1-minute cron floor).
     - `prompt`: `/biff-dev:poll-dev` — the recurring job runs
       `/biff-dev:poll-dev` with NO argument (the no-arg check below). Do NOT put
       the duration in the loop body, or every fire would reschedule itself.
     - `recurring`: true
     - `durable`: true
3. Confirm what was set in one line: the poll interval, the loop job id from
   `CronCreate`, and the 7-day durable-loop expiry.
4. Then fall through and run one check now (section C).

### B. `/biff-dev:poll-dev n` — stop polling

Trigger when `$ARGUMENTS` is `n`, `off`, or `stop`. Turn the server-side cadence
off and remove the recurring model check.

1. Call `mcp__plugin_biff-dev_tty__set_poll_interval` with `interval` set to `n`
   — this disables the server-side poll cadence.
2. Remove the recurring `/loop` job: call `CronList`; for any job whose `prompt`
   field exactly matches `/biff-dev:poll-dev`, call `CronDelete` on it.
3. Confirm in one line: polling disabled and the loop job removed. Do NOT fall
   through to a check.

### C. `/biff-dev:poll-dev` (no duration) — check now

Also the path when `$ARGUMENTS` is empty or `force`. Inspect the two live tool
descriptions (the biff server mutates them and fires tools/list_changed when
activity arrives) and pull ONLY when a marker is present — `talk_read` and
`read_messages` mark-read/consume and can be slow, so do not call them blindly.
If `$ARGUMENTS` is `force`, run both pulls unconditionally.

1. Look at your own current `talk` and `read_messages` tool descriptions. Do not
   call any tool for this step.
2. **Talk** — pull only if the `talk` description begins with the marker
   `[TALK]` (emitted by refresh_talk; the base starts with "Start a real-time
   conversation" and carries no marker):
   - Call `mcp__plugin_biff-dev_tty__talk_read`.
   - If it reports a pending invite (a line with "wants to talk"), tell the user
     who wants to talk and that `/biff-dev:talk-dev @<user>:<tty>` accepts it —
     use the session-scoped `@<user>:<tty>` address `talk_read` prints (talk is
     session-scoped, so a bare `@<user>` can fail to resolve).
   - If it returns talk messages, surface them.
   - Emit the tool output verbatim — no reformatting, code fences, tables, or
     boxes.
3. **Mail** — pull only if the `read_messages` description contains the marker
   `unread)` (the "(N unread)" form emitted by refresh_read_messages; the base
   is "Check your inbox for new messages. Marks all as read." with no marker):
   - Call `mcp__plugin_biff-dev_tty__read_messages`.
   - Emit the tool output exactly as returned — character for character,
     including the leading ▶ unicode character. Do not reformat, add commentary,
     wrap in code fences, convert to markdown tables, or add boxes (same rule as
     /biff-dev:read-dev).
4. If neither description carries its marker, emit nothing and call nothing.

The markers `[TALK]` and `unread)` are the exact strings the biff server writes
into those descriptions (server tools `_descriptions._talk_description` and
`refresh_read_messages`). If they change, this command must change with them.
