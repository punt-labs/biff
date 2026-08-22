---
description: Set your BSD .plan status — a one-line, ~40-char note (not a task plan), visible via /finger and /who
argument-hint: "<message>"
allowed-tools: ["ToolSearch", "mcp__plugin_biff-dev_tty__plan"]
---
<!-- markdownlint-disable MD041 -->

## Input

Arguments: $ARGUMENTS

## Task

Call `mcp__plugin_biff-dev_tty__plan` with `message` set to the full arguments string. The result is already formatted by a PostToolUse hook and displayed above. Do not repeat or reformat the data. Do not send any text after the tool call.
