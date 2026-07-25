---
description: Enable or disable biff for this repo
argument-hint: "enable | disable"
allowed-tools: ["ToolSearch", "mcp__plugin_biff-dev_tty__biff"]
---
<!-- markdownlint-disable MD041 -->

## Input

Arguments: $ARGUMENTS

Parse the argument as `enable` or `disable`.

## Task

Call `mcp__plugin_biff-dev_tty__biff` with `action` set to the parsed value (`"enable"` or `"disable"`). The result is already formatted by a PostToolUse hook and displayed above. Do not repeat or reformat the data. Do not send any text after the tool call.
