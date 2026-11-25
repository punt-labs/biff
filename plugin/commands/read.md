---
description: "Check your inbox for new messages"
argument-hint: "[duration | n | status | force]"
allowed-tools: ["ToolSearch", "mcp__plugin_biff_tty__talk_read", "mcp__plugin_biff_tty__read_messages", "mcp__plugin_biff_tty__set_poll_interval", "mcp__plugin_biff_tty__get_poll_status", "CronCreate", "CronList", "CronDelete"]
---
<!-- markdownlint-disable MD041 -->

## Input

Arguments: $ARGUMENTS

## Task

Channel events tell the agent when mail, a wall or talk arrives, in sessions
where channels are live. This command is the fallback where they are not: it
manages polling and checks for new activity on demand.

**Polling.** `/biff:read <duration>` turns polling on: it records the cadence and
schedules a cron that runs `/biff:read`. While polling is on, the
`unread-nudge` hook also tells the model when the unread count rises; it stays
quiet otherwise. `/biff:read n` turns both off. Bare `/biff:read` checks now in any
session.

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

Also the path when `$ARGUMENTS` is empty or `force`. Pull directly: both tools
are cheap and answer plainly when there is nothing new. The status line is the
user's display; this command never reads it.

1. **Talk** — call `mcp__plugin_biff_tty__talk_read`.
   - If it reports a pending invite (a line with "wants to talk"), tell the user
     who wants to talk and that `/biff:talk <user>:<tty>` accepts it — use the
     session-scoped `<user>:<tty>` address `talk_read` prints (talk is
     session-scoped, so a bare `<user>` can fail to resolve).
   - If it returns talk messages, surface them.
   - If it says there is nothing new, emit nothing for talk.
2. **Mail** — call `mcp__plugin_biff_tty__read_messages`. The tool retries a transport
   error once internally, per inbox, and never raises for one.
   - If the result starts with "Could not check ", surface it plainly — this
     is the automated path, so a silently-swallowed failure here persists the
     longest. It can appear standalone or as a leading line before rendered
     messages (one inbox failed, another succeeded) — either way, do not treat
     it as, or report it like, an empty inbox, and do not drop it to make the
     output look clean if messages follow it.
   - If the result is "No new messages.", emit nothing for mail.
   - Otherwise emit the result exactly as returned — character for character,
     including the leading ▶ unicode character. Do not reformat, add
     commentary, wrap in code fences, convert to markdown tables, or add boxes.
3. If both report nothing new, emit nothing.

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
