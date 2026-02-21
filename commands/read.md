---
description: Check your inbox for new messages
---
<!-- markdownlint-disable MD041 -->

Call `mcp__plugin_biff_biff__read_messages` with no arguments.

If the result says "No new messages.", do not emit any text.

Otherwise, emit the tool output exactly as returned — character for character, including the leading ▶ unicode character. Do not reformat, add commentary, wrap in code fences, convert to markdown tables, or add boxes around the output.
