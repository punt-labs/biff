---
description: Check your inbox for new messages
allowed-tools: ["ToolSearch", "mcp__plugin_biff_tty__read_messages"]
---
<!-- markdownlint-disable MD041 -->

Call `mcp__plugin_biff_tty__read_messages` with no arguments. The tool retries a transport error once internally and, on a persistent failure, returns a result starting with "Could not check mail" instead of raising — so every outcome below is a normal return value, never an error to catch.

If the result says "No new messages.", do not emit any text — this is a confirmed-empty result.

If the result starts with "Could not check mail", surface it plainly — this is a failure, not an empty inbox, and must not be treated as or reported like one.

Otherwise, emit the tool output exactly as returned — character for character, including the leading ▶ unicode character. Do not reformat, add commentary, wrap in code fences, convert to markdown tables, or add boxes around the output.
