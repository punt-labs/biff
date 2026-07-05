---
description: Check for talk/mail activity signalled by tool descriptions; pull only if flagged
argument-hint: "[force]"
allowed-tools: ["ToolSearch", "mcp__plugin_biff-dev_tty__talk_read", "mcp__plugin_biff-dev_tty__read_messages"]
---
<!-- markdownlint-disable MD041 -->

## Input

Arguments: $ARGUMENTS

## Task

biff is passive/pull. Incoming talk (invites + real-time messages) and /write
mail are held on the biff server and surface by MUTATING the descriptions of the
`talk` and `read_messages` tools. This command inspects those two descriptions
and pulls ONLY when one signals activity — `talk_read` and `read_messages`
mark-read/consume and can be slow, so do not call them blindly.

If `$ARGUMENTS` contains `force`, skip the description checks and run BOTH pulls
(steps 2 and 3) unconditionally — a manual full sweep.

1. Look at your own current tool descriptions for `talk` and `read_messages`
   (the live descriptions; the biff server mutates them and fires
   tools/list_changed when activity arrives). Do not call any tool for step 1.

2. **Talk** — pull only if the `talk` tool description begins with the marker
   `[TALK]` (emitted by refresh_talk; the base description starts with "Start a
   real-time conversation" and carries no marker):
   - Call `mcp__plugin_biff-dev_tty__talk_read`.
   - If it reports a pending invite (a line with "wants to talk"), tell the user
     who wants to talk and that `/biff-dev:talk-dev @<user>` accepts it.
   - If it returns talk messages, surface them.
   - Emit the tool output verbatim — no reformatting, code fences, tables, or
     boxes.

3. **Mail** — pull only if the `read_messages` tool description contains the
   marker `unread)` (the "(N unread)" form emitted by refresh_read_messages; the
   base description is "Check your inbox for new messages. Marks all as read."
   and carries no marker):
   - Call `mcp__plugin_biff-dev_tty__read_messages`.
   - Emit the tool output exactly as returned — character for character,
     including the leading ▶ unicode character. Do not reformat, add commentary,
     wrap in code fences, convert to markdown tables, or add boxes (same rule as
     /biff-dev:read-dev).

4. If NEITHER description carries its marker, emit nothing and call nothing.

The markers `[TALK]` and `unread)` are the exact strings the biff server writes
into those descriptions (server tools `_descriptions._talk_description` and
`refresh_read_messages`). If they change, this command must change with them.
