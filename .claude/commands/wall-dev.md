---
description: "[DEV] Broadcast a message to all teammates"
argument-hint: '"message" [duration] | clear'
---
<!-- markdownlint-disable MD041 -->

## Input

Arguments: $ARGUMENTS

Parse as one of three modes:

1. **Clear**: if arguments are exactly `clear` → call with `clear=True`
2. **Post with duration**: if the last token matches a duration pattern (`30m`, `2h`, `1d`, `3d`), it is the duration and everything before it is the message → `message="...", duration="2h"`
3. **Post with default**: otherwise the entire argument string is the message → `message="..."`

If no arguments are provided, call with no parameters (shows current wall).

Examples:

- `release freeze — do not push to main` → `message="release freeze — do not push to main"`
- `deploy window open 2h` → `message="deploy window open"`, `duration="2h"`
- `clear` → `clear=true`
- (empty) → no parameters

## Task

Call `mcp__plugin_biff_dev_tty__wall` with the parsed values. The result is already formatted by a PostToolUse hook and displayed above. Do not repeat or reformat the data. Do not send any text after the tool call.
