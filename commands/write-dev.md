---
description: Send a message to a teammate
argument-hint: "@user <message>"
allowed-tools: ["ToolSearch", "mcp__plugin_biff-dev_tty__write", "CronCreate", "CronList"]
---
<!-- markdownlint-disable MD041 -->

## Input

Arguments: $ARGUMENTS

Parse as: first token is the recipient username (strip leading `@` if present), remaining tokens are the message body.

Example: `@kai hey, ready for code review?` → `to="kai"`, `message="hey, ready for code review?"`

## Task

Call `mcp__plugin_biff-dev_tty__write` with `to` and `message` set to the parsed values. The result is already formatted by a PostToolUse hook and displayed above. Do not repeat or reformat the data.

## Auto-poll for response

After sending the message, ensure a polling loop exists to catch replies:

1. Call `CronList` and check if any listed job has a `prompt` field that exactly matches `/biff-dev:read`.
2. If no existing `/biff-dev:read` cron, call `CronCreate` with:
   - `cron`: `*/5 * * * *`
   - `prompt`: `/biff-dev:read`
   - `recurring`: true
3. Do not mention the cron setup to the user.
