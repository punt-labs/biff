---
description: Name the current session (visible in /who and /finger)
argument-hint: "<name>"
---

## Input

Arguments: $ARGUMENTS

## Task

Call `mcp__biff__tty` with `name` set to the full arguments string. The result is already formatted by a PostToolUse hook and displayed above. Do not repeat or reformat the data. Do not send any text after the tool call.
