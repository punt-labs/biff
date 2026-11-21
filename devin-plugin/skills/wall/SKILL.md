---
name: wall
description: Broadcast a message to all teammates or view the current wall.
---

# Wall

Call `mcp_call_tool` with server `biff`, tool `wall`, parsing arguments as:

- No arguments: empty arguments to show the current wall.
- Exactly `clear`: `clear=true`.
- A trailing duration such as `30m`, `2h`, or `1d`: set `duration` to that
  token and `message` to everything before it.
- Otherwise: set `message` to the entire argument string.

Return the tool output without reformatting.
