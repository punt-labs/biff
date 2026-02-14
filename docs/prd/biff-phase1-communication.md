# PRD: Biff Phase 1 — Async Communication + Presence

**Status:** Draft (pending hive-mind consensus)
**Date:** 2026-02-14
**Origin:** Spike biff-6k7 findings + hive-mind expert analysis
**Bead:** biff-6k7 (spike), biff-4rg (Phase 1 core)

## Problem Statement

Engineers using AI coding tools context-switch to Slack/Discord for every
coordination need — destroying flow state that takes 15-25 minutes to recover.
Biff keeps communication where the code already lives: inside the terminal,
inside the Claude Code session.

Two critical feasibility assumptions were invalidated by the spike:
1. MCP `notifications/message` is silently dropped by Claude Code (#3174)
2. Subprocess terminal handoff is impossible via MCP stdio transport

However, investigating the Slack MCP integration revealed a viable alternative:
**HTTP/SSE transport + `notifications/tools/list_changed`** enables server-push
of tool metadata updates, including dynamic descriptions that show unread
message counts.

## Root Cause Analysis

The original Phase 1 design assumed stdio transport and server-push
notifications. Both are blocked. The root cause is that Claude Code treats
MCP servers as tool providers, not notification sources — `list_changed` is
the only server-push notification that triggers a client-side action.

The Slack MCP server (`mcp.slack.com/sse`) proves that remote MCP servers
with push capabilities work in production. Biff can follow the same pattern.

## Proposed Solution

### Architecture: HTTP Transport + Dynamic Tool Descriptions

```
                    Claude Code (CLI)
                         |
                    [HTTP transport]
                    localhost:8419
                         |
                  biff MCP server (FastMCP)
                    /        \
              local store    relay (WebSocket)
            ~/.biff/data/         |
                            biff relay service
                                  |
                          other biff instances
```

**Message notification flow:**
1. Message arrives at biff MCP server (via relay or local)
2. Server updates tool descriptions: `"Check messages"` becomes
   `"Check messages (3 unread: @kai about auth, @eric about lunch)"`
3. FastMCP automatically sends `notifications/tools/list_changed`
4. Claude Code refreshes tool list, sees updated description
5. On next user turn, Claude sees the unread summary in the tool list
6. Claude mentions it naturally or user calls `/check` explicitly

**Why this works:**
- `notifications/tools/list_changed` IS handled by Claude Code (since v2.1.0)
- FastMCP sends it automatically when tools are added/removed/modified
- HTTP transport provides persistent server-push channel
- No hook dependency (avoids bugs #13912, #12151)
- Proven pattern (Slack MCP uses identical transport)

**Fallback modes:**
- stdio transport for local-only mode (no push, explicit `/check` only)
- Hook injection as opt-in enhancement (if user wants it)

### Transport Modes

| Mode | Transport | Notification | Use Case |
|------|-----------|-------------|----------|
| **Relay** (default) | HTTP (`localhost:8419`) | Dynamic tool descriptions via `list_changed` | Team communication |
| **Local** | stdio | None (explicit `/check` only) | Solo dev, offline |

## User Stories

### P0 — Must Have

| ID | Story | Acceptance Criteria |
|----|-------|---------------------|
| US-1 | As an engineer, I can set my status so teammates know what I'm working on | `/plan "refactoring auth"` stores status; `/finger @me` shows it |
| US-2 | As an engineer, I can see who on my team is active | `/who` returns list of online teammates with plans and availability |
| US-3 | As an engineer, I can check a teammate's status without interrupting them | `/finger @kai` returns their plan, last-active time, availability |
| US-4 | As an engineer, I can control whether I receive messages | `/biff on` enables, `/biff off` queues messages silently |
| US-5 | As an engineer, I can send an async message to a teammate | `/mesg @kai "auth is ready for review"` delivers to their inbox |
| US-6 | As an engineer, I can read my messages | `/check` returns unread messages; Claude sees unread count in tool descriptions |
| US-7 | As an engineer, I'm aware of unread messages without explicit polling | Tool descriptions update to show unread count via `list_changed` |

### P1 — Important (deferred to Phase 2)

| ID | Story | Acceptance Criteria |
|----|-------|---------------------|
| US-8 | As an engineer, I can broadcast to my team | `/wall "deploying in 5 min"` reaches all active sessions |
| US-9 | As an engineer, I can share code artifacts | `/send @kai` shares current diff or file |
| US-10 | As an engineer, I can have a real-time conversation | `/talk @kai` starts bidirectional exchange |

### P2 — Nice to Have (deferred to Phase 3+)

| ID | Story | Acceptance Criteria |
|----|-------|---------------------|
| US-11 | As an engineer, I can create temporary groups | `/hive @kai @eric` creates ephemeral group |
| US-12 | As an engineer, I can invite someone to steer my session | `/pair @kai` with consent |

## Design Considerations

### Visual Style: Git-like, Not Slack-like

Message output uses horizontal rules, `@username` prefixes, `[HH:MM]`
timestamps. No emoji, no ASCII art. Semantic color only (green=success,
yellow=pending, red=error). Compatible with any terminal theme.

```
--------------------------------------------------
biff - 2 messages

@kai [14:32]
auth is ready for review

@eric [14:45]
lunch?
--------------------------------------------------
```

### Interaction Model: Pull with Smart Awareness

- **Tool descriptions provide ambient awareness** — Claude sees unread counts
  without any action from the user
- **Explicit commands retrieve content** — `/check` for messages, `/finger`
  for status
- **Claude can proactively mention** — "I see you have 2 unread messages.
  Want me to show them?"
- **No interruption** — messages queue; user decides when to read

### Notification Fatigue Prevention

- Tool descriptions only update when unread count changes (not on every poll)
- Description shows preview of first 1-2 messages, not full content
- No notification when unread count is 0 (tool description stays generic)

## Technical Considerations

### FastMCP HTTP Transport

- `mcp.run(transport="http", host="127.0.0.1", port=8419)` serves both
  Streamable HTTP (`/mcp`) and SSE (`/sse`) on same port
- Claude Code connects via `claude mcp add --transport http biff http://localhost:8419/mcp`
- FastMCP automatically sends `notifications/tools/list_changed` when tools
  are modified via `add_tool()`/`remove_tool()`

### Dynamic Tool Description Implementation

When messages arrive, biff replaces the `check_messages` tool with an updated
version whose description includes unread count and preview:

```python
# Pseudocode
async def on_message_received(message: Message) -> None:
    unread = await inbox.get_unread_summary()
    # Remove old tool, add new one with updated description
    # FastMCP sends notifications/tools/list_changed automatically
    server.update_tool_description(
        "check_messages",
        f"Check messages ({unread.count} unread: {unread.preview})"
    )
```

### Storage

- JSONL for messages (`~/.biff/data/inbox.jsonl`)
- JSON for presence/plans (`~/.biff/data/sessions.json`)
- Atomic writes (write to temp, rename)
- No database dependency

### Relay Architecture (Phase 1: local only)

- Phase 1 uses file-based local relay (same machine)
- Phase 2 adds WebSocket relay for cross-machine communication
- Relay protocol designed from day 1 for network upgrade

## Domain Considerations

### Unix Communication Tradition

Biff preserves the Unix philosophy:
- **Intent-driven**: clear, distinct verbs (`/mesg` is one-way, `/talk` is two-way)
- **Pull-based**: messages don't interrupt, they queue
- **Ephemeral**: messages vanish after reading (durable artifacts via `/send`)
- **Team-scoped**: `.biff` file is a whitelist, not a public directory
- **Local-first**: solo value at N=1 (`/plan` works without teammates)

### Agent-Native

Agents are first-class participants. An autonomous coding agent can `/mesg` a
human when it needs a decision. The `.biff` roster lists agents alongside
humans: `members = @kai @eric @agent:coder-1`.

## Success Metrics

| Metric | Target (Week 4) | Failure Threshold |
|--------|-----------------|-------------------|
| `tools/list_changed` delivery | >95% reliability | <80% triggers fallback to hook |
| Tool description refresh latency | <2s | >5s requires investigation |
| `/check` tool reliability | 100% | Any failure is a bug |
| Test coverage | 100% | Non-negotiable |
| Quality gates (ruff, mypy, pytest) | Zero violations | Non-negotiable |

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| `tools/list_changed` not refreshing tool descriptions in practice | Medium | High | Validate in spike; fallback to hook+tool hybrid |
| FastMCP `add_tool`/`remove_tool` during active session causes issues | Low | High | Test thoroughly; may need custom notification sending |
| Claude ignores updated tool descriptions | Medium | Medium | Test LLM behavior; adjust description format |
| HTTP transport adds complexity vs stdio | Low | Low | FastMCP handles transport abstraction |
| Adoption chicken-and-egg (needs teammates) | High | High | `/plan`, `/who`, `/finger` provide solo value |

## Out of Scope

- `/talk` (real-time bidirectional) — deferred to Phase 2
- `/wall`, `/hive` (broadcast/groups) — requires group model
- `/pair` (remote steering) — security model unproven
- E2E encryption — Phase 3
- Hosted relay service — Phase 4
- Persistent message history/search — against philosophy

## Open Questions

1. **Port selection**: Is 8419 (BIFF on phone keypad) acceptable as default?
2. **Relay protocol**: Design relay message format now for network upgrade later?
3. **Tool description format**: What format makes Claude most likely to mention
   unread messages? Needs empirical testing.
