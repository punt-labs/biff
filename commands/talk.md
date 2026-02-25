---
description: Start a real-time conversation with a teammate or agent
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

1. Call `mcp__plugin_biff-dev_tty__talk` with the parsed values.
2. If the talk tool succeeds, immediately call `mcp__plugin_biff-dev_tty__talk_listen` to wait for a reply.
3. When a reply arrives, show it and ask what to reply. Send replies with `mcp__plugin_biff-dev_tty__write` to the same user, then call `mcp__plugin_biff-dev_tty__talk_listen` again.
4. Continue the listen → reply loop until the user says to stop, then call `mcp__plugin_biff-dev_tty__talk_end`.

Do not repeat or reformat tool output — it is already formatted by hooks.
