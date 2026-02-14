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

### Claude Code end-to-end validation (2026-02-13)

Registered biff-spike in `.mcp.json` with `"type": "http"`, restarted Claude Code.

| Test | Result | Evidence |
|------|--------|---------|
| Claude Code connects to HTTP MCP server | **YES** | Tools visible in session |
| `simulate_message` triggers description update | **YES** | Description changed to "Check messages (1 unread: @jess about tests passing)" |
| Claude (model) sees updated description | **YES** | Model can read updated tool description in its tool definitions |
| User sees status bar notification | **NO** | No user-visible indicator — description is model-only |
| `check_messages` returns accumulated messages | **YES** | Returns unread messages correctly |

### UX constraint discovered

The dynamic tool description is visible to the **model** but not to the **user** in the
Claude Code UI. There is no status bar badge, no pop-up, no visual notification.
Message awareness is model-mediated: Claude knows about messages and can mention them
proactively, but the user relies on Claude to surface this information.

## Exit Criteria

- **YES**: Full chain validated — proceed with HTTP transport architecture
- UX note: Message awareness is model-mediated, not user-visible
