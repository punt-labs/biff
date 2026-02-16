---
description: Check what a teammate is working on and their availability
argument-hint: "@user"
---

## Input

Arguments: $ARGUMENTS

Parse the username from arguments, stripping any leading `@` if present.

## Task

Call `mcp__biff__finger` with `user` set to the parsed username.

If the result contains "Never logged in.", do not emit any text.

Otherwise, emit the full result in a fenced code block. Do not add any text before or after the code block.
