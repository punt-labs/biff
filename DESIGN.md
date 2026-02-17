# Biff Design Decision Log

This file is the authoritative record of design decisions, prior approaches, and their outcomes. **Every design change must be logged here before implementation.**

## Rules

1. Before proposing ANY design change, consult this log for prior decisions on the same topic.
2. Do not revisit a settled decision without new evidence.
3. Log the decision, alternatives considered, and outcome.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                       Claude Code UI                         │
│                                                              │
│  ┌──────────────┐  ┌──────────────────┐  ┌───────────────┐  │
│  │  Tool Result  │  │ Assistant Output │  │  Status Line  │  │
│  │    Panel      │  │   (LLM emits)    │  │  (shell cmd)  │  │
│  └──────┬───────┘  └────────▲─────────┘  └───────▲───────┘  │
│         │                   │                     │          │
└─────────┼───────────────────┼─────────────────────┼──────────┘
          │                   │                     │
          │ updatedMCP        │ additionalContext    │ reads
          │ ToolOutput        │ (model emits this)   │ unread.json
          │                   │                     │
┌─────────┴───────────────────┴─────────────────────┴──────────┐
│                     PostToolUse Hook                          │
│                   (suppress-output.sh)                        │
│                                                              │
│  Silent commands (/write, /plan, /mesg):                     │
│    updatedMCPToolOutput = full result     (panel shows all)  │
│    additionalContext    = (none)          (LLM stays silent)  │
│                                                              │
│  Data commands (/who, /finger, /read):                       │
│    updatedMCPToolOutput = summary         (panel: "3 online")│
│    additionalContext    = full table      (LLM emits it)     │
└──────────────────────────▲───────────────────────────────────┘
                           │
                           │ raw tool response
                           │
┌──────────────────────────┴───────────────────────────────────┐
│                      MCP Server (biff)                        │
│                                                              │
│  Tools: write, read_messages, mesg, who, finger, plan, wall  │
│                                                              │
│  ┌─────────────────────┐    ┌────────────────────────────┐   │
│  │  Background Poller  │    │  Dynamic Tool Descriptions │   │
│  │  (poll_inbox, 2s)   ├───►│  read_messages: "2 unread" │   │
│  │                     │    │  + tools/list_changed notify│   │
│  └─────────┬───────────┘    └────────────────────────────┘   │
│            │                                                  │
│            │ poll relay                                       │
│  ┌─────────▼───────────────────────────────────────────────┐ │
│  │  Relay (Protocol)                                       │ │
│  │  ├── LocalRelay (filesystem: JSONL inboxes, JSON state) │ │
│  │  └── NatsRelay  (NATS KV sessions + JetStream inbox)   │ │
│  └─────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
```

## Key Interactions

### 1. Silent Command: `/write @kai "review the PR"`

```
User              Skill Prompt       MCP Tool        Hook              UI Panel
 │                  (write.md)        (write)    (suppress-output.sh)
 │  /write @kai     │                  │                │                │
 ├─────────────────►│  call write      │                │                │
 │                  ├─────────────────►│                │                │
 │                  │                  │── deliver ──►  │                │
 │                  │   "Sent to @kai" │                │                │
 │                  │◄─────────────────┤                │                │
 │                  │                  │                │                │
 │                  │            tool_response          │                │
 │                  │──────────────────────────────────►│                │
 │                  │                  │    updatedMCPToolOutput:        │
 │                  │                  │    "Sent to @kai"               │
 │                  │                  │    additionalContext: (none)    │
 │                  │                  │                ├───────────────►│
 │                  │                  │                │  "Sent to @kai"│
 │  (LLM silent)   │                  │                │                │
 │◄─ ─ ─ ─ ─ ─ ─ ─ ┤                  │                │                │
```

### 2. Data Command: `/who`

```
User              Skill Prompt       MCP Tool        Hook              UI Panel
 │                  (who.md)          (who)      (suppress-output.sh)
 │  /who            │                  │                │                │
 ├─────────────────►│  call who        │                │                │
 │                  ├─────────────────►│                │                │
 │                  │  "▶  NAME ..."   │                │                │
 │                  │◄─────────────────┤                │                │
 │                  │                  │                │                │
 │                  │            tool_response          │                │
 │                  │──────────────────────────────────►│                │
 │                  │                  │   updatedMCPToolOutput:         │
 │                  │                  │   "3 online"   │                │
 │                  │                  │   additionalContext:            │
 │                  │                  │   "▶  NAME ..."│                │
 │                  │                  │                ├───────────────►│
 │                  │                  │                │  "3 online"    │
 │                  │                  │                │                │
 │  LLM emits full  │◄──── additionalContext ──────────┤                │
 │  table verbatim  │                  │                │                │
 │◄─────────────────┤                  │                │                │
 │  ▶  NAME  S IDLE │                  │                │                │
 │    @kai   + 0m   │                  │                │                │
```

### 3. Push Notification: Message Arrives While Idle

```
                    Background         MCP Tool        Status Line
Sender              Poller           Description         Script
 │                  (poll_inbox)    (read_messages)    (unread.json)
 │                     │                │                  │
 │  /write @you ...    │                │                  │
 ├────── deliver ─────►│                │                  │
 │                     │                │                  │
 │              (2s poll cycle)         │                  │
 │                     │  count changed │                  │
 │                     ├───────────────►│                  │
 │                     │  description = │                  │
 │                     │  "1 unread:    │                  │
 │                     │   @sender ..." │                  │
 │                     │                │                  │
 │                     │  tools/list_changed               │
 │                     │  (belt: ctx._queue...             │
 │                     │   or suspenders:                  │
 │                     │   session.send...)                │
 │                     │────────────────────────►Claude Code refreshes
 │                     │                │       tool list; model sees
 │                     │                │       "1 unread" in description
 │                     │                │                  │
 │                     │  write unread.json                │
 │                     ├──────────────────────────────────►│
 │                     │                │        status line reads:
 │                     │                │        "biff(1)" in UI chrome
```

---

## DES-001: Plugin Hook Architecture — Display Path

**Date:** 2026-02-16
**Status:** SETTLED
**Topic:** How biff tool output reaches the user in Claude Code

### Design

Three-layer architecture:

1. **PostToolUse hook** (`suppress-output.sh`) — Runs after every biff MCP tool call.
   - Sets `updatedMCPToolOutput` (compact summary shown in tool-result panel).
   - Sets `additionalContext` (full output passed to the model as context).
2. **Skill command prompt** (e.g., `who.md`) — Instructs the LLM to call the tool and emit the additionalContext verbatim.
3. **MCP tool** — Returns the raw data.

### Why This Design

**The core problem:** If multi-line output goes into `updatedMCPToolOutput`, Claude Code truncates it and shows a "Control-O for more" expansion prompt. This is unacceptable for data commands — users should see their `/who` table immediately, not click to expand.

**The solution:** Split output into two channels:

- `/write`, `/mesg`, `/plan` need NO model output — the hook's `updatedMCPToolOutput` is sufficient (single-line confirmations). Their skill prompts say "Do not send any text after the tool call."
- `/who`, `/finger`, `/read` need the model to emit the full output because `updatedMCPToolOutput` shows only a summary (e.g., "3 online"). The full data travels via `additionalContext` and the skill prompt tells the model to emit it verbatim.

**This split represents 12-16 hours of iteration.** The hooks, status line, push notification, and display formatting are the fragile surface area of the system. The three-layer architecture exists precisely because neither `updatedMCPToolOutput` alone (truncation) nor model-only output (no panel summary) works.

### Solved: LLM Reformatting of Verbatim Output

The LLM sometimes reformatted `additionalContext` output (markdown tables, removed unicode, code fence boxes). This was a **prompt delivery timing problem**, not a prompt wording problem.

**Root cause:** Skill prompts arrive at the same turn as the tool call. The model receives and understands the formatting instructions (can repeat them verbatim when asked) but only follows them when biff-related context exists in a **prior** conversation turn. Same-turn delivery is insufficient.

**Solution:** Expand the MCP server `instructions` field (`app.py` → `FastMCP(instructions=...)`) to include formatting guidance. The MCP `instructions` field loads during the `initialize` handshake, before any tool calls. Claude Code injects it into the system context as prior context at session start — exactly the condition that makes formatting work. The skill prompt reinforces on the same turn; the instructions field primes on session start.

**Validation:** Fresh session, `/who` as first command, no prior biff conversation. Output rendered verbatim with unicode characters, no boxes. Tested 2026-02-16.

### Prior Approaches Tried (2026-02-16)

| Attempt | Mechanism | Outcome |
|---------|-----------|---------|
| v1 | Skill prompt: "Emit the full session table. Do not add commentary or code fences." | LLM reformats as markdown table with boxes |
| v2 | Skill prompt: "emit the tool output exactly as returned — character for character" | LLM still adds boxes |
| v3 | Skill prompt: added "Do not ... convert to markdown tables, or add boxes around the output." | LLM still adds boxes after reload+clear |
| v4 | Skill prompt: added "including the leading ▶ unicode character" | LLM still drops ▶ in some sessions |
| **v5** | **MCP `instructions` field with formatting guidance (prior-context delivery)** | **Works — verbatim output in fresh sessions** |

**What did NOT work:** Iterating on skill prompt wording. The delivery mechanism (same-turn) was the problem, not the words.

**What did work:** Moving formatting guidance to a mechanism that loads before the first tool call (MCP server `instructions` field).

### Rejected Approach: Collapsing to updatedMCPToolOutput Only

Attempted 2026-02-16 — putting full data in `updatedMCPToolOutput` and telling the model to emit nothing (matching `/write` pattern). **Rolled back immediately.** This changes the display architecture for data-emitting commands and was attempted without consulting prior design decisions or logging. The summary-in-panel + full-data-via-additionalContext split was deliberate.

---

## DES-002: Session Key Format

**Date:** 2026-02-16
**Status:** SETTLED
**Topic:** How sessions are identified

### Design

Session keys are composite `{user}:{tty}` strings. TTY is an 8-char hex random ID generated at server startup. This is the fundamental identifier throughout the stack — relay, storage, tools, tests.

- Broadcast: `/write @user` — delivers to all sessions of that user
- Targeted: `/write @user:tty` — delivers to one session
- Per-TTY inboxes: `inbox-{user}-{tty}.jsonl` (local) / NATS subjects per session

### Why

Supports multiple concurrent sessions per user (human + agents in same repo). The TTY metaphor maps cleanly to the Unix communication vocabulary.

### Rejected Alternative: Connection Port as TTY

The biff-2lc spike description proposed using the OS ephemeral port (NATS client connection port) as a natural TTY identifier. This was rejected in favor of an 8-char random hex ID generated by `secrets.token_hex(4)` at server startup.

**Why port was rejected:** Ports are transient OS resources, not stable identifiers. They change on reconnect, are not human-readable, and leak implementation details. A random hex ID is stable for the server lifetime, short enough to type in `/write @user:abc123de`, and decoupled from transport.

---

## DES-003: MCP Transport — stdio for Claude Code

**Date:** 2026-02-13
**Status:** SETTLED
**Topic:** Which MCP transport to use for Claude Code integration

### Design

Use **stdio transport** for the Claude Code MCP server. The `biff serve` command spawns as a subprocess with JSON-RPC over stdin/stdout.

### Spike Findings (biff-6k7)

The initial spike tested two questions:

1. **MCP notifications for push messages:** Do `notifications/message` render in Claude Code? **No.** Silently dropped (Claude Code issue #3174, closed not-planned).
2. **Subprocess handoff for /talk:** Can an MCP tool spawn an interactive PTY? **No.** stdio transport reserves stdout for JSON-RPC.

The spike then validated HTTP/SSE transport as an alternative and found `tools/list_changed` works over SSE — Claude Code re-reads the tool list when notified.

### Why stdio Won

Despite the spike recommending HTTP, stdio was adopted because:

- Claude Code's `claude mcp add` natively manages stdio servers (spawn, restart, lifecycle).
- HTTP requires the user to manage a long-running background process independently.
- The `tools/list_changed` notification works over stdio too — FastMCP sends it when tools are re-registered.
- The "push notification" use case (unread message alerts) was solved by **dynamic tool descriptions** + **status line file polling** instead of MCP notifications.

### Rejected: HTTP/SSE Transport

HTTP transport works technically but adds operational complexity: the user must start/stop the server, handle port conflicts, and ensure it runs before Claude Code connects. stdio eliminates this entirely.

---

## DES-004: Push Notifications — Dynamic Tool Descriptions + Status Line

**Date:** 2026-02-13
**Status:** SETTLED
**Topic:** How biff delivers "you have unread messages" without MCP push notification support

### The Constraint

MCP has **no push notification mechanism** that renders in Claude Code. The spike (biff-6k7) proved `notifications/message` is silently dropped (Claude Code issue #3174, closed not-planned). There is no way to push a visible alert to the user or the model from a background event.

### Design

Three complementary mechanisms work together:

1. **Dynamic tool description** — The `read_messages` tool description is mutated in-place to include the unread count and preview: `"Check messages (2 unread: @kai about auth, @eric about lunch). Marks all as read."` When the description changes, a `notifications/tools/list_changed` notification tells Claude Code to re-read the tool list. The model then sees the updated description and can proactively mention unread messages.

2. **Belt-and-suspenders notification firing** — `tools/list_changed` must be sent through two different code paths because MCP tool handlers and background tasks have different access to the session:
   - **Belt path** (inside a tool handler): `get_context()` returns the FastMCP Context. Call `ctx._queue_tool_list_changed()` to piggyback the notification on the tool response. Also captures `ctx.session` for the suspenders path.
   - **Suspenders path** (background poller): No request context exists. Uses the stored `ServerSession` reference captured from the last tool call. Calls `session.send_tool_list_changed()` directly. Best-effort — failures are logged, never raised.

3. **Status line file** — Background poller writes unread count to `~/.biff/unread/{repo-name}.json`. Claude Code's status line script (a shell command in `~/.claude/settings.json`) reads and displays it. This is the **human-visible** channel.

### Background Poller (`poll_inbox`)

Runs as an `asyncio.Task` for the lifetime of the MCP server (started in `app.py` lifespan). Polls the relay every 2 seconds. On count change, calls `refresh_read_messages()` which mutates the tool description and fires the notification.

The poller exists because messages can arrive at any time, but `tools/list_changed` can only be sent when the server has a session. The session reference is captured from the first tool call (belt path) and reused by the poller (suspenders path) thereafter.

### Why Three Mechanisms

| Mechanism | Audience | Trigger | Latency |
|-----------|----------|---------|---------|
| Dynamic tool description | Model | Every tool call + background poll | 0-2s |
| `tools/list_changed` notification | Model (via Claude Code refresh) | On description change | 0-2s |
| Status line file | Human | Background poll | 0-2s |

- The model cannot read the status line.
- The human cannot see tool descriptions without expanding them.
- Neither channel alone is sufficient. All three are needed.

### Spike Validation (biff-czx)

The spike validated end-to-end: `simulate_message` → remove/re-register tool → `tools/list_changed` → Claude Code refreshes → updated description visible to Claude.

### Key Bug Fixed (biff-10u)

The description mutation initially did **not** fire `tools/list_changed`. The tool description changed silently — Claude never re-read the tool list and never saw the updated count. Fixed in PR #21 by adding both notification paths. This was a subtle bug: the description was correct but invisible.

---

## DES-005: Command Vocabulary — Unix Ancestors

**Date:** 2026-02-15
**Status:** SETTLED
**Topic:** Mapping biff commands to their BSD/Unix ancestors

### Design

| MCP Tool | Slash Command | Unix Ancestor | Semantics |
|----------|---------------|---------------|-----------|
| `write` | `/write @user msg` | `write(1)` | Send a message |
| `read_messages` | `/read` | `from(1)` / `mail(1)` | Check inbox |
| `mesg` | `/mesg y` / `/mesg n` | `mesg(1)` | Control message reception |
| `finger` | `/finger @user` | `finger(1)` | Check user status |
| `who` | `/who` | `who(1)` / `w(1)` | List active sessions |
| `plan` | `/plan "msg"` | `.plan` file | Set working status |
| `wall` | `/wall msg` | `wall(1)` | Team broadcast |

### Prior Names (Renamed)

| Original | Renamed To | Why |
|----------|-----------|-----|
| `send_message` | `write` | `write(1)` is the Unix message-sending command |
| `biff` (on/off) | `mesg` | `mesg(1)` controls reception; `biff(1)` is mail notification (the product name) |
| `check_messages` | `read_messages` | Aligns with the read/inbox metaphor |
| `/on`, `/off` | `/mesg y`, `/mesg n` | BSD `mesg(1)` uses `y`/`n`, not `on`/`off` |

Implemented in biff-faz. Commit `3101515` (replace /on /off with /mesg y|n), PR #31 (full vocabulary alignment).

---

## DES-006: Relay Protocol — Local and NATS

**Date:** 2026-02-14
**Status:** SETTLED
**Topic:** How biff abstracts messaging and presence backends

### Design

`Relay` is a Python Protocol with two implementations:

1. **LocalRelay** — Filesystem-based. JSONL inboxes in `{data_dir}/`. JSON session files. Works out of the box with no infrastructure. Repo-scoped by directory path.
2. **NatsRelay** — NATS KV for sessions/presence, JetStream for messaging. Requires a NATS server. Enables cross-machine communication.

Selection is automatic: if `.biff` config has `relay_url`, use NatsRelay. Otherwise, LocalRelay.

### Message Semantics: POP (Read-Once)

Messages use POP semantics — read once, then deleted. NATS JetStream consumer with ack-and-delete. LocalRelay reads and truncates the JSONL file. No message history, no threads, no channels.

### Why POP

- Matches Unix `write(1)` / `mail(1)` — messages are ephemeral notifications, not a chat log.
- Simpler storage model — no pagination, no search, no retention policy.
- Privacy by default — messages don't persist after reading.

---

## DES-007: NATS Namespace Scoping

**Date:** 2026-02-16
**Status:** SETTLED
**Topic:** Isolating NATS resources per repository

### Design

All NATS resources include the repo name:

- KV bucket: `biff-{repo}-sessions`
- Stream: `BIFF_{repo}_INBOX`
- Subject prefix: `biff.{repo}.inbox`
- Client name: `biff-{repo}-{user}`

Repo name comes from `sanitize_repo_name(repo_root.name)` — alphanumeric, dash, underscore only. Fallback: `_default` when not in a git repo.

### Why

A single NATS server serves multiple repos (the normal deployment). Without scoping, sessions/messages/presence bleed across repo boundaries. LocalRelay was already scoped by directory path; NATS was not. Fixed in PR #33 (biff-jfu).

### Migration

No migration. Sessions rebuild on next heartbeat. Orphaned NATS resources (old `biff-sessions` bucket, `BIFF_INBOX` stream) expire via TTL or are manually purged. Acceptable pre-1.0.

---

## DES-008: Long-Lived Sessions with Idle Time

**Date:** 2026-02-15
**Status:** SETTLED
**Topic:** Session persistence and the meaning of "active"

### Design

Sessions persist for **30 days** (NATS KV TTL: 2,592,000s). The `/who` command shows an **IDLE column** instead of filtering out "stale" sessions.

```
▶  NAME       S   IDLE  PLAN
   @kai       +     0m  fixing auth
   @eric      -     3h  reviewing PR #42
```

Every biff tool call is a heartbeat — `update_current_session()` refreshes `last_active` before reading sessions.

### Prior Design (Rejected)

Sessions had aggressive TTLs: NATS KV 300s, application filter 120s. A user who hadn't called a biff tool in 2 minutes vanished from `/who`. `/finger` said "Never logged in" for known users with stale sessions. This made plan data inaccessible — the core bug in biff-a7p.

### Why 30 Days

- Unix `finger` read `.plan` from disk regardless of login state. Plans should persist.
- Users don't call biff tools every 2 minutes. Aggressive TTLs made everyone look offline.
- 30 days is long enough to never matter in practice, short enough for NATS to garbage-collect abandoned sessions.

---

## DES-009: Identity — GitHub Login

**Date:** 2026-02-15
**Status:** SETTLED
**Topic:** How biff determines the user's identity

### Design

Primary identity is the GitHub login, resolved via `gh api user --jq .login`. Resolution chain:

1. CLI `--user` override
2. GitHub login (via `gh` CLI, which uses stored OAuth token)
3. OS username (fallback)

The `GitHubIdentity` dataclass resolves both `login` and `display_name` in a single API call. Display name propagates to `UserSession` and appears in `/finger` output.

### Prior Design (Rejected)

Original design used `git config biff.user` as primary identity, with a `biff init` command that offered to persist to git config. Rejected because:

- A git config key is an extra setup step that users forget.
- GitHub login is the natural identity for a git-based communication tool.
- `gh auth` is already required for biff's other features.

---

## DES-010: Plugin Naming

**Date:** 2026-02-15
**Status:** SETTLED
**Topic:** Plugin name and command prefix in Claude Code

### Design

Plugin name: `biff`. Commands appear as `/biff:who`, `/biff:finger`, etc. Standalone short-form aliases (`/who`, `/finger`) are installed when no conflicts exist.

### Prior Name (Renamed)

Originally `biff-commands`. Changed because `/help` output was noisy — every line repeated "biff-commands" twice (once in path, once as prefix). Shortened to `biff` in PR #30 (biff-a68).

The `/biff:biff on|off` command (awkward after rename) was split into `/mesg y` and `/mesg n`.

---

## DES-011: Status Line — Per-Project Unread Counts

**Date:** 2026-02-15
**Status:** SETTLED
**Topic:** How unread message counts appear in Claude Code's status bar

### Design

Each biff server writes unread counts to `~/.biff/unread/{repo-name}.json`. The status line script scans all files in `~/.biff/unread/` and renders per-project segments: e.g., `biff(2) myapp(1)`.

The MCP server is registered **globally** in `~/.claude.json` (not per-project `.mcp.json`) so biff runs in every session and can update counts regardless of which project is open.

### Why Per-Project

A single global `unread.json` file caused whichever server wrote last to win. Multiple projects with different unread counts stomped each other.

---

## DES-012: Config File — .biff TOML

**Date:** 2026-02-14
**Status:** SETTLED
**Topic:** Per-repo configuration format

### Design

`.biff` file in repo root. TOML format. Contains:

- `relay_url` — NATS server URL (omit for local relay)
- `relay_auth_jwt` / `relay_auth_nkey` — NATS credentials
- `team` — list of usernames on this project
- Identity is resolved separately (DES-009), not stored in `.biff`.

### Why TOML

- Standard for Python tooling (`pyproject.toml`).
- Simpler than YAML for flat key-value config.
- `.biff` is a short, recognizable filename matching the product name.

---

## DES-013: Per-User Mailbox for Broadcast Messages

**Date:** 2026-02-16
**Status:** SETTLED
**Topic:** Fixing broadcast message delivery broken by multi-TTY sessions (PR #35)

### Problem

PR #35 introduced multi-TTY sessions (`{user}:{tty}` keys). This broke broadcast messaging in three ways:

1. **No offline delivery.** Broadcast `/write @user` looked up active sessions and dropped silently if none existed.
2. **Message duplication.** Each active session received its own copy — N sessions meant N copies of the same message.
3. **Orphaned messages.** Unread copies stranded in per-TTY inboxes when sessions exited.

### Design

Two mailbox types per user:

| Mailbox | File / Subject | Written By | Semantics |
|---------|---------------|------------|-----------|
| **User mailbox** | `userinbox-{user}.jsonl` / `biff.{repo}.inbox.{user}` | Broadcast `/write @user` | POP: first reader consumes |
| **TTY mailbox** | `inbox-{user}-{tty}.jsonl` / `biff.{repo}.inbox.{user}.{tty}` | Targeted `/write @user:tty` | POP: session-specific |

**Delivery rules:**
- Broadcast (`to_user` without `:`) → write to user mailbox. No session lookup. Persists offline.
- Targeted (`to_user` with `:`) → write to TTY mailbox. Same as before.

**Read rules:**
- `/read` merges both inboxes, sorted by timestamp.
- POP semantics apply independently to each mailbox.
- First session to `/read` consumes broadcast messages; other sessions see nothing.

### NATS Subject Safety

The user-level subject `biff.{repo}.inbox.{user}` (3 tokens) is distinct from the TTY-level subject `biff.{repo}.inbox.{user}.{tty}` (4 tokens). NATS exact-match filtering on the 3-token subject does not consume 4-token messages. The existing stream filter `biff.{repo}.inbox.>` covers both without config changes.

### Alternatives Considered

| Alternative | Rejected Because |
|-------------|-----------------|
| Keep fan-out, add offline queue | Two storage paths for the same message type; complexity for no benefit |
| Single user-level inbox for everything | Breaks targeted delivery semantics; `/write @user:tty` must only go to one session |
| Dedup at read time | Doesn't solve offline delivery; adds read-time complexity |

### Backward Compatibility

- Existing `inbox-{user}-{tty}.jsonl` files remain valid for targeted delivery.
- Old broadcast copies already in per-TTY inboxes will still be read normally.
- No migration needed.
- NATS stream config unchanged (`biff.{repo}.inbox.>` covers both).
