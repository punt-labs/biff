---
description: Check your inbox for new messages
allowed-tools: ["ToolSearch", "mcp__plugin_biff_tty__read_messages"]
---
<!-- markdownlint-disable MD041 -->

Call `mcp__plugin_biff_tty__read_messages` with no arguments.

If the call raises a transport error (e.g. "nats: timeout"), retry once, immediately, with no other action in between. This mirrors the observed recovery pattern (biff-brn: every session-reported occurrence cleared on the very next attempt) and is not a general-purpose reliability fix for the underlying connection issue — it exists only so a caller distinguishes "confirmed empty" from "could not determine" instead of silently treating them as identical.

If the retry also fails, do not report "No new messages." and do not stay silent. Report plainly that the mailbox could not be checked, name the underlying error, and state that inbox state is unknown — not confirmed empty. Swallowing this into silence recreates the exact defect this retry exists to expose: an unread mailbox believed to be caught up.

If either attempt returns "No new messages.", do not emit any text — this is a confirmed-empty result, not a failure.

Otherwise, emit the tool output exactly as returned — character for character, including the leading ▶ unicode character. Do not reformat, add commentary, wrap in code fences, convert to markdown tables, or add boxes around the output.
