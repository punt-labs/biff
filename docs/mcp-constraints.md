# MCP and Claude Code Constraints

**Date**: 2026-02-13
**Status**: Confirmed via testing and documentation review
**Purpose**: Reference document for proven constraints that affect biff architecture

## Overview

This document records the confirmed technical constraints of MCP protocol and Claude Code implementation that directly impact biff's architecture. These are facts, not assumptions.

## Confirmed Constraints

### 1. Server-Initiated Notifications Are Dropped

**Constraint**: MCP servers cannot push notifications that render visibly in Claude Code sessions.

**Evidence**:
- MCP specification defines `notifications/message` notification type
- Claude Code silently drops these notifications (confirmed behavior as of 2026-02-13)
- No error, no rendering, no indication to user

**Impact on biff**:
- Cannot use server push for incoming message notifications
- Must use hook-based approach (UserPromptSubmit) for proactive notification
- Must provide tool-based polling as fallback

**Workaround**:
- Hook: `UserPromptSubmit` reads notification file, injects `additionalContext`
- Tool: `check_messages()` tool for explicit retrieval
- Hybrid: Hook notifies count, tool retrieves full messages

**Code Example (what does NOT work)**:
```python
from mcp.types import Notification, LoggingMessageNotificationParams

# This notification is silently dropped by Claude Code
notification = Notification(
    method="notifications/message",
    params=LoggingMessageNotificationParams(
        level="info",
        data="You have 3 unread messages from @alice"
    )
)
await session.send_notification(notification)
```

### 2. No PTY Allocation in Bash Tool

**Constraint**: The Bash tool does not allocate a pseudo-terminal (PTY) for spawned processes.

**Evidence**:
- `python -c "input('> ')"` fails with `EOFError` in Bash tool
- `subprocess.Popen(..., stdin=subprocess.PIPE)` receives closed stdin
- Confirmed via direct testing in Claude Code session

**Impact on biff**:
- Cannot spawn interactive subprocess for real-time talk via simple `subprocess.run()`
- Must use tmux split-pane approach (spawns process with native TTY)
- Must provide tool-call loop as non-interactive fallback

**Workaround**:
- Phase 1: Tool-call loop (Claude mediates conversation)
- Phase 2: tmux split-pane (spawns Textual TUI with real TTY)

**Code Example (what does NOT work)**:
```python
@mcp.tool()
async def talk_interactive(user: str) -> str:
    """Start interactive talk session."""
    # This fails: subprocess has no TTY, input() raises EOFError
    subprocess.run([
        "python", "-c",
        "while True: msg = input('> '); print(f'Sent: {msg}')"
    ])
    return "Talk ended"
```

**Code Example (what DOES work)**:
```python
@mcp.tool()
async def talk_interactive(user: str) -> str:
    """Start interactive talk session."""
    # Spawn in tmux with native TTY
    subprocess.run([
        "tmux", "split-window", "-v", "-l", "15",
        f"uv run python -m biff.talk --user {user}"
    ])
    return f"Talk session with @{user} started in tmux pane."
```

### 3. stdout Reserved for JSON-RPC (stdio Transport)

**Constraint**: MCP servers using stdio transport (default for Claude Code) have stdout reserved for JSON-RPC protocol messages.

**Evidence**:
- MCP specification: stdio transport uses stdin/stdout for JSON-RPC
- Writing non-JSON-RPC data to stdout breaks protocol
- stderr is available for logging

**Impact on biff**:
- Cannot write progress/status messages to stdout
- Must use stderr for logging
- Must use IPC (Unix socket, named pipe) for subprocess communication

**Workaround**:
- Use stderr for all logging
- Use Unix socket for MCP server ↔ Textual TUI communication
- Never write to stdout directly

**Code Example (what does NOT work)**:
```python
@mcp.tool()
async def send_message(to: str, body: str) -> str:
    print(f"Sending message to {to}...")  # BREAKS PROTOCOL
    # ... send message ...
    return "Message sent"
```

**Code Example (what DOES work)**:
```python
import logging
import sys

# Configure logging to stderr
logging.basicConfig(stream=sys.stderr, level=logging.INFO)

@mcp.tool()
async def send_message(to: str, body: str) -> str:
    logging.info(f"Sending message to {to}")  # OK: uses stderr
    # ... send message ...
    return "Message sent"
```

### 4. No MCP Tools in Background Subagents

**Constraint**: When Claude Code spawns background agents (e.g., for parallel tasks), those agents do not have access to MCP server tools from the parent session.

**Evidence**:
- Confirmed via testing with background agent workflows
- Background agents see different tool inventory

**Impact on biff**:
- Cannot call biff tools from background agents
- Must provide alternative communication path (files, sockets)

**Workaround**:
- Phase 1: Not relevant (single-session only)
- Phase 2: If multi-agent support needed, use file-based signaling

### 5. Hook stdout Rendering Bugs

**Constraint**: UserPromptSubmit hook has known stdout rendering issues in Claude Code.

**Evidence**:
- Claude Code issues #13912, #12151
- Hook output may be silently dropped or garbled
- Inconsistent behavior across versions

**Impact on biff**:
- Hook-based notifications may be unreliable
- Must provide tool-based fallback
- Must keep hook implementation simple (minimize risk)

**Workaround**:
- Hook only returns count (lightweight, low risk)
- Tool provides reliable retrieval (full messages)
- Hybrid approach: best of both worlds

**Code Example (high-risk hook)**:
```python
def on_user_prompt_submit(prompt: str) -> str:
    """High-risk: returning large/complex output."""
    messages = get_all_unread_messages()  # Could be 10+ messages
    # This may be garbled or dropped
    return "\n".join([
        f"[{msg.from_user}] {msg.body[:50]}..." for msg in messages
    ])
```

**Code Example (low-risk hook)**:
```python
def on_user_prompt_submit(prompt: str) -> str:
    """Low-risk: returning minimal count."""
    count = get_unread_count()
    if count == 0:
        return ""
    # Simple, short string - less likely to be dropped
    return f"You have {count} unread messages. Use /mesg check to read."
```

## Unconfirmed Constraints (Needs Testing)

These constraints are suspected but not yet proven:

### A. Hook Rate Limiting

**Hypothesis**: Hooks may be rate-limited or throttled by Claude Code.

**Test**: Create hook that logs every invocation, send 100 prompts, measure hook call rate.

**Impact**: May need to implement client-side rate limiting to avoid wasted work.

### B. Tool Call Latency

**Hypothesis**: MCP tool calls have 200-500ms overhead.

**Test**: Create minimal tool that returns immediately, measure latency over 100 calls.

**Impact**: Sets baseline for talk round-trip time.

### C. File Watcher Overhead

**Hypothesis**: Watching files for changes (for relay) has acceptable overhead.

**Test**: Benchmark file watching (inotify/FSEvents) with 10 concurrent users.

**Impact**: May need polling instead of watching for Phase 1 simplicity.

## Testing Protocol

When validating a constraint:

1. **Hypothesis**: State what you believe to be true
2. **Test**: Minimal code to prove/disprove
3. **Evidence**: Logs, screenshots, error messages
4. **Conclusion**: Confirmed constraint or rejected hypothesis
5. **Document**: Add to this file with evidence

## References

- MCP Specification: https://spec.modelcontextprotocol.io/
- fastmcp Documentation: https://github.com/jlowin/fastmcp
- Claude Code Issue Tracker: https://github.com/anthropics/claude-code/issues

## Change Log

- 2026-02-13: Initial document with 5 confirmed constraints
