---
trigger: always_on
---

# Check team mail

While working, check biff mail at least every five minutes using
`mcp_call_tool` with server `biff`, tool `read_messages`, and empty arguments,
or run `biff read` from inside a git repository. Act on new messages: reply
with `write` and carry out requests that are in scope. This polling exists
until push delivery replaces it.

The org admin must bind `MCP_BIFF_RELAY_TOKEN` on this plugin's MCP
installation. The launcher maps that placeholder to the relay token,
discards empty or unexpanded values, supplies a startup session ID, and
requires biff 1.24.0 or newer (using uv when the installed binary is older).
Explicit `biff mcp --user devin` binds unstamped calls to the same fixed
startup session as presence.
Never print or persist the token.
