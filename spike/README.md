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

## Expected Results

| Test | Expected | Fallback if fails |
|------|----------|-------------------|
| `simulate_message` works | Returns success message | Debug FastMCP tool registration |
| Tool description updates server-side | Server logs show update | Check FastMCP remove/add_tool |
| `list_changed` notification sent | FastMCP sends automatically | May need manual notification |
| Claude Code refreshes tool list | Tool description visible to Claude | Use hook+tool hybrid instead |

## Exit Criteria

- **YES**: Claude Code sees updated tool descriptions → proceed with HTTP transport architecture
- **NO**: Descriptions don't update → pivot to hook+tool hybrid (see docs/phase1-recommendation.md)
