---
description: "[DEV] Start a real-time conversation with a teammate or agent"
argument-hint: "@user [message]"
---
<!-- markdownlint-disable MD041 -->

## Input

Arguments: $ARGUMENTS

Parse as: first token is the recipient username (strip leading `@` if present), remaining tokens are the opening message (optional).

Examples:

- `@kai` → `to="kai"`
- `@kai hey, got a minute?` → `to="kai"`, `message="hey, got a minute?"`

## Task

1. Call `mcp__plugin_biff_dev_tty__talk` with the parsed values.
2. Incoming messages from the partner appear on the status bar automatically (0-2s). No need to poll or call talk_listen.
3. When the user wants to reply, send with `mcp__plugin_biff_dev_tty__write` to the same user.
4. When the user says to stop, call `mcp__plugin_biff_dev_tty__talk_end`.

If `$ARGUMENTS` is "end", call `mcp__plugin_biff_dev_tty__talk_end` directly.

Do not repeat or reformat tool output — it is already formatted by hooks.
