---
description: Send a message to a teammate
argument-hint: "@user <message>"
---

## Input

Arguments: $ARGUMENTS

Parse as: first token is the recipient username (strip leading `@` if present), remaining tokens are the message body.

Example: `@kai hey, ready for code review?` → `to="kai"`, `message="hey, ready for code review?"`

## Task

Call `mcp__biff__write` with `to` and `message` set to the parsed values. The result is already formatted by a PostToolUse hook and displayed above. Do not repeat or reformat the data. Do not send any text after the tool call.
