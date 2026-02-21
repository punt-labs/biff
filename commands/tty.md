---
description: Name the current session (visible in /who and /finger)
argument-hint: "[name]"
---
<!-- markdownlint-disable MD041 -->

## Input

Arguments: $ARGUMENTS

## Task

Call `mcp__plugin_biff_biff__tty`. If arguments were provided, pass them as `name`. If no arguments, call with no arguments to auto-assign the next ttyN. The result is already formatted by a PostToolUse hook and displayed above. Do not repeat or reformat the data. Do not send any text after the tool call.
