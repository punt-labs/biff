---
description: Check your inbox for new messages
allowed-tools: ["ToolSearch", "mcp__plugin_biff_tty__read_messages"]
---
<!-- markdownlint-disable MD041 -->

Call `mcp__plugin_biff_tty__read_messages` with no arguments. The tool retries a transport error once internally, per inbox, and never raises for a transport failure — every outcome below is a normal return value to check, never an error to catch.

If the result says "No new messages.", do not emit any text — this is a confirmed-empty result.

If the result starts with "Could not check ", surface it plainly and do not treat it as, or report it like, an empty inbox. This can appear standalone (every inbox failed) or as a leading line before rendered messages (some inboxes succeeded and are shown below it, one specific inbox could not be checked) — in the combined case, do not drop the warning line to make the output look clean; both facts are real.

Otherwise, emit the tool output exactly as returned — character for character, including the leading ▶ unicode character. Do not reformat, add commentary, wrap in code fences, convert to markdown tables, or add boxes around the output.
