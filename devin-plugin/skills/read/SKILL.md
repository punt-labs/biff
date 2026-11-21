---
name: read
description: Check your inbox for new messages.
---

# Read

Use `mcp_call_tool` with server `biff`, routing arguments as follows:

- A duration such as `1m`, `5m`, or `1h`: call `set_poll_interval` with
  `interval` set to that duration, then check mail now.
- Exactly `n`, `off`, or `stop`: call `set_poll_interval` with `interval="n"`
  and report the result without checking mail.
- Exactly `status`: call `get_poll_status` with empty arguments and report
  the returned configuration.
- Empty, `force`, or unrecognized arguments: check mail now.

To check mail now, call `read_messages` with empty arguments. Return the
tool output verbatim; surface errors rather than treating them as an empty
inbox. Act on new messages in scope and reply with `write` when appropriate.
The always-on mail rule covers periodic checks; do not create cron jobs.
