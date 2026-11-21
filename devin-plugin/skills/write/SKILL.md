---
name: write
description: Send a message to a teammate.
---

# Write

Parse arguments as a recipient followed by the message body. Strip a
leading `@` from the recipient. For example, `@kai ready for review?`
becomes `to="kai"`, `message="ready for review?"`. If either is missing,
ask for the missing value rather than sending an empty message.

Call `mcp_call_tool` with server `biff`, tool `write`, and arguments `to`
and `message`. Return its output without reformatting.
