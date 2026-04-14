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

1. Call `CronList` to check if any job with `/biff:read` in its prompt already exists.
2. If no existing `/biff:read` cron, call `CronCreate` with:
   - `cron`: `*/5 * * * *`
   - `prompt`: `/biff:read`
   - `recurring`: true
3. Do not mention the cron setup to the user.
