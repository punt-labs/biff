# SPARC Plan: Biff Phase 1 Implementation

**Date:** 2026-02-14
**PRD:** [biff-phase1-communication.md](../prd/biff-phase1-communication.md)
**Status:** Approved

## S — Specification

### Problem Statement

Biff needs a working MCP server that provides async messaging and presence
tools inside Claude Code sessions. The server uses HTTP transport to enable
server-push notification of unread messages via dynamic tool descriptions.

### Success Criteria

1. Six MCP tools registered and functional: `set_plan`, `who`, `finger`,
   `biff_toggle`, `send_message`, `check_messages`
2. Dynamic tool descriptions update when messages arrive (via `tools/list_changed`)
3. HTTP transport mode serves both Streamable HTTP and SSE endpoints
4. stdio transport mode works for local-only use
5. All quality gates pass: ruff, mypy, pytest with 100% coverage

### Functional Requirements

| Req | Description | Priority |
|-----|-------------|----------|
| FR-1 | MCP server starts in HTTP or stdio mode via CLI flag | P0 |
| FR-2 | `set_plan` stores user's status text | P0 |
| FR-3 | `who` returns list of sessions with plans and availability | P0 |
| FR-4 | `finger` returns a specific user's plan and last-active time | P0 |
| FR-5 | `biff_toggle` enables/disables message reception | P0 |
| FR-6 | `send_message` delivers a message to a user's inbox | P0 |
| FR-7 | `check_messages` returns unread messages and marks them read | P0 |
| FR-8 | Tool descriptions update dynamically with unread counts | P0 |
| FR-9 | Messages persist across server restarts (JSONL storage) | P0 |
| FR-10 | `.biff` config file parsed for team roster | P1 |

### Non-Functional Requirements

- Python 3.13+, strict mypy, comprehensive ruff linting
- Immutable data models (`@dataclass(frozen=True)`)
- `from __future__ import annotations` in every file
- Double quotes, 88 char line length
- Zero `Any` types, full Protocol typing

## P — Pseudocode

### MCP Server Initialization

```
function create_server(transport: "http" | "stdio") -> FastMCP:
    server = FastMCP("biff")
    store = MessageStore(data_dir=~/.biff/data/)
    sessions = SessionStore(data_dir=~/.biff/data/)

    register_tools(server, store, sessions)

    if transport == "http":
        start_message_watcher(server, store)  # watches for new messages
    return server

function start_message_watcher(server, store):
    # Background task that watches inbox for changes
    # When new messages arrive, update tool descriptions
    last_count = 0
    while True:
        current = store.get_unread_summary()
        if current.count != last_count:
            update_check_messages_description(server, current)
            last_count = current.count
        sleep(1)  # poll local file every second
```

### Dynamic Tool Description Update

```
function update_check_messages_description(server, summary):
    if summary.count == 0:
        description = "Check for new messages"
    else:
        description = f"Check messages ({summary.count} unread: {summary.preview})"

    # Replace tool with updated description
    # FastMCP sends notifications/tools/list_changed automatically
    server.remove_tool("check_messages")
    server.add_tool(make_check_messages_tool(description))
```

### Message Send/Receive

```
function send_message(to: str, body: str) -> str:
    message = Message(
        id=uuid4(),
        from_user=get_current_user(),
        to_user=to,
        body=body,
        timestamp=now(),
    )
    store.append(to, message)
    return f"Message sent to @{to}"

function check_messages() -> str:
    messages = store.get_unread(user=get_current_user())
    if not messages:
        return "No new messages"
    store.mark_read(messages)
    return format_messages(messages)
```

### Presence

```
function set_plan(text: str) -> str:
    sessions.update_plan(user=get_current_user(), plan=text)
    return f"Plan set: {text}"

function who() -> str:
    active = sessions.get_active(ttl=120)  # active in last 2 min
    return format_roster(active)

function finger(user: str) -> str:
    info = sessions.get_user(user)
    if not info:
        return f"@{user} not found"
    return format_user_info(info)
```

## A — Architecture

### Module Layout

```
src/biff/
    __init__.py          # Package docstring + version
    __main__.py          # CLI entry point (typer)
    server.py            # FastMCP server creation + tool registration
    models.py            # Frozen dataclasses: Message, UserSession, BiffConfig
    config.py            # .biff file parser
    storage/
        __init__.py
        inbox.py         # JSONL message storage (append, read, mark_read)
        sessions.py      # JSON session/presence storage
    tools/
        __init__.py
        messaging.py     # send_message, check_messages
        presence.py      # set_plan, who, finger, biff_toggle
    watcher.py           # Background task: watches inbox, updates tool descriptions

tests/
    __init__.py
    conftest.py          # Fixtures: temp dirs, mock stores, test server
    test_models.py       # Model creation, validation, serialization
    test_config.py       # .biff parsing, edge cases
    test_storage/
        __init__.py
        test_inbox.py    # Append, read, mark_read, concurrent access
        test_sessions.py # Update, get_active, TTL expiry
    test_tools/
        __init__.py
        test_messaging.py  # send_message, check_messages
        test_presence.py   # set_plan, who, finger, biff_toggle
    test_watcher.py      # Description update, list_changed trigger
    test_server.py       # Server creation, transport modes, tool registration
```

### Component Interactions

```
CLI (typer)
    |
    v
server.py (FastMCP)
    |--- tools/messaging.py ---> storage/inbox.py ---> ~/.biff/data/inbox.jsonl
    |--- tools/presence.py  ---> storage/sessions.py -> ~/.biff/data/sessions.json
    |--- watcher.py ---------> reads inbox.py
    |                           updates tool descriptions
    |                           triggers list_changed
    |--- config.py -----------> .biff (repo root)
```

### Data Flow: Message Arrives

```
1. Sender's Claude Code calls send_message("kai", "auth ready")
2. tools/messaging.py creates Message dataclass
3. storage/inbox.py appends to ~/.biff/data/inbox.jsonl
4. watcher.py detects new message (file poll, 1s interval)
5. watcher.py calls server.update_tool_description(...)
6. FastMCP sends notifications/tools/list_changed
7. Recipient's Claude Code refreshes tool list
8. Claude sees: "check_messages (1 unread: @sender about auth ready)"
9. Claude mentions it to user or user calls /check
10. tools/messaging.py reads from inbox.jsonl, marks read
```

## R — Refinement

### Edge Cases

| Edge Case | Handling |
|-----------|----------|
| Message to offline user | Store in inbox; they see it when they start biff |
| Message to `biff off` user | Store in inbox; don't count as unread until `biff on` |
| Self-message | Reject with clear error: "Cannot message yourself" |
| Unknown user | Check .biff roster; return "User @x not in team roster" |
| Empty message body | Reject: "Message body cannot be empty" |
| Very long message | Truncate tool description preview to 80 chars |
| Concurrent writes to inbox | Atomic write (write temp file, rename) |
| Stale session data | TTL of 120s; `who` filters expired sessions |
| Missing ~/.biff/data/ | Create on first use |
| Corrupt JSONL | Skip malformed lines, log warning |

### Error Handling Strategy

- **User errors** (bad input): Return clear error message as tool result
- **System errors** (file I/O): Log, return generic error, never crash
- **Configuration errors** (missing .biff): Work without config, solo mode

### Testing Strategy

- **Unit tests**: Every public function, every edge case
- **Integration tests**: Server creation, tool registration, full send/check cycle
- **Property tests**: Message serialization round-trips
- **No mocking of stdlib**: Use real temp directories, real files

## C — Completion

### Definition of Done

- [ ] All 6 MCP tools functional in both HTTP and stdio modes
- [ ] Dynamic tool descriptions update on message arrival (HTTP mode)
- [ ] `notifications/tools/list_changed` sent and received by Claude Code
- [ ] Messages persist across server restarts
- [ ] `.biff` config parsed when present, graceful without it
- [ ] Quality gates pass: `uv run ruff check .`, `uv run ruff format --check .`,
      `uv run mypy src/ tests/`, `uv run pytest`
- [ ] 100% test coverage
- [ ] README updated with Phase 1 commands

### Task Breakdown (Beads)

| # | Task | Type | Priority | Depends On | Est LOC |
|---|------|------|----------|------------|---------|
| 1 | Data models (Message, UserSession, BiffConfig) | task | P1 | — | 150 |
| 2 | Storage layer (inbox.py, sessions.py) | task | P1 | 1 | 300 |
| 3 | .biff config parser | task | P2 | 1 | 100 |
| 4 | MCP server scaffold (server.py, transport modes) | task | P0 | 1 | 200 |
| 5 | Presence tools (set_plan, who, finger, biff_toggle) | task | P1 | 2, 4 | 200 |
| 6 | Messaging tools (send_message, check_messages) | task | P1 | 2, 4 | 250 |
| 7 | Message watcher + dynamic tool descriptions | task | P0 | 4, 6 | 200 |
| 8 | Spike: validate tools/list_changed in Claude Code | task | P0 | 4 | 50 |
| 9 | CLI entry point (__main__.py) | task | P2 | 4 | 100 |
| 10 | Integration tests + quality gates | task | P1 | 5, 6, 7 | 300 |

### Implementation Order

```
1. Models (no deps)
   |
   +---> 2. Storage (needs models)
   |        |
   |        +---> 5. Presence tools (needs storage + server)
   |        |
   |        +---> 6. Messaging tools (needs storage + server)
   |                  |
   +---> 4. Server scaffold (needs models)
   |        |
   |        +---> 7. Watcher + dynamic descriptions (needs server + messaging)
   |        |
   |        +---> 8. Spike: validate list_changed (needs server)
   |
   +---> 3. Config parser (needs models)
   |
   +---> 9. CLI (needs server)
   |
   +---> 10. Integration tests (needs everything)
```

### Acceptance Criteria Per Task

**Task 8 (Spike: validate list_changed)** is the critical gate:
- If `tools/list_changed` works: proceed with dynamic descriptions (tasks 7+)
- If it fails: pivot to hook+tool hybrid (add hook task, modify watcher)
- This task should be done early (after task 4) to derisk the architecture

## Four-Dimensional Evaluation

### Usability
- Commands follow Unix naming convention (intuitive for target audience)
- Zero configuration for solo use; `.biff` file for team use
- Claude sees unread counts automatically (no user action needed)

### Value
- Solo value at N=1 (`/plan` for self-documentation)
- Team value at N=2+ (async messaging without Slack)
- Solves the core problem: context switching kills flow

### Feasibility
- All components use proven patterns (FastMCP, JSONL, JSON)
- HTTP transport proven in production (Slack MCP)
- `tools/list_changed` documented as supported since v2.1.0
- Spike (task 8) validates the key assumption early

### Viability
- Maintainable: simple module structure, comprehensive tests
- Extensible: relay protocol designed for network upgrade
- No tech debt: follows all CLAUDE.md standards
- Aligned with Phase 2-4 roadmap
