---
description: Control message reception (on/off)
argument-hint: "on|off"
---

## Input

Arguments: $ARGUMENTS

Parse as:
- `on` — set `enabled` to `true`
- `off` — set `enabled` to `false`
- any other or missing argument — do not call the tool; respond with: `Usage: /biff on|off`

## Task

If the argument is `on` or `off`, call `mcp__biff__biff` with `enabled` set to the parsed boolean value. Confirm the new state. For any other or missing argument, show the usage message. Do not send any other text.
