---
description: "Check your inbox for new messages"
argument-hint: "[duration | n | status | force]"
allowed-tools: ["ToolSearch", "mcp__plugin_biff-dev_tty__talk_read", "mcp__plugin_biff-dev_tty__read_messages", "mcp__plugin_biff-dev_tty__set_poll_interval", "mcp__plugin_biff-dev_tty__get_poll_status", "CronCreate", "CronList", "CronDelete"]
---
<!-- markdownlint-disable MD041 -->

## Input

Arguments: $ARGUMENTS

## Task

biff is passive/pull. Incoming talk (invites + real-time messages) and /write
mail are held on the biff server and surface by MUTATING the descriptions of the
`talk` and `read_messages` tools. This command manages polling and checks for
new activity.

### Argument routing

First, check if `$ARGUMENTS` matches a **polling config** command:

- A duration like `1m`, `2m`, `5m`, `10m`, `15m`, `30m`, `1h`, `2h` → start
  polling (section A)
- Exactly `n`, `off`, or `stop` → disable polling (section B)
- Exactly `status` → show polling config (section D)
- Exactly `force` → force-check now (section C, unconditional)

If none of the above match (empty or unrecognized), fall through to **check
now** (section C).

### A. `/biff-dev:read-dev <duration>` — start polling every `<duration>`

1. Call `mcp__plugin_biff-dev_tty__set_poll_interval` with `interval` set to
   `<duration>` — this sets the server-side poll cadence.
2. Delete every existing `/biff-dev:read-dev` auto-poll cron job (see "Managing
   the cron job" below). This must happen **before** creating the new job, so two
   back-to-back `/biff-dev:read-dev 10m` calls leave exactly one cron job, not
   two.
3. Create a new cron job:

   ```text
   CronCreate(
     cron: "<expression>",   // from the table below
     recurring: true,
     durable: true,          // persists across session restarts
     prompt: "/biff-dev:read-dev"
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

### B. `/biff-dev:read-dev n` — stop polling

Trigger when `$ARGUMENTS` is `n`, `off`, or `stop`.

1. Call `mcp__plugin_biff-dev_tty__set_poll_interval` with `interval` set to
   `n` — this disables the server-side poll cadence.
2. Delete every existing `/biff-dev:read-dev` auto-poll cron job (see "Managing
   the cron job" below).
3. Confirm in one line: polling disabled and the loop job removed. Do NOT fall
   through to a check.

### C. `/biff-dev:read-dev` (no argument) — check now

Also the path when `$ARGUMENTS` is empty or `force`. Inspect the two live tool
descriptions (the biff server mutates them and fires tools/list_changed when
activity arrives) and pull ONLY when a marker is present — `talk_read` and
`read_messages` mark-read/consume and can be slow, so do not call them blindly.
If `$ARGUMENTS` is `force`, run both pulls unconditionally.

1. Look at your own current `talk` and `read_messages` tool descriptions. Do not
   call any tool for this step.
2. **Talk** — pull only if the `talk` description begins with `[TALK]` AND
   signals *new activity* — it contains `wants to talk` (a pending invite) or
   `new message` (queued messages). Do NOT pull on the bare connected form
   (`[TALK] connected to …`): that marks an already-open session with nothing
   new to read, so calling `talk_read` every tick just churns. The base
   description (no `[TALK]`) starts with "Start a real-time conversation".
   - Call `mcp__plugin_biff-dev_tty__talk_read`.
   - If it reports a pending invite (a line with "wants to talk"), tell the user
     who wants to talk and that `/biff-dev:talk-dev <user>:<tty>` accepts it —
     use the session-scoped `<user>:<tty>` address `talk_read` prints (talk is
     session-scoped, so a bare `<user>` can fail to resolve).
   - If it returns talk messages, surface them.
   - Emit the tool output verbatim — no reformatting, code fences, tables, or
     boxes.
3. **Mail** — pull only if the `read_messages` description contains the marker
   `unread)` (the "(N unread)" form emitted by refresh\_read\_messages; the base
   is "Check your inbox for new messages. Marks all as read." with no marker):
   - Call `mcp__plugin_biff-dev_tty__read_messages`. The tool retries a transport
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
4. If neither description carries its marker, emit nothing and call nothing.

The markers `[TALK]` and `unread)` are the exact strings the biff server writes
into those descriptions (server tools `_descriptions._talk_description` and
`refresh_read_messages`). If they change, this command must change with them.

### D. `/biff-dev:read-dev status` — show polling config

1. Call `mcp__plugin_biff-dev_tty__get_poll_status`.
2. Report the returned values: interval, active, last check time.

### Managing the cron job

To delete existing auto-poll cron jobs:

1. Call `CronList`.
2. For every line whose prompt suffix is exactly `: /biff-dev:read-dev` — that
   is, the text after the final colon-space separator is the literal string
   `/biff-dev:read-dev`, with no trailing space and no argument — extract the job
   ID (the first whitespace-separated token on the line).
3. Call `CronDelete` for each matching ID.

Lines whose prompt is `/biff-dev:read-dev <something>` (an argument-bearing
invocation, not the bare auto-poll job) must not be deleted. Match on the exact
`: /biff-dev:read-dev` suffix only.
