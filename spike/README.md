# Spike: Validate tools/list_changed in Claude Code

## What This Tests

Can an MCP server dynamically update tool descriptions and have Claude Code
see the changes? This tests the core notification mechanism for biff's
message awareness feature.

## Architecture Being Validated

```
Message arrives → Server updates tool description
    → FastMCP sends notifications/tools/list_changed
    → Claude Code refreshes tool list
    → Claude sees "check_messages (3 unread: @kai about auth)"
```

## How to Run

### 1. Start the spike server

```bash
uv run python spike/list_changed_server.py
```

### 2. Register with Claude Code (in another terminal)

```bash
claude mcp add --transport http biff-spike http://localhost:8419/mcp
```

### 3. Test in Claude Code

1. Start a Claude Code session
2. Ask Claude to call `simulate_message` with from_user="kai" and body="auth is ready"
3. **Critical check**: Does the `check_messages` tool description now show
   "Check messages (1 unread: @kai about auth is ready)"?
4. Ask Claude what tools are available — does it see the updated description?
5. Call `check_messages` to read the message

### 4. Clean up

```bash
claude mcp remove biff-spike
```

## Results (2026-02-13)

### Server-side validation (via curl)

All server-side mechanisms confirmed working:

| Test | Result | Evidence |
|------|--------|---------|
| Server starts with HTTP transport | **YES** | FastMCP 2.14.5, Streamable HTTP on port 8419 |
| `simulate_message` updates tool description | **YES** | Description changed to "Check messages (1 unread: @kai about auth is ready)" |
| `tools/list` returns updated description | **YES** | Before/after comparison shows description change |
| `notifications/tools/list_changed` sent via SSE | **YES** | Captured on SSE stream: `{"method":"notifications/tools/list_changed"}` |
| Multiple messages accumulate | **YES** | Second message produced "2 unread" description |

### Key technical details

- FastMCP's `remove_tool` + re-register pattern works correctly
- The `notifications/tools/list_changed` notification is emitted automatically on the SSE channel
- Streamable HTTP uses POST for RPC calls, GET SSE stream for server-push notifications
- Server advertises `"tools": {"listChanged": true}` in capabilities

### Remaining validation

Claude Code end-to-end test (registering biff-spike as an MCP server in a new
Claude Code session) not yet performed — requires session restart. Server-side
protocol behavior is fully validated.

## Exit Criteria

- **YES**: Server-side mechanism works → proceed with HTTP transport architecture
- Remaining: Confirm Claude Code client acts on `list_changed` (needs new session)
