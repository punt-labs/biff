---
description: Control message reception
argument-hint: "y | n"
---

## Input

Arguments: $ARGUMENTS

Parse the argument as `y` or `n`. Map `y` to `enabled=true`, `n` to `enabled=false`.

## Task

Call `mcp__biff__mesg` with `enabled` set to the parsed value. The result is already formatted by a PostToolUse hook and displayed above. Do not repeat or reformat the data. Do not send any text after the tool call.
