---
description: Check what a teammate is working on and their availability
argument-hint: "@user"
---

## Input

Arguments: $ARGUMENTS

Parse the username from arguments, stripping any leading `@` if present.

## Task

Call `mcp__biff__finger` with `user` set to the parsed username. Display the user's status, plan, and availability. Do not send any other text besides the tool call and the formatted results.
