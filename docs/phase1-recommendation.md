# Phase 1 Architecture Recommendation

**Date**: 2026-02-13
**Status**: Ready for Implementation
**Source**: [architecture-analysis.md](./architecture-analysis.md)

## TL;DR

**Message RX**: Hybrid approach (Hook notification + Tool retrieval)
**Talk**: Tool-call loop (Claude-mediated, 1-2s latency)
**Timeline**: 4 weeks
**Risk Level**: Medium (primarily hook reliability)

## Core Decisions

### 1. Message Reception: Hybrid (C) ⭐

**Approach**: Lightweight hook for notification + reliable tool for retrieval

```
User receives message
  ↓
MCP server writes to:
  - ~/.biff/notifications.json (count only)
  - ~/.biff/inbox.jsonl (full messages)
  ↓
UserPromptSubmit hook reads count
  ↓
Claude sees: "You have 3 unread messages. Use /mesg check to read."
  ↓
User says: "check my messages"
  ↓
Claude calls: check_messages() tool
  ↓
Full message list returned
```

**Why:**
- Proactive when hooks work
- Reliable fallback when hooks fail
- Hook only handles count (low risk)
- Tool handles full retrieval (high reliability)

**Feasibility: 4/5** | **LOC: ~700** | **Timeline: 2 weeks**

### 2. Real-Time Talk: Tool-Call Loop (B) ⭐

**Approach**: Claude mediates the conversation via tool calls

```
User: /talk @alice
  ↓
MCP tool: talk_start("alice")
  ↓
User: "Hey, can you review my PR?"
  ↓
Claude calls: send_talk_message("alice", "Hey, can you review my PR?")
  ↓
Claude proactively calls: check_talk_reply("alice")
  ↓
Claude shows: "@alice replied: Sure, send me the link"
```

**Why:**
- Zero dependencies (no tmux required)
- Proven tool call path
- Works everywhere (macOS, Linux, Windows)
- Can add tmux real-time in Phase 2

**Latency**: 1-2 seconds per message (acceptable for Phase 1)

**Feasibility: 5/5** | **LOC: ~500** | **Timeline: 1 week**

## What We're NOT Doing (Yet)

### tmux Split-Pane (Phase 2)

Real-time talk via Textual TUI in tmux pane:
- Requires tmux installed and active
- More complex IPC (Unix sockets)
- Better UX but higher risk

**Decision**: Validate core patterns in Phase 1 first, add Phase 2 after proven

### Pure Hook Approach (Rejected)

Hook directly injects full message bodies:
- High risk: hook stdout bugs may garble messages
- No fallback if hooks break
- All-or-nothing reliability

**Decision**: Hybrid approach provides graceful degradation

### Server Push (Not Possible)

MCP notifications (`notifications/message`) rendered inline:
- **Confirmed bug**: Claude Code silently drops these notifications
- No workaround available

**Decision**: Hook + tool hybrid is only viable approach

## Implementation Plan

### Week 1: Infrastructure

**Deliverables:**
- `models.py`: Message, User, Session dataclasses
- `storage/inbox.py`: JSONL storage
- `storage/notifications.py`: count storage
- `relay/local.py`: same-machine relay
- Tests: 100% coverage

**LOC**: ~600 (400 code + 200 tests)

### Week 2: Message Tools

**Deliverables:**
- `tools/mesg.py`: send, check, mark_read, delete
- `hooks/user_prompt_submit.py`: lightweight notification
- MCP server integration
- Tests: tool + hook behavior

**LOC**: ~700 (500 code + 200 tests)

**Validation**: Hook reliability test (measure success rate)

### Week 3: Talk Tools

**Deliverables:**
- `tools/talk.py`: start, send, check_reply, end
- Session state management
- Prompt engineering for proactive checks
- Tests: session lifecycle

**LOC**: ~500 (350 code + 150 tests)

**Validation**: Latency test (measure round-trip time)

### Week 4: Discovery Tools

**Deliverables:**
- `tools/plan.py`: set_plan, get_plan
- `tools/finger.py`: finger_user
- `tools/who.py`: list_active_sessions
- `.biff` config parsing
- Documentation

**LOC**: ~450 (300 code + 150 tests)

**Total Phase 1: ~2,250 LOC** (1,550 code + 700 tests)

## Risk Mitigation

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Hook stdout bugs | High | Fallback to `/mesg check` tool |
| Claude doesn't proactively check replies | Medium | Prompt engineering + documentation |
| File corruption | Low | Atomic writes + validation |
| Stale session state | Medium | TTL cleanup + warnings |

## Exit Criteria

**Must have before Phase 2:**

- [ ] `/mesg @user "text"` sends message
- [ ] Incoming messages trigger notification (hook or manual)
- [ ] `/mesg check` retrieves full messages
- [ ] `/talk @user` starts conversation
- [ ] 5+ message exchange in talk session works
- [ ] `/plan "status"` sets plan
- [ ] `/finger @user` shows user plan
- [ ] `/who` lists active sessions
- [ ] Quality gates pass: ruff, mypy, pytest (100% coverage)
- [ ] Documentation complete

## Success Metrics

**Phase 1 is successful if:**

1. **Hook reliability ≥ 80%**: Notifications delivered in 4/5 prompts
2. **Talk latency ≤ 2s**: Message round-trip time acceptable
3. **Zero critical bugs**: No data loss, no crashes
4. **Test coverage = 100%**: All code paths tested
5. **User feedback positive**: Engineers find it useful

**If hook reliability < 80%**: Document as known limitation, emphasize `/mesg check` tool

**If talk latency > 3s**: Consider escalating tmux approach to Phase 1

## Phase 2 Preview

Once Phase 1 is validated:

1. **Real-time talk**: tmux split-pane with Textual TUI (~500 LOC)
2. **Network relay**: WebSocket relay for remote communication (~800 LOC)
3. **Team commands**: `/wall` broadcast, `/hive` groups (~300 LOC)
4. **Hook optimization**: More context if hooks prove reliable (~100 LOC)

**Phase 2 Timeline**: 3-4 weeks after Phase 1 complete

## Next Actions

1. **Validate hook behavior**:
   ```bash
   # Create test MCP server with UserPromptSubmit hook
   # Measure: notification delivery rate, stdout corruption rate
   ```

2. **Validate tool latency**:
   ```bash
   # Measure: tool call overhead (baseline for talk latency)
   ```

3. **Begin Phase 1A**: Implement storage layer and models

## Questions for Product/UX

1. **Hook notification format**: "You have 3 unread messages" vs "3 unread from @alice, @bob"?
2. **Talk exit UX**: Explicit `/talk end` vs inferred from context?
3. **Rate limiting**: Check for messages every 10th prompt? Configurable?
4. **Error messages**: How verbose? Show file paths or abstract errors?

## References

- Full analysis: [architecture-analysis.md](./architecture-analysis.md)
- Bead: [biff-6k7] Spike: MCP notification rendering and subprocess handoff
- Related beads: [biff-4rg] Phase 1: Core MCP server

---

**Recommendation**: Proceed with Hybrid Message RX + Tool-Call Loop Talk for Phase 1.

**Confidence**: High (4/5) — Well-understood constraints, proven tool call path, clear fallback strategy.
