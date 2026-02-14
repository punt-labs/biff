# UX Analysis: Messaging in MCP-Mediated Environments

**Author**: forge-ux (Claude Opus 4.6)
**Date**: 2026-02-13
**Context**: Biff is an MCP-native CLI communication tool. Claude Code mediates all interaction — the user types prompts, Claude processes them, and MCP tools are called by Claude on behalf of the user. There is no way for the MCP server to push visible notifications directly to the user.

---

## Executive Summary

Biff faces a **fundamental UX tension**: terminal messaging requires immediacy and low friction, but MCP introduces mandatory AI-mediated latency. The core trade-offs:

1. **Hook-triggered message injection** solves discoverability but risks notification fatigue.
2. **Explicit polling** (`/check`) gives users control but adds cognitive load.
3. **OS desktop notifications** break the "everything in Claude Code" promise.
4. **tmux split-pane /talk** delivers real-time UX but requires subprocess handoff.
5. **Tool-call loop /talk** stays in Claude Code but adds 1-2s per message.

**Recommendation**: Hybrid approach with **smart notification suppression**. Hook-triggered injection for new messages, explicit polling for history, OS notifications as opt-in fallback. For `/talk`, prioritize tmux split-pane — real-time matters more than ideological purity.

---

## 1. User Mental Model: How Engineers Expect Terminal Messaging to Work

### 1.1 Historical Context: The Unix Communication Vocabulary

Engineers who remember `talk`, `wall`, `finger`, and `mesg` expect:

- **Immediate visibility**: `talk` didn't wait for you to check — it split your terminal and drew a conversation window.
- **Minimal friction**: One command, one action. `write user` sent a message. Done.
- **Terminal-native**: No separate app, no browser, no OS notification center.
- **Human-in-the-loop by default**: You saw who was trying to talk to you before deciding to engage.

The original `talk` command interrupted your session **visually** but not **computationally**. You could ignore it, respond later, or engage immediately. The interruption was **purposeful and visible**, not a red badge or a sidebar.

### 1.2 Modern CLI UX Expectations

Engineers using tools like `gh`, `git`, `docker`, and `kubectl` expect:

- **Synchronous feedback**: `gh pr create` immediately shows you the URL.
- **Explicit invocation**: No automatic background polling. You run the command when you want the result.
- **Composability**: Output is parseable, pipeable, scriptable.

Biff inherits both mental models. From Unix: **interruption should be visible and purposeful**. From modern CLI: **commands should be explicit and synchronous**.

### 1.3 The AI-Mediated Paradigm Shift

Claude Code users have a **new mental model**: the terminal is a conversation, not a command line. They type natural language ("check my messages") and expect Claude to translate that into tool calls.

This creates two sub-populations:

1. **CLI-native users** who want `biff check` to behave like `git status`.
2. **Conversation-native users** who want to say "do I have any messages?" and have Claude handle the mechanics.

Biff must serve both. The MCP layer supports conversation-native users (Claude can call `/check` in response to "any messages?"). The CLI wrapper (`biff check`) supports CLI-native users.

**Key insight**: The MCP server is invisible infrastructure. The UX layer is the **slash commands in Claude Code** and the **CLI wrapper for terminal power users**.

---

## 2. Cognitive Load: Hook Injection on Every Prompt

### 2.1 The Proposal

On every user prompt submission, a hook checks for unread messages and injects context:

```
User: "refactor the auth module"
[Hook injects: "You have 3 unread messages from @kai"]
Claude sees: "You have 3 unread messages from @kai\n\nUser: refactor the auth module"
```

Claude can then mention the messages: "Before we start, you have 3 unread messages from @kai. Would you like to read them, or should I proceed with refactoring?"

### 2.2 Benefits

- **Zero user action required**: Messages are surfaced automatically.
- **No missed messages**: Every prompt submission checks for new messages.
- **Conversational UX**: Claude can intelligently decide when to mention messages based on context.

### 2.3 Risks

#### Notification Fatigue

If messages are injected on **every prompt**, users will experience:

- **Repetition**: "You have 0 unread messages" on every prompt is noise.
- **Interruption**: "You have 1 unread message" when the user is debugging a critical production issue is distracting.
- **Resentment**: Users will learn to ignore the injection, defeating the purpose.

#### Context Pollution

Every hook injection adds tokens to the conversation. Over a long session, repeated "0 unread" messages waste context window.

#### Timing Mismatch

Messages arrive asynchronously. If @kai sends a message between prompts, the user won't see it until the **next** prompt. This creates a lag that violates the "immediate visibility" mental model.

### 2.4 Mitigation: Smart Suppression

**Solution**: Only inject when there are **new unread messages since the last check**.

State tracking:

```python
last_checked_timestamp: datetime
unread_count_at_last_check: int

def should_inject() -> bool:
    current_unread = get_unread_count()
    if current_unread > unread_count_at_last_check:
        return True  # New messages arrived
    return False  # No change
```

Behavioral changes:

- **First prompt after new message**: "You have 1 new message from @kai"
- **Subsequent prompts**: No injection (user has been notified)
- **User explicitly reads messages**: Reset the counter, suppress future injections until new messages arrive

This reduces noise from O(prompts) to O(new messages).

### 2.5 Alternative: Explicit Polling

Instead of hook injection, require users to type `/check` or "check my messages" when they want to poll.

**Pros**:
- No notification fatigue.
- No context pollution.
- User retains full control.

**Cons**:
- Missed messages if user forgets to check.
- Higher cognitive load (remembering to check).
- Violates the "immediate visibility" mental model.

**Verdict**: Explicit polling is a **fallback** for users who disable hook injection. The default should be smart hook injection with suppression.

---

## 3. User Flow Efficiency: Steps to Send and Receive

### 3.1 Sending a Message

**Ideal flow**:

```
User: "/mesg @kai the auth module is ready"
Claude: [calls mesg tool]
Tool response: "Message sent to @kai"
Claude: "Message sent to @kai."
```

**Step count**: 1 user action (type the prompt), 1 tool call, 1 confirmation.

**Latency**: ~1-2 seconds (LLM processing + tool call + response).

**Comparison to Slack**:
- Slack: Open app (if not already open), find @kai, type message, hit enter. ~5-10 seconds if app is backgrounded.
- Terminal: `echo "..." | write kai` was instant (but required recipient to be on the same system).

**Verdict**: Biff's send flow is **acceptable**. The 1-2s latency is tolerable for asynchronous messaging.

### 3.2 Receiving a Message: Hook Injection

**Flow**:

```
[Message arrives from @kai]
User: "what's the status of the auth module?"
[Hook injects: "You have 1 new message from @kai: 'LGTM, approved'"]
Claude: "You have a new message from @kai: 'LGTM, approved'. As for the auth module status..."
```

**Step count**: 0 explicit user actions (automatic injection).

**Latency**: 0 (messages appear on next prompt).

**Verdict**: Efficient **if** smart suppression prevents spam.

### 3.3 Receiving a Message: Explicit Polling

**Flow**:

```
[Message arrives from @kai]
User: "check my messages"
Claude: [calls check tool]
Tool response: "1 unread from @kai: 'LGTM, approved'"
Claude: "You have 1 unread message from @kai: 'LGTM, approved'."
```

**Step count**: 1 user action (type "check my messages").

**Latency**: ~1-2 seconds.

**Verdict**: Acceptable for users who prefer control over automation.

### 3.4 Receiving a Message: OS Desktop Notification

**Flow**:

```
[Message arrives from @kai]
[macOS notification appears: "biff - @kai: LGTM, approved"]
User: [clicks notification, terminal window focuses]
User: "show me kai's message"
Claude: [calls check tool]
Tool response: "1 unread from @kai: 'LGTM, approved'"
Claude: "Kai's message: 'LGTM, approved'."
```

**Step count**: 2 user actions (click notification, type prompt).

**Latency**: ~1-2 seconds after user engagement.

**Pros**:
- Immediate visibility (doesn't wait for next prompt).
- Works even if Claude Code is backgrounded.
- Familiar pattern (matches Slack, email, etc.).

**Cons**:
- Breaks the "everything in Claude Code" promise.
- Requires OS-level notification permissions.
- Notification fatigue risk (every message triggers a system notification).

**Verdict**: OS notifications should be **opt-in**. Power users can enable them for critical messages (e.g., `/wall` broadcasts or messages from specific people).

---

## 4. Discoverability: How Does a New User Learn Biff's Commands?

### 4.1 The Bootstrap Problem

A new user installs biff (`pip install biff-mcp`) and opens Claude Code. Now what?

**Scenario 1: User knows to type `/who`**
→ They see the team roster.
→ They type `/mesg @kai "hello"`.
→ It works.

**Scenario 2: User has no idea biff exists**
→ They work for hours without ever discovering it.
→ Biff is invisible infrastructure.

### 4.2 Onboarding Strategies

#### Strategy A: Post-Install Welcome Message

After `pip install biff-mcp`, print:

```
✓ biff installed successfully

  To get started:
    1. Type '/who' in Claude Code to see your team
    2. Type '/mesg @username "your message"' to send a message
    3. Type '/plan "what you're working on"' to set your status

  Full command reference: https://github.com/punt-labs/biff#commands
```

**Pros**: Immediate guidance.
**Cons**: Users forget terminal output after the first run.

#### Strategy B: First-Run Prompt Injection

The first time the MCP server starts in a new session, inject:

```
[biff is now active. Type '/who' to see your team, or '/help' for a command reference.]
```

**Pros**: Shows up in the conversation where users will see it.
**Cons**: Intrusive if the user isn't interested in biff right now.

#### Strategy C: Slash Command Autocomplete

If Claude Code supports slash command autocomplete (it doesn't today, but might in the future), `/` followed by Tab shows available commands.

**Pros**: Discoverable without interrupting flow.
**Cons**: Requires host client support.

#### Strategy D: Help Command

`/help` or `/biff help` prints the command reference.

**Pros**: Standard pattern (matches `git help`, `gh help`, etc.).
**Cons**: Requires the user to know to ask for help.

### 4.3 Recommendation

**Layered discoverability**:

1. **Post-install welcome message** (printed to terminal).
2. **First-run prompt injection** (shows up once in Claude Code).
3. **`/help` command** (always available).
4. **README.md and docs** (external reference).

The first-run injection is the most effective because it appears in the conversation context.

---

## 5. Error States: Relay Down, User Offline, Message Delivery Failure

### 5.1 Relay Down

**Scenario**: The relay server is unreachable (network issue, server maintenance, etc.).

**Current behavior** (likely):

```
User: "/mesg @kai hello"
Claude: [calls mesg tool]
Tool response: {"error": "Relay unreachable"}
Claude: "I couldn't send the message. The relay server is unreachable."
```

**User expectation**: Terminal tools should fail **fast and loudly**. `curl example.com` doesn't wait 30 seconds to tell you the server is down.

**Recommendation**:

- **Timeout**: 5 seconds max for relay connection.
- **Error message**: Clear and actionable. "The relay server at wss://relay.example.com is unreachable. Check your network connection or contact your team admin."
- **Retry**: `biff retry` command to resend the last failed message.
- **Offline queue** (future): Store unsent messages locally and retry when the relay is reachable.

### 5.2 Recipient Offline

**Scenario**: @kai is not connected to the relay (their machine is off, their MCP server is not running, etc.).

**Behavior**: The relay stores the message and delivers it when @kai reconnects.

**User expectation**: This should be **invisible**. Email works this way — you send a message, it gets delivered whenever the recipient checks.

**Recommendation**:

- **Confirmation message**: "Message sent to @kai (will be delivered when they reconnect)."
- **Delivery receipts** (future): Optional. `/mesg @kai --receipt "message"` triggers a delivery confirmation when @kai reads the message.

### 5.3 Message Delivery Failure

**Scenario**: @kai's session ID is invalid (typo in username, user not on the team, etc.).

**Behavior**:

```
User: "/mesg @kaiiii hello"
Claude: [calls mesg tool]
Tool response: {"error": "User @kaiiii not found"}
Claude: "I couldn't find @kaiiii. Type '/who' to see your team roster."
```

**Recommendation**: Fuzzy matching. If the user types `@kaiiii`, suggest `@kai` ("Did you mean @kai?").

---

## 6. The /talk UX Gap: Real-Time Conversation Trade-Offs

### 6.1 The Core Problem

`/talk @kai` is **real-time bidirectional conversation**. Unlike `/mesg` (asynchronous, one-way), `/talk` requires low latency and continuous interaction.

**Unix `talk` UX**:

```
[Your terminal splits. Top half: your input. Bottom half: kai's input.]
You: "hey, can you review the auth PR?"
Kai: "sure, looking now"
You: "line 47 is the key change"
Kai: "gotcha, makes sense"
[You press Ctrl-C to exit]
```

**Latency**: ~50-200ms (local network) or ~200-500ms (internet).

**MCP-mediated `talk` UX**: Two approaches.

---

### 6.2 Approach A: Tool-Call Loop (Stay in Claude Code)

**Flow**:

```
User: "/talk @kai"
Claude: [calls talk_start tool]
Tool response: "Talk session started with @kai. He'll see a notification."
Claude: "I've started a talk session with @kai. Type your messages and I'll send them."

User: "hey, can you review the auth PR?"
Claude: [calls talk_send tool with message]
Tool response: "Message sent"
Claude: "Sent to @kai."

[1-2 seconds later]
Claude: [on next prompt, hook injects: "Kai replied: 'sure, looking now'"]
Claude: "Kai replied: 'sure, looking now'"

User: "line 47 is the key change"
Claude: [calls talk_send tool]
...
```

**Latency per message**: 1-2 seconds (LLM processing + tool call).

**Pros**:
- Stays in Claude Code (no subprocess handoff).
- Claude can add context ("Kai replied 30 seconds ago, he might be thinking").
- Conversation is logged in Claude Code history.

**Cons**:
- **1-2s latency per message is unacceptable for real-time conversation**. A 5-minute chat becomes 10 minutes.
- User types in natural language, Claude translates to tool calls — adds overhead and misinterpretation risk.
- No "typing indicator" (you don't know if @kai is composing a reply).

**Verdict**: This approach **kills the real-time UX**. It's better than nothing, but it's not `talk`.

---

### 6.3 Approach B: tmux Split-Pane Subprocess (Real Terminal I/O)

**Flow**:

```
User: "/talk @kai"
Claude: [calls talk tool]
Tool spawns a subprocess: `biff talk-tui @kai`
[Terminal splits — new pane appears with a Textual-based TUI]

┌─ biff talk: @kai ──────────────────┐
│ Kai: sure, looking now             │
│ You: line 47 is the key change     │
│ Kai: gotcha, makes sense           │
│                                    │
│ Type your message (Ctrl-C to exit)│
│ > |                                │
└────────────────────────────────────┘

[User types directly in the TUI, no LLM intermediation]
[Ctrl-C exits, returns to Claude Code]
```

**Latency per message**: 50-500ms (relay round-trip, no LLM).

**Pros**:
- **Real-time UX**. Typing feels like a chat app.
- No LLM intermediation — direct keyboard to relay to @kai's terminal.
- Typing indicators, presence ("@kai is typing...").
- Can support rich formatting (colors, bold, code blocks) via Textual.

**Cons**:
- **Requires subprocess handoff**. The MCP server must spawn an interactive process that takes over terminal I/O.
- **Breaks the "everything in Claude Code" paradigm**. The conversation happens outside Claude Code's awareness.
- **UX transition risk**: Switching from Claude Code (natural language) to TUI (raw typing) might feel jarring.

---

### 6.4 Technical Feasibility: Subprocess Handoff

**Question**: Can an MCP tool spawn an interactive subprocess that takes over terminal I/O, then return control to Claude Code cleanly?

**Answer** (from spike biff-6k7): **Yes, but with caveats.**

Mechanism:

1. MCP tool call (`/talk @kai`) returns immediately with: `{"session_id": "abc123", "tui_command": "biff talk-tui abc123"}`.
2. Claude Code tells the user: "Starting talk session. Run `biff talk-tui abc123` in a new terminal pane."
3. User runs the command in a split pane (tmux/screen/iTerm split).
4. TUI takes over that pane's I/O.
5. User exits TUI (Ctrl-C), pane closes, returns to Claude Code.

**Caveats**:

- **Manual split**: User must manually split their terminal. The MCP server cannot spawn a new pane directly (no access to terminal multiplexer API).
- **Session resumption**: If the user closes the TUI accidentally, they need the session ID to resume.

**Alternative** (if Claude Code supports it):

- Claude Code spawns the subprocess automatically and manages the split.
- Requires host client API support (doesn't exist today).

---

### 6.5 Recommendation: Hybrid Approach

**Default**: tmux split-pane subprocess for real-time UX.

**Fallback**: Tool-call loop for users who:
- Don't use tmux/screen/iTerm splits.
- Prefer everything in Claude Code (accept the latency trade-off).
- Are on a system where subprocess handoff doesn't work.

**Configuration**:

```bash
biff config set talk.mode tui  # Default
biff config set talk.mode loop # Fallback
```

**Why prioritize the subprocess approach?**

Because **latency kills real-time conversation**. The tool-call loop is a 5x slowdown. Users will hate it and stop using `/talk`. The subprocess approach delivers the UX that Unix `talk` users expect.

**Why keep the tool-call loop?**

Because some users can't or won't use terminal multiplexers. The fallback ensures `/talk` is always available, even if it's slower.

---

## 7. Notification Fatigue: Avoiding "0 Unread" Spam

### 7.1 The Problem

If hook injection fires on **every prompt**, users see:

```
User: "refactor the auth module"
Claude: "You have 0 unread messages. Let's refactor the auth module..."

User: "add a test for the login flow"
Claude: "You have 0 unread messages. I'll add a test for the login flow..."
```

This is **noise**. Users will learn to ignore it, and when they do have unread messages, they'll miss them.

### 7.2 Suppression Strategies

#### Strategy 1: Only Inject on State Change

Inject only when `unread_count` **increases**.

```python
if current_unread > last_known_unread:
    inject("You have {new_count} new messages")
    last_known_unread = current_unread
else:
    # No injection
    pass
```

**Result**: "0 unread" messages never appear.

#### Strategy 2: Throttle Injections

After a message is injected, suppress further injections for N prompts.

```python
prompts_since_last_injection = 0

def should_inject():
    if prompts_since_last_injection < 10:
        return False  # Throttle
    if current_unread > 0:
        prompts_since_last_injection = 0
        return True
    return False
```

**Result**: Messages appear at most once per 10 prompts.

**Risk**: User might miss messages if they send 10+ prompts without reading.

#### Strategy 3: Time-Based Suppression

Suppress injections for 5 minutes after the last injection.

```python
last_injection_time = None

def should_inject():
    if last_injection_time and (now - last_injection_time) < 5 * 60:
        return False  # Throttle
    if current_unread > 0:
        last_injection_time = now
        return True
    return False
```

**Result**: Messages appear at most once per 5 minutes.

**Risk**: User might miss urgent messages.

#### Strategy 4: Priority-Based Injection

Some messages are **urgent** (e.g., `/wall` broadcasts, messages from the team lead). Inject those immediately, throttle others.

```python
def should_inject(message):
    if message.priority == "urgent":
        return True  # Always inject
    if current_unread > last_known_unread:
        return True  # State change
    return False  # Throttle
```

**Result**: Urgent messages always appear, routine messages are throttled.

### 7.3 Recommendation

**Combination of Strategy 1 (state change) and Strategy 4 (priority)**:

- Inject only when `unread_count` increases.
- Always inject urgent messages (immediate override).
- Never inject "0 unread" messages.

Configuration:

```bash
biff config set notifications.mode auto     # Default: smart suppression
biff config set notifications.mode explicit # Fallback: no hook injection
biff config set notifications.urgent.always true  # Urgent messages bypass throttle
```

---

## 8. Comparative UX: Biff vs. Slack vs. Terminal `talk`

| Dimension | Terminal `talk` (1980s) | Slack (2024) | Biff (2026) |
|-----------|-------------------------|--------------|-------------|
| **Latency (send)** | Instant (~50ms) | ~500ms (app wake + network) | ~1-2s (LLM + tool call) |
| **Latency (receive)** | Instant (terminal split) | Instant (notification badge) | 0-2s (next prompt or OS notification) |
| **Context switch** | None (same terminal) | High (separate app) | Low (same session, or tmux split) |
| **Discoverability** | Low (man pages, word of mouth) | High (app onboarding, tooltips) | Medium (post-install message, `/help`) |
| **Notification fatigue** | None (one-to-one only) | High (channels, threads, reactions) | Low (smart suppression, pull-based) |
| **Real-time conversation** | Yes (split terminal) | Yes (WebSocket) | Partial (tmux split) or No (tool-call loop) |
| **Asynchronous messaging** | No (recipient must be online) | Yes (offline delivery) | Yes (relay stores messages) |
| **Team broadcasts** | Yes (`wall`) | Yes (channel @here) | Yes (`/wall`) |
| **Presence/status** | Yes (`who`, `finger`, `.plan`) | Yes (online/away/status) | Yes (`/who`, `/finger`, `/plan`) |
| **Code review integration** | No | No (separate PR comments) | Yes (`/cr` with context) |

**Key insight**: Biff is **closer to terminal `talk` than to Slack**, but the MCP layer adds unavoidable latency. The tmux split-pane approach recovers real-time UX for `/talk`, but loses it for other commands.

---

## 9. Recommendations: The Pragmatic UX Stack

### 9.1 Message Reception: Hybrid Approach

**Default**: Hook-triggered injection with smart suppression.

- Inject on `unread_count` increase.
- Suppress "0 unread" messages.
- Always inject urgent messages (`/wall`, priority senders).

**Opt-in**: OS desktop notifications.

- Configured per-team or per-sender.
- Example: "Notify me for all `/wall` broadcasts and messages from @boss."

**Always available**: Explicit polling.

- `/check` or "check my messages" triggers a manual poll.
- Useful for users who disable hook injection.

### 9.2 Real-Time Conversation: tmux Split-Pane First

**Default**: `/talk @kai` spawns `biff talk-tui` in a new terminal pane.

- Real-time latency (~50-500ms).
- Direct keyboard input (no LLM intermediation).
- Textual-based TUI with typing indicators, history, and rich formatting.

**Fallback**: Tool-call loop.

- Configured via `biff config set talk.mode loop`.
- Useful for users who don't use terminal multiplexers.
- Acceptable latency (~1-2s per message) for occasional use.

### 9.3 Discoverability: Layered Onboarding

1. **Post-install welcome message** (printed to terminal after `pip install biff-mcp`).
2. **First-run prompt injection** (appears once in Claude Code).
3. **`/help` command** (always available).
4. **README and docs** (external reference).

### 9.4 Error Handling: Fast Failure, Clear Messaging

- **Relay unreachable**: Fail within 5 seconds. Print actionable error message.
- **Recipient offline**: Transparent offline delivery (relay stores message).
- **Invalid recipient**: Fuzzy matching ("Did you mean @kai?").

### 9.5 Notification Fatigue: Smart Suppression

- Never inject "0 unread" messages.
- Inject only on state change (new unread messages).
- Always inject urgent messages.
- User can disable hook injection entirely (`biff config set notifications.mode explicit`).

---

## 10. Open Questions for Spike biff-6k7

The feasibility spike should answer:

1. **Can MCP notifications render visibly in Claude Code?**
   → Test: Create a minimal MCP server that fires a notification after a delay. Does it appear in the terminal?

2. **Can an MCP tool spawn an interactive subprocess that takes over terminal I/O?**
   → Test: MCP tool call spawns `biff talk-tui`. Does Claude Code resume cleanly after the TUI exits?

3. **What is the UX quality of the tmux split-pane transition?**
   → Test: Manually split terminal, run `biff talk-tui`, exit. Is it seamless or jarring?

4. **What is the latency of the tool-call loop approach?**
   → Test: Measure round-trip time for send → LLM → tool call → relay → response → LLM → user.

5. **Can OS desktop notifications be triggered from an MCP server?**
   → Test: Use `osascript` (macOS) or `notify-send` (Linux) from within an MCP tool call.

**Exit criteria**: Clear yes/no on each question, with evidence and latency measurements.

---

## 11. Conclusion

Biff's UX challenge is **fundamental**: MCP introduces AI-mediated latency that conflicts with the immediacy of terminal messaging. The solution is **pragmatic hybrid design**:

- **Hook injection with smart suppression** for message reception (no fatigue, no missed messages).
- **tmux split-pane subprocess** for `/talk` (real-time UX, no LLM overhead).
- **Explicit polling and OS notifications** as fallbacks (user control, familiar patterns).
- **Layered discoverability** (onboarding messages, `/help`, docs).
- **Fast failure and clear errors** (matches CLI mental model).

The **critical trade-off**: Real-time `/talk` requires breaking the "everything in Claude Code" paradigm. But **latency kills real-time conversation**, so the subprocess approach is the right default. The tool-call loop fallback ensures `/talk` works everywhere, even if it's slower.

**Validation needed**: Spike biff-6k7 must confirm that subprocess handoff is technically feasible and that the UX transition is acceptable. If subprocess handoff fails, the tool-call loop becomes the only option — and biff must accept that `/talk` will be slower than Unix `talk`.

---

## Appendix A: User Flows

### Flow 1: Sending a Message

```
User: "/mesg @kai the auth module is ready for review"
Claude: [calls mesg tool]
Tool response: {"status": "sent", "recipient": "@kai", "timestamp": "2026-02-13T14:32:00Z"}
Claude: "Message sent to @kai."
```

### Flow 2: Receiving a Message (Hook Injection)

```
[Message arrives from @kai: "LGTM, approved"]
User: "what's next?"
[Hook injects: "You have 1 new message from @kai: 'LGTM, approved'"]
Claude: "You have a new message from @kai: 'LGTM, approved'. For next steps..."
```

### Flow 3: Receiving a Message (Explicit Polling)

```
[Message arrives from @kai: "LGTM, approved"]
User: "check my messages"
Claude: [calls check tool]
Tool response: {"unread": [{"from": "@kai", "text": "LGTM, approved", "timestamp": "..."}]}
Claude: "You have 1 unread message from @kai: 'LGTM, approved'."
```

### Flow 4: Real-Time Conversation (tmux Split-Pane)

```
User: "/talk @kai"
Claude: [calls talk tool]
Tool response: {"session_id": "abc123", "command": "biff talk-tui abc123"}
Claude: "I've started a talk session with @kai. Run this in a new terminal pane:

    biff talk-tui abc123

Type Ctrl-C to exit."

[User splits terminal, runs command]
[Textual TUI appears with live conversation]
[User exits TUI, returns to Claude Code]
```

### Flow 5: Real-Time Conversation (Tool-Call Loop Fallback)

```
User: "/talk @kai"
Claude: [calls talk_start tool]
Tool response: {"session_id": "abc123"}
Claude: "Talk session started with @kai. Type your messages and I'll send them."

User: "hey, can you review the auth PR?"
Claude: [calls talk_send tool]
Tool response: {"status": "sent"}
Claude: "Sent to @kai."

[Hook injects: "Kai replied: 'sure, looking now'"]
Claude: "Kai replied: 'sure, looking now'"

User: "line 47 is the key change"
Claude: [calls talk_send tool]
...
```

### Flow 6: Checking Team Presence

```
User: "/who"
Claude: [calls who tool]
Tool response: {
  "online": ["@kai", "@eric"],
  "offline": ["@jim"],
  "last_seen": {"@jim": "2026-02-13T12:00:00Z"}
}
Claude: "Online: @kai, @eric. Offline: @jim (last seen 2 hours ago)."
```

### Flow 7: Setting Your Status

```
User: "/plan refactoring the auth layer"
Claude: [calls plan tool]
Tool response: {"status": "updated", "plan": "refactoring the auth layer"}
Claude: "Your plan is now: 'refactoring the auth layer'. Teammates can see it with /finger @you."
```

### Flow 8: Broadcasting to the Team

```
User: "/wall I'm deploying the auth changes to staging"
Claude: [calls wall tool]
Tool response: {"status": "sent", "recipients": ["@kai", "@eric", "@jim"]}
Claude: "Broadcast sent to @kai, @eric, @jim."
```

---

## Appendix B: Configuration Options

```bash
# Message reception mode
biff config set notifications.mode auto      # Hook injection (default)
biff config set notifications.mode explicit  # Explicit polling only
biff config set notifications.mode hybrid    # Hook + OS notifications

# OS notifications
biff config set notifications.os.enabled true
biff config set notifications.os.urgent_only true

# Real-time conversation mode
biff config set talk.mode tui   # tmux split-pane (default)
biff config set talk.mode loop  # Tool-call loop fallback

# Notification suppression
biff config set notifications.suppress_zero true  # Don't show "0 unread"
biff config set notifications.throttle 10         # Max 1 injection per 10 prompts
```

---

## Appendix C: Error Messages

### Relay Unreachable

```
Error: Cannot connect to relay server at wss://relay.example.com

Possible causes:
  - Network connectivity issue
  - Relay server is down
  - Incorrect relay URL in .biff file

To diagnose:
  1. Check your network connection
  2. Verify the relay URL: biff config get relay.url
  3. Contact your team admin

To retry: biff retry
```

### Recipient Not Found

```
Error: User @kaiiii not found

Did you mean @kai?

To see your team roster: /who
```

### Message Send Timeout

```
Error: Message to @kai timed out after 5 seconds

The relay server is reachable, but @kai did not acknowledge receipt.

Possible causes:
  - @kai is offline (message will be delivered when they reconnect)
  - Network latency is very high

To retry: biff retry
```

---

**End of UX Analysis**
