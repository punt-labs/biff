---
description: "[DEV] Enable or disable biff for this repo"
argument-hint: "y | n"
---
<!-- markdownlint-disable MD041 -->

## Input

Arguments: $ARGUMENTS

Parse the argument as `y` or `n`. Map `y` to `enabled=true`, `n` to `enabled=false`.

## Task

Call `mcp__plugin_biff_dev_tty__biff` with `enabled` set to the parsed value. The result is already formatted by a PostToolUse hook and displayed above. Do not repeat or reformat the data. Do not send any text after the tool call.
