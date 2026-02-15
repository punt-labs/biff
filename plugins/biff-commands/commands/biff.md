---
description: Control message reception (on/off)
argument-hint: "on|off"
---

## Input

Arguments: $ARGUMENTS

Parse as:
- `on` — set `enabled` to `true`
- `off` — set `enabled` to `false`
- (no argument) — default to `true`

## Task

Call `mcp__biff__biff` with `enabled` set to the parsed boolean value. Confirm the new state. Do not send any other text besides the tool call and confirmation.
