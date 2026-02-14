# Biff Architecture Analysis: Message RX and Real-Time Communication

**Author**: forge-eng
**Date**: 2026-02-13
**Status**: Phase 1 Feasibility Analysis
**Context**: [biff-6k7] Spike: MCP notification rendering and subprocess handoff

## Executive Summary

This analysis evaluates architecture approaches for biff's two critical features: asynchronous message reception (`/mesg`, `/wall`) and real-time bidirectional communication (`/talk`). The analysis is grounded in confirmed MCP/Claude Code constraints and provides concrete recommendations with implementation estimates.

**Key Findings:**
- **Message RX**: Recommend **Hybrid (C)** — Hook for notification, tool for retrieval
- **Talk**: Recommend **tmux Split-Pane (A)** with fallback to Tool-Call Loop (B)
- **Phase 1 Scope**: Implement Message RX Hybrid + Talk Tool-Call Loop
- **Phase 2 Scope**: Add tmux-based real-time talk once core patterns proven

## Constraints (Confirmed)

These constraints are facts, not assumptions:

1. **`notifications/message` silently dropped**: Claude Code does not render MCP server notifications sent via `notifications/message` notification type. This is confirmed behavior as of 2026-02-13.

2. **No PTY allocation in Bash tool**: The `Bash` tool does not allocate a PTY for spawned processes. Interactive subprocesses expecting TTY input (e.g., `python -c "input('> ')"`) will fail with `EOFError`.

3. **stdio transport reserved**: MCP servers using stdio transport (default for Claude Code integration) have stdout reserved for JSON-RPC messages only. Writing non-JSON-RPC data to stdout will break the protocol.

4. **No MCP tools in background subagents**: When Claude Code spawns background agents, those agents do not have access to MCP server tools from the parent session.

5. **Hook stdout bugs**: UserPromptSubmit hook has known stdout rendering issues (Claude Code issues #13912, #12151). Hook output may be silently dropped or garbled.

6. **No server push to client**: MCP protocol does not support server-initiated tool calls or context injection. All tool calls must originate from the client (Claude).

## Message RX Approaches

### A: Hook + Local File (Lightweight Notification)

**Architecture:**
```
MCP Server (biff)
  |
  +-- writes to ~/.biff/notifications.json when /mesg received
  |
UserPromptSubmit Hook
  |
  +-- reads ~/.biff/notifications.json (rate-limited)
  +-- injects additionalContext: "You have 3 unread messages from @alice"
  |
Claude sees notification text inline with every prompt (rate-limited)
```

**Technical Feasibility: 3/5**

**Pros:**
- Proactive: user sees notifications without explicit tool call
- Lightweight: file read is fast (~1ms)
- MCP-agnostic: no reliance on `notifications/*` protocol
- Rate-limited: check once per N prompts to avoid spam

**Cons:**
- Hook stdout bugs: notifications may be silently dropped
- Not real-time: only triggers on user prompts
- Persistence: file-based state needs cleanup/rotation
- Hook dependency: if hooks break, notifications stop

**Integration with fastmcp:**
- `@mcp.tool()` for `send_message(to: str, body: str)` writes to both relay AND local file
- Hook reads file, returns notification text via `additionalContext`
- File format: `{"count": 3, "latest": {"from": "alice", "timestamp": 1234567890}}`

**Implementation Complexity: ~150 LOC**
- Message storage: 50 LOC (atomic write, rotation)
- Hook implementation: 50 LOC (read, rate-limit, format)
- Tests: 50 LOC (mock file I/O, hook invocation)

**Dependencies:**
- `fastmcp>=2.0.0` (already in pyproject.toml)
- Standard library only (json, pathlib, time)

**Testing Strategy:**
- Unit test: write message, verify file updated
- Unit test: hook reads file, returns correct context
- Integration test: rate-limiting behavior (check every Nth prompt)
- Manual test: send message, verify Claude sees notification on next prompt

**Risk Assessment:**
- **High risk**: Hook stdout bugs may make this unreliable
- **Medium risk**: File state corruption (needs atomic write)
- **Low risk**: Performance impact (file read is fast)

**Performance:**
- Latency: ~1ms file read per rate-limited prompt
- Memory: ~1KB persistent file
- CPU: negligible

**Maintenance Burden:**
- Hook API changes: if UserPromptSubmit changes, need to update
- File format migrations: if schema changes, need migration logic
- Cleanup logic: stale notifications need expiration

---

### B: Pure MCP Tool Polling (Explicit Retrieval)

**Architecture:**
```
MCP Server (biff)
  |
  +-- @mcp.tool() check_messages() -> List[Message]
  |
  +-- stores messages in ~/.biff/inbox.jsonl
  |
Claude calls check_messages when:
  - User explicitly asks: "/mesg check" or "do I have messages?"
  - Proactively: Claude decides to check (low probability)
```

**Technical Feasibility: 5/5**

**Pros:**
- **Highest reliability**: no hook dependencies, pure tool call
- **Simple state model**: JSONL append-only log
- **Explicit UX**: user controls when to check
- **No stdout bugs**: standard tool call response

**Cons:**
- **Not proactive**: messages sit unread until user asks
- **Poor discovery**: users may not know to check
- **Tool call latency**: 200-500ms per check

**Integration with fastmcp:**
```python
@mcp.tool()
async def check_messages() -> list[Message]:
    """Check for unread messages."""
    inbox_path = Path.home() / ".biff" / "inbox.jsonl"
    messages = []
    if inbox_path.exists():
        with inbox_path.open() as f:
            for line in f:
                msg = json.loads(line)
                if not msg.get("read"):
                    messages.append(Message(**msg))
    return messages

@mcp.tool()
async def mark_read(message_id: str) -> None:
    """Mark message as read."""
    # Rewrite inbox.jsonl with updated read status
    ...
```

**Implementation Complexity: ~200 LOC**
- Inbox storage: 80 LOC (JSONL append, read filtering, mark read)
- Tools: 60 LOC (check_messages, mark_read, delete_message)
- Models: 40 LOC (Message, MessageStatus dataclasses)
- Tests: 100 LOC (storage, tool responses, edge cases)

**Dependencies:**
- `fastmcp>=2.0.0`
- Standard library only (json, pathlib)

**Testing Strategy:**
- Unit test: write message to inbox, verify append
- Unit test: check_messages returns only unread
- Unit test: mark_read updates status
- Integration test: send message, check, mark read, verify empty
- Manual test: send message, call `/mesg check`, verify response

**Risk Assessment:**
- **Low risk**: standard tool call, no exotic dependencies
- **Low risk**: JSONL format is robust
- **Low risk**: append-only minimizes corruption risk

**Performance:**
- Latency: 200-500ms tool call overhead + 1-5ms file read
- Memory: ~1KB per message in inbox.jsonl
- CPU: negligible

**Maintenance Burden:**
- **Minimal**: stable API, no hook dependencies
- File rotation: need periodic cleanup of old messages
- Schema evolution: JSONL supports additive changes

---

### C: Hybrid (Hook Notification + Tool Retrieval) ⭐ RECOMMENDED

**Architecture:**
```
MCP Server (biff)
  |
  +-- writes to ~/.biff/notifications.json (count only)
  +-- writes to ~/.biff/inbox.jsonl (full messages)
  |
UserPromptSubmit Hook
  |
  +-- reads ~/.biff/notifications.json
  +-- injects additionalContext: "You have 3 unread messages. Use /mesg check to read."
  |
Claude sees notification, calls check_messages tool
  |
  +-- @mcp.tool() check_messages() reads inbox.jsonl
  +-- returns full message list with bodies
```

**Technical Feasibility: 4/5**

**Pros:**
- **Best of both worlds**: proactive notification + reliable retrieval
- **Degraded gracefully**: if hooks break, tool still works
- **Minimal hook risk**: hook only returns count, not full message bodies
- **Low latency notification**: hook is fast (file read)
- **High reliability retrieval**: tool call guarantees delivery

**Cons:**
- **More complexity**: two code paths (hook + tool)
- **Hook dependency**: notification relies on hook working
- **File state duplication**: count in one file, messages in another

**Integration with fastmcp:**
```python
# On message receive:
def _store_message(msg: Message) -> None:
    # Append to inbox
    inbox_path.append(msg.to_json())

    # Update notification count
    notif_path = Path.home() / ".biff" / "notifications.json"
    count = _count_unread(inbox_path)
    notif_path.write_text(json.dumps({"count": count, "updated_at": time.time()}))

# Hook implementation (separate from MCP server):
def on_user_prompt_submit(prompt: str) -> str:
    notif_path = Path.home() / ".biff" / "notifications.json"
    if not notif_path.exists():
        return ""

    data = json.loads(notif_path.read_text())
    if data["count"] == 0:
        return ""

    # Rate limit: only notify every 10th prompt
    if _should_notify():
        return f"You have {data['count']} unread messages. Use /mesg check to read."
    return ""

# MCP tool:
@mcp.tool()
async def check_messages() -> list[Message]:
    """Check and retrieve unread messages."""
    inbox_path = Path.home() / ".biff" / "inbox.jsonl"
    messages = []
    if inbox_path.exists():
        with inbox_path.open() as f:
            for line in f:
                msg = json.loads(line)
                if not msg.get("read"):
                    messages.append(Message(**msg))
    return messages
```

**Implementation Complexity: ~300 LOC**
- Message storage: 100 LOC (inbox.jsonl + notifications.json)
- Hook: 50 LOC (read count, format notification, rate-limit)
- Tools: 100 LOC (check_messages, mark_read, delete_message)
- Models: 40 LOC (Message, MessageStatus dataclasses)
- Tests: 150 LOC (storage, hook, tools, integration)

**Dependencies:**
- `fastmcp>=2.0.0`
- Standard library only (json, pathlib, time)

**Testing Strategy:**
- Unit test: message storage writes both files
- Unit test: hook reads count, formats notification
- Unit test: check_messages returns full messages
- Unit test: rate-limiting works correctly
- Integration test: send message, verify notification, check messages, verify retrieval
- Manual test: send message, wait for notification, call check_messages

**Risk Assessment:**
- **Medium risk**: hook may fail, but tool provides fallback
- **Low risk**: dual file approach is redundant but safe
- **Low risk**: rate-limiting prevents notification spam

**Performance:**
- Latency: 1ms hook file read + 200-500ms tool call (on demand)
- Memory: ~2KB (notifications.json + inbox.jsonl)
- CPU: negligible

**Maintenance Burden:**
- **Medium**: two code paths to maintain
- **Low**: if hooks break, tool path still works
- **Medium**: need to keep notification count in sync with inbox

---

## Talk (Real-Time Communication) Approaches

### A: tmux Split-Pane (Native Real-Time)

**Architecture:**
```
User types: /talk @alice

MCP Tool: talk_start(user: str)
  |
  +-- spawns: tmux split-window -v -l 15 'uv run python -m biff.talk --user alice'
  |
  +-- Textual TUI launches in new pane with real TTY
  |
  +-- User types directly in TUI pane (no Claude intermediation)
  |
  +-- TUI connects to biff MCP server via Unix domain socket
        |
        +-- sends: {"type": "talk_message", "to": "alice", "body": "hello"}
        +-- receives: {"type": "talk_message", "from": "alice", "body": "hi back"}
  |
  +-- Press Ctrl-D to exit TUI, returns to Claude session
```

**Technical Feasibility: 4/5**

**Pros:**
- **True real-time**: sub-100ms message round-trip
- **Native UX**: user types directly, no LLM latency
- **Familiar**: tmux split-pane is standard for terminal users
- **Clean separation**: TUI has full TTY, Claude session unaffected
- **Textual library**: rich TUI with minimal code

**Cons:**
- **tmux dependency**: requires tmux installed and active session
- **Pane management**: need to clean up pane on exit
- **IPC complexity**: Unix socket or named pipe for MCP<->TUI communication
- **State synchronization**: TUI needs to know relay connection details
- **Multi-session**: what if multiple /talk sessions active?

**Integration with fastmcp:**
```python
@mcp.tool()
async def talk_start(user: str) -> str:
    """Start real-time conversation with a user in tmux split pane."""
    # Check if tmux is available
    if not _is_tmux_active():
        return "Error: tmux not running. Use /talk-loop instead."

    # Create Unix socket for IPC
    socket_path = Path.home() / ".biff" / f"talk-{uuid.uuid4()}.sock"

    # Spawn TUI in tmux split
    cmd = f"tmux split-window -v -l 15 'uv run python -m biff.talk --user {user} --socket {socket_path}'"
    subprocess.run(cmd, shell=True, check=True)

    # Start background thread to forward messages to socket
    asyncio.create_task(_forward_messages_to_tui(socket_path))

    return f"Talk session with @{user} started in tmux pane. Press Ctrl-D to exit."

# biff.talk module (Textual TUI):
class TalkApp(App):
    def __init__(self, user: str, socket_path: Path):
        self.user = user
        self.socket_path = socket_path
        self.messages = []

    async def on_mount(self):
        # Connect to MCP server via socket
        self.reader, self.writer = await asyncio.open_unix_connection(str(self.socket_path))
        asyncio.create_task(self._receive_messages())

    async def on_input_submit(self, message: str):
        # Send message via socket
        await self._send_json({"type": "talk_message", "to": self.user, "body": message})

    async def _receive_messages(self):
        # Receive messages from socket
        while True:
            data = await self.reader.readline()
            msg = json.loads(data)
            self.messages.append(msg)
            self.refresh()
```

**Implementation Complexity: ~500 LOC**
- TUI (Textual): 200 LOC (message list, input box, layout)
- IPC layer: 150 LOC (Unix socket server, message forwarding)
- MCP tool: 80 LOC (spawn tmux, setup socket, cleanup)
- State management: 70 LOC (active sessions, cleanup on exit)
- Tests: 200 LOC (mock tmux, socket communication, TUI behavior)

**Dependencies:**
- `fastmcp>=2.0.0`
- `textual>=1.0.0` (rich TUI library)
- `tmux` (external binary, user must install)

**Testing Strategy:**
- Unit test: socket IPC (mock socket, send/receive messages)
- Unit test: tmux detection (mock `pgrep tmux`)
- Integration test: spawn TUI, send message, verify received
- Manual test: run `/talk @alice` in tmux, verify split-pane appears, type message, verify sent

**Risk Assessment:**
- **High risk**: tmux not available (need fallback)
- **Medium risk**: pane cleanup (orphaned panes if crash)
- **Medium risk**: IPC socket cleanup (stale sockets)
- **Low risk**: Textual TUI is stable library

**Performance:**
- Latency: 50-100ms message round-trip (IPC + relay)
- Memory: ~5MB for Textual TUI process
- CPU: negligible (async I/O)

**Maintenance Burden:**
- **Medium**: tmux API changes rare but possible
- **Medium**: Textual version updates may break layout
- **High**: need to handle edge cases (multiple sessions, crashes)

---

### B: Tool-Call Loop (Claude-Mediated) ⭐ RECOMMENDED FOR PHASE 1

**Architecture:**
```
User types: /talk @alice

MCP Tool: talk_start(user: str)
  |
  +-- creates talk session state
  +-- returns: "Talk session with @alice started. Type your message or '/talk end' to exit."

User types: "Hey Alice, can you review my PR?"

Claude calls: send_talk_message(user: "alice", body: "Hey Alice, can you review my PR?")
  |
  +-- MCP server sends message to relay
  +-- relay forwards to alice's session

Claude proactively calls: check_talk_reply(user: "alice")
  |
  +-- MCP server polls relay for reply
  +-- returns: {"from": "alice", "body": "Sure, send me the link"}

Claude shows reply to user inline
```

**Technical Feasibility: 5/5**

**Pros:**
- **No dependencies**: works with pure MCP tools
- **No PTY issues**: no subprocess, no TTY needed
- **Simple state**: just track active session in ~/.biff/talk_session.json
- **Reliable**: standard tool call path, no exotic IPC
- **Fallback friendly**: works when tmux unavailable

**Cons:**
- **Not real-time**: 1-2 second latency per message (Claude mediation)
- **Claude in the loop**: LLM may summarize or filter messages
- **Polling required**: Claude must proactively check for replies
- **Context window**: long conversations consume tokens

**Integration with fastmcp:**
```python
@mcp.tool()
async def talk_start(user: str) -> str:
    """Start a conversation session with a user."""
    session_path = Path.home() / ".biff" / "talk_session.json"
    session_path.write_text(json.dumps({
        "user": user,
        "started_at": time.time(),
        "active": True
    }))
    return f"Talk session with @{user} started. Type your messages and I'll relay them. Say '/talk end' to finish."

@mcp.tool()
async def send_talk_message(user: str, body: str) -> str:
    """Send a message in active talk session."""
    # Send to relay
    await _send_to_relay({"type": "talk", "to": user, "body": body})
    return f"Sent to @{user}: {body}"

@mcp.tool()
async def check_talk_reply(user: str) -> list[Message]:
    """Check for replies in active talk session."""
    messages = await _fetch_from_relay({"type": "talk", "from": user})
    return messages

@mcp.tool()
async def talk_end() -> str:
    """End active talk session."""
    session_path = Path.home() / ".biff" / "talk_session.json"
    session_path.unlink(missing_ok=True)
    return "Talk session ended."
```

**Implementation Complexity: ~250 LOC**
- Session management: 80 LOC (start, end, track state)
- Tools: 120 LOC (send, check, end)
- Relay integration: 50 LOC (send to relay, poll for replies)
- Tests: 150 LOC (session lifecycle, message send/receive)

**Dependencies:**
- `fastmcp>=2.0.0`
- Standard library only (json, pathlib, time)

**Testing Strategy:**
- Unit test: session creation, state tracking
- Unit test: send_talk_message calls relay
- Unit test: check_talk_reply returns messages
- Integration test: start session, send, receive, end
- Manual test: `/talk @alice`, send message, wait for reply

**Risk Assessment:**
- **Low risk**: standard tool calls, no exotic dependencies
- **Medium risk**: Claude may not proactively check for replies (need prompt engineering)
- **Low risk**: session state corruption (atomic write)

**Performance:**
- Latency: 1-2 seconds per message (Claude mediation + tool call + relay)
- Memory: ~1KB session state
- CPU: negligible

**Maintenance Burden:**
- **Minimal**: stable API, no external dependencies
- **Low**: session cleanup (delete stale sessions)

---

## Recommended Phase 1 Architecture

### Message RX: Hybrid (C)

Implement Hook + Tool approach with graceful degradation:

1. **Notification Hook**: Lightweight notification count in `additionalContext`
2. **Retrieval Tool**: Reliable `check_messages()` tool for full message bodies
3. **Fallback**: If hook fails, users can still call `/mesg check` directly

**Rationale:**
- Best user experience when hooks work (proactive notification)
- Reliable fallback when hooks fail (explicit tool call)
- Low implementation risk (hook is simple, tool is standard)

### Talk: Tool-Call Loop (B)

Implement Claude-mediated conversation for Phase 1:

1. **Simple state**: track active session in `talk_session.json`
2. **Three tools**: `talk_start`, `send_talk_message`, `check_talk_reply`, `talk_end`
3. **Prompt engineering**: encourage Claude to proactively check for replies

**Rationale:**
- Zero external dependencies (no tmux requirement)
- Proven tool call path (reliable)
- Faster to implement (~250 LOC vs ~500 LOC for tmux)
- Can add tmux-based real-time talk in Phase 2 once patterns proven

### Phase 2 Additions

Once Phase 1 is stable and hook behavior is validated:

1. **Real-Time Talk (tmux)**: Add `talk_start_realtime()` tool that spawns Textual TUI
2. **Auto-detection**: If tmux available, use real-time; else fall back to tool-call loop
3. **Hook optimization**: If hooks prove reliable, add more context (sender, preview)

---

## Module Layout

```
src/biff/
├── __init__.py
├── __main__.py              # CLI entry point (typer)
├── mcp_server.py            # FastMCP server definition
├── models.py                # Pydantic models (Message, Session, User)
├── config.py                # .biff file parsing, settings
├── storage/
│   ├── __init__.py
│   ├── inbox.py             # inbox.jsonl read/write
│   ├── notifications.py     # notifications.json read/write
│   └── sessions.py          # talk_session.json read/write
├── tools/
│   ├── __init__.py
│   ├── mesg.py              # /mesg tools: check_messages, mark_read
│   ├── talk.py              # /talk tools: talk_start, send_talk_message, check_talk_reply
│   ├── plan.py              # /plan tools: set_plan, get_plan
│   ├── finger.py            # /finger tools: finger_user
│   └── who.py               # /who tools: list_active_sessions
├── hooks/
│   ├── __init__.py
│   └── user_prompt_submit.py  # UserPromptSubmit hook for notifications
├── relay/
│   ├── __init__.py
│   └── local.py             # Local relay for same-machine communication
└── talk_tui/                # Phase 2: Textual TUI for real-time talk
    ├── __init__.py
    └── app.py               # Textual app, IPC with MCP server

tests/
├── conftest.py
├── test_models.py
├── test_storage.py
├── test_tools_mesg.py
├── test_tools_talk.py
├── test_hooks.py
├── test_relay_local.py
└── integration/
    ├── test_mesg_flow.py
    └── test_talk_flow.py
```

**Estimated LOC for Phase 1:**
- Models: 100 LOC
- Storage: 300 LOC
- Tools (mesg, talk, plan, finger, who): 600 LOC
- Hooks: 100 LOC
- Relay (local): 200 LOC
- MCP server: 150 LOC
- Tests: 800 LOC
- **Total: ~2,250 LOC**

---

## Implementation Phases

### Phase 1A: Core Infrastructure (Week 1)

**Goal**: Storage, models, local relay

**Deliverables:**
- `models.py`: Message, User, Session, TalkSession dataclasses
- `storage/inbox.py`: JSONL append, read, mark_read
- `storage/notifications.py`: notification count read/write
- `storage/sessions.py`: talk session state
- `relay/local.py`: same-machine message passing (file-based or Unix socket)
- Tests: 100% coverage on storage and models

**LOC**: ~600 (400 code + 200 tests)

**Risks**:
- File corruption (mitigated by atomic writes)
- Concurrent access (mitigated by file locking)

### Phase 1B: Message Tools (Week 2)

**Goal**: `/mesg` command working end-to-end

**Deliverables:**
- `tools/mesg.py`: `send_message`, `check_messages`, `mark_read`, `delete_message`
- `mcp_server.py`: FastMCP server with mesg tools registered
- Hook: `hooks/user_prompt_submit.py` for lightweight notification
- Tests: tool invocation, hook behavior, integration tests

**LOC**: ~700 (500 code + 200 tests)

**Risks**:
- Hook stdout bugs (validated during Phase 1B)
- Rate-limiting logic (tested with time mocking)

### Phase 1C: Talk Tools (Week 3)

**Goal**: `/talk` command working via tool-call loop

**Deliverables:**
- `tools/talk.py`: `talk_start`, `send_talk_message`, `check_talk_reply`, `talk_end`
- Integration with local relay
- Prompt engineering: encourage Claude to proactively check replies
- Tests: session lifecycle, message exchange

**LOC**: ~500 (350 code + 150 tests)

**Risks**:
- Claude may not proactively check (mitigated by prompt engineering)
- Long conversations consume context window (document limitation)

### Phase 1D: Discovery Tools (Week 4)

**Goal**: `/plan`, `/finger`, `/who` commands

**Deliverables:**
- `tools/plan.py`: `set_plan`, `get_plan`
- `tools/finger.py`: `finger_user` (read user's plan and status)
- `tools/who.py`: `list_active_sessions` (read from relay)
- `.biff` file configuration parsing
- Tests: tool behavior, config parsing

**LOC**: ~450 (300 code + 150 tests)

**Risks**:
- `.biff` file schema (keep simple, use INI format or TOML)

---

## Testing Strategy

### Unit Tests (70% of test suite)

**Storage:**
- Write message to inbox, verify JSONL append
- Read inbox, filter by read/unread status
- Mark message as read, verify update
- Atomic write behavior (no partial writes)
- Concurrent access (file locking)

**Tools:**
- Mock relay, call `send_message`, verify relay receives
- Mock inbox, call `check_messages`, verify filtering
- Mock session state, call `talk_start`, verify session created

**Hooks:**
- Mock notification file, call hook, verify context injected
- Rate-limiting logic (check only every Nth prompt)
- Hook with zero unread, verify empty context

### Integration Tests (20% of test suite)

**Message flow:**
1. Send message via `send_message` tool
2. Verify inbox.jsonl appended
3. Verify notifications.json updated
4. Call `check_messages` tool
5. Verify message returned
6. Call `mark_read` tool
7. Verify message marked read

**Talk flow:**
1. Call `talk_start` tool
2. Verify session state created
3. Call `send_talk_message` tool
4. Verify relay receives message
5. Mock incoming reply
6. Call `check_talk_reply` tool
7. Verify reply returned
8. Call `talk_end` tool
9. Verify session state deleted

### Manual Tests (10% of test suite)

**Hook validation:**
- Send message, wait for notification on next prompt
- Verify notification text format
- Verify rate-limiting works

**Talk UX:**
- Start talk session
- Send 3-5 messages back and forth
- Verify latency acceptable (1-2s per message)
- Verify Claude proactively checks for replies

**Error cases:**
- Try to send message to nonexistent user
- Try to check messages with corrupted inbox file
- Try to start talk session while one is already active

---

## Risk Assessment Matrix

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Hook stdout bugs make notifications unreliable | High | Medium | Fallback to explicit `/mesg check` tool call |
| Claude does not proactively check talk replies | Medium | High | Prompt engineering, document limitation |
| File corruption (inbox.jsonl, notifications.json) | Low | High | Atomic writes, file locking, validation on read |
| Relay connection issues (local relay) | Low | Medium | Retry logic, clear error messages |
| Stale session state (talk_session.json) | Medium | Low | TTL-based cleanup, warn on stale sessions |
| `.biff` file schema evolution | Low | Low | Versioned schema, migration logic |
| tmux not available (Phase 2) | High | Medium | Fallback to tool-call loop, auto-detect tmux |

---

## Performance Benchmarks

### Message RX (Hybrid Approach)

**Hook path (proactive notification):**
- File read (notifications.json): ~1ms
- Context injection: ~5ms (string formatting)
- Total overhead per rate-limited prompt: **~6ms**

**Tool path (explicit retrieval):**
- Tool call overhead: ~200-500ms
- File read (inbox.jsonl): ~1-5ms (depends on message count)
- JSON parsing: ~1ms per message
- Total latency: **~200-500ms**

**Storage:**
- notifications.json: ~1KB
- inbox.jsonl: ~1KB per 10 messages
- Disk I/O: negligible (all files < 100KB for typical use)

### Talk (Tool-Call Loop)

**Message round-trip:**
- Claude receives user input: 0ms (native)
- Claude calls `send_talk_message`: ~200-500ms (tool call)
- Relay forwards message: ~50-100ms (local relay)
- Remote session receives: ~50-100ms (relay poll)
- Remote user replies: variable (human input)
- Claude calls `check_talk_reply`: ~200-500ms (tool call)
- Claude shows reply: ~100-200ms (rendering)
- **Total round-trip: 1-2 seconds + human reply time**

### Talk (tmux Split-Pane, Phase 2)

**Message round-trip:**
- User types in TUI: 0ms (native TTY)
- TUI sends to socket: ~5-10ms (IPC)
- Relay forwards message: ~50-100ms (local relay)
- Remote session receives: ~50-100ms (relay poll)
- Remote user replies: variable (human input)
- Reply arrives via socket: ~50-100ms
- TUI renders: ~5-10ms
- **Total round-trip: 150-300ms + human reply time**

---

## Maintenance Burden

### Hook Maintenance (Medium Burden)

**What could break:**
- UserPromptSubmit API changes in Claude Code
- Hook stdout rendering changes (already buggy)
- Rate-limiting logic needs tuning

**Monitoring:**
- Log hook invocations and success/failure
- Track notification delivery rate
- Alert if hook success rate < 80%

**Mitigation:**
- Keep hook simple (count only, no complex logic)
- Fallback to tool call if hook fails
- Document hook limitations for users

### Tool Maintenance (Low Burden)

**What could break:**
- fastmcp API changes (rare, fastmcp is stable)
- File format schema evolution (need migrations)

**Monitoring:**
- Tool call success rate
- Latency metrics (p50, p95, p99)
- Error rate by tool

**Mitigation:**
- Comprehensive unit tests
- Schema versioning in JSON files
- Clear error messages

### Relay Maintenance (Medium Burden, Phase 2)

**What could break:**
- Network relay protocol changes
- WebSocket connection issues
- Authentication/authorization changes

**Monitoring:**
- Relay connection uptime
- Message delivery rate
- Latency metrics

**Mitigation:**
- Retry logic with exponential backoff
- Heartbeat/ping to detect connection loss
- Clear error messages for connectivity issues

---

## Alternative Approaches Considered

### Message RX: Server-Sent Events (SSE)

**Why rejected:**
- MCP stdio transport does not support SSE
- Would require HTTP transport, breaking Claude Code integration

### Message RX: Polling from Client

**Why rejected:**
- Client (Claude) cannot initiate background tasks
- Tool calls only triggered by user input or Claude decision

### Talk: WebRTC for Real-Time

**Why rejected:**
- Overkill for text-only communication
- High implementation complexity
- No clear benefit over tmux split-pane

### Talk: Shared Terminal Session (screen/tmux attach)

**Why rejected:**
- Requires both users in same tmux/screen session
- Poor isolation (users see each other's full session)
- Not secure (full terminal access)

---

## Open Questions

1. **Hook reliability**: What is the actual success rate of UserPromptSubmit hooks in Claude Code? Need to test in Phase 1B.

2. **Rate-limiting tuning**: Check every Nth prompt for notifications. What is optimal N? (Start with N=10, tune based on user feedback)

3. **Claude proactivity**: Will Claude reliably check for talk replies without explicit user prompt? (Test with prompt engineering in Phase 1C)

4. **File rotation**: How many messages to keep in inbox.jsonl before rotation? (Start with 1000 messages, ~100KB file size)

5. **Relay protocol**: Should local relay use Unix socket or file-based? (Start with file-based for simplicity, add socket in Phase 2)

6. **Authentication**: How to authenticate users in Phase 2 network relay? (JWT, signed messages, see Phase 2 spec)

---

## Conclusion

**Recommended Phase 1 Architecture:**

1. **Message RX: Hybrid (Hook + Tool)**
   - Feasibility: 4/5
   - Implementation: ~700 LOC
   - Timeframe: 2 weeks
   - Risk: Medium (hook may be unreliable, but tool provides fallback)

2. **Talk: Tool-Call Loop**
   - Feasibility: 5/5
   - Implementation: ~500 LOC
   - Timeframe: 1 week
   - Risk: Low (standard tool call path)

**Total Phase 1 Estimate:**
- LOC: ~2,250 (code + tests)
- Timeframe: 4 weeks
- Risk: Medium (primarily hook reliability)

**Phase 2 Enhancements:**
- Real-time talk via tmux split-pane (~500 LOC)
- Network relay for remote communication (~800 LOC)
- Team commands: `/wall`, `/hive` (~300 LOC)

**Exit Criteria for Phase 1:**
- [ ] Engineer can send `/mesg @alice "hello"`
- [ ] Engineer receives notification of incoming messages (via hook or explicit check)
- [ ] Engineer can read messages via `/mesg check`
- [ ] Engineer can start `/talk @alice` session
- [ ] Engineer can exchange 5+ messages in talk session
- [ ] Engineer can view their own plan via `/plan`
- [ ] Engineer can view teammate's plan via `/finger @alice`
- [ ] Engineer can see active sessions via `/who`
- [ ] All quality gates pass (ruff, mypy, pytest with 100% coverage)
- [ ] Documentation complete (README, API docs, user guide)

**Next Steps:**
1. Validate hook reliability (run test MCP server with UserPromptSubmit hook)
2. Validate tmux integration (test tmux split-pane spawning and cleanup)
3. Begin Phase 1A implementation (storage, models, local relay)
