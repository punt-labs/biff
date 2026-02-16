---
description: Control message reception (on/off)
argument-hint: "on|off"
---

## Input

Arguments: $ARGUMENTS

## Task

If the argument is `on`, call `mcp__biff__biff` with `enabled` set to `true` and confirm messages are now enabled.

If the argument is `off`, call `mcp__biff__biff` with `enabled` set to `false` and confirm messages are now disabled.

If no argument or an unrecognized argument is provided, respond with: `Usage: /biff on|off`

Do not send any other text besides the tool call and confirmation (or usage message).
