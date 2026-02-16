---
description: Check what a teammate is working on and their availability
argument-hint: "@user"
---

## Input

Arguments: $ARGUMENTS

Parse the username from arguments, stripping any leading `@` if present.

## Task

Call `mcp__biff__finger` with `user` set to the parsed username. The result is already formatted by a PostToolUse hook and displayed above. Do not repeat or reformat the data. Do not send any text after the tool call.
