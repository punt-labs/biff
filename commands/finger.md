---
description: Check what a teammate is working on and their availability
argument-hint: "@user"
---
<!-- markdownlint-disable MD041 -->

## Input

Arguments: $ARGUMENTS

Parse the username from arguments, stripping any leading `@` if present.

## Task

Call `mcp__plugin_biff_biff__finger` with `user` set to the parsed username. Emit the tool output exactly as returned — character for character, including the leading ▶ unicode character. Do not reformat, add commentary, wrap in code fences, convert to markdown tables, or add boxes around the output.
