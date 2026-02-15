---
description: Send a message to a teammate
argument-hint: "@user <message>"
---

## Input

Arguments: $ARGUMENTS

Parse as: first token is the recipient username (strip leading `@` if present), remaining tokens are the message body.

Example: `@kai hey, ready for code review?` → `to="kai"`, `message="hey, ready for code review?"`

## Task

Call `mcp__biff__send_message` with `to` and `message` set to the parsed values. Confirm the message was sent. Do not send any other text besides the tool call and confirmation.
