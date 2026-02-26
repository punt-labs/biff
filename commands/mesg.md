---
description: Control message reception
argument-hint: "y | n"
---
<!-- markdownlint-disable MD041 -->

## Input

Arguments: $ARGUMENTS

Parse the argument as `y` or `n`. Map `y` to `enabled=true`, `n` to `enabled=false`.

## Task

Call `mcp__plugin_biff_tty__mesg` with `enabled` set to the parsed value. The result is already formatted by a PostToolUse hook and displayed above. Do not repeat or reformat the data. Do not send any text after the tool call.
