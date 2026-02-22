---
description: Show session login/logout history
argument-hint: "[@user] [count]"
---
<!-- markdownlint-disable MD041 -->

Call `mcp__plugin_biff_tty__last` with optional `user` (e.g. `@kai`) and `count` (default 25) arguments.

If the result says "No session history.", say "No session history available."

Otherwise, emit the tool output exactly as returned — character for character, including the leading ▶ unicode character. Do not reformat, add commentary, wrap in code fences, convert to markdown tables, or add boxes around the output.
