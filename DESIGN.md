# Biff Design Decision Log

This file is the authoritative record of design decisions, prior approaches, and their outcomes. **Every design change must be logged here before implementation.**

## Rules

1. Before proposing ANY design change, consult this log for prior decisions on the same topic.
2. Do not revisit a settled decision without new evidence.
3. Log the decision, alternatives considered, and outcome.

---

## System Architecture

```text
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

```text
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

```text
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

```text
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

### DES-007a: Slug-Based Namespace (2026-02-17)

**Problem:** `repo_root.name` is not globally unique. `punt-labs/biff` and `someone/biff` both resolve to `biff`, sharing sessions and messages on the demo relay.

**Fix:** Derive the namespace from `owner/repo` (git remote origin) instead of the bare directory name.

- `get_repo_slug(repo_root)` runs `git remote get-url origin` and parses the SSH or HTTPS URL to extract `owner/repo`.
- `_parse_repo_slug(url)` handles both `git@github.com:owner/repo.git` and `https://github.com/owner/repo.git`.
- `_parse_repo_slug(url)` handles scp-style SSH, `ssh://` scheme (with optional port), and HTTPS URLs.
- `sanitize_repo_name` gains `/` → `__` (double underscore) mapping. The double underscore is collision-resistant: `a_b/c` → `a_b__c` vs `a/b_c` → `a__b_c`.
- `load_config` prefers the slug; falls back to `repo_root.name` when no remote exists (local-only repos have no shared namespace collision risk).

**Examples:**

- `punt-labs/biff` → `punt-labs__biff`
- `punt-labs/socket.io` → `punt-labs__socket-io`

**Migration:** Same stance as DES-007 — sessions rebuild on next heartbeat (120s TTL), messages are consumed on read. Orphaned bare-name resources expire. Acceptable pre-1.0.

---

## DES-008: Long-Lived Sessions with Idle Time

**Date:** 2026-02-15
**Status:** SETTLED
**Topic:** Session persistence and the meaning of "active"

### Design

Sessions persist for **30 days** (NATS KV TTL: 2,592,000s). The `/who` command shows an **IDLE column** instead of filtering out "stale" sessions.

```text
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

## DES-011: Status Line — Per-Session Unread Counts via PPID

**Date:** 2026-02-15 (revised 2026-02-17, 2026-02-22)
**Status:** SETTLED
**Topic:** How unread message counts appear in Claude Code's status bar

### Design

Each biff MCP server writes its unread state to `~/.biff/unread/{key}.json`, where `key` is the topmost `claude` ancestor PID found by walking the process tree (`find_session_key()` in `src/biff/session_key.py`). The status line script reads the same path using the same function.

**Why this works:** Both the MCP server and the statusline command are descendants of the same root Claude Code process. Walking up the process tree to the topmost `claude` ancestor gives both a stable key regardless of intermediate child processes.

```text
Claude Code (PID 19147)
├── biff statusline                (status line, walks up → 19147)
└── claude (PID 57369, MCP manager)
    └── biff serve --transport stdio   (MCP server, walks up → 19147)
```

Falls back to `os.getppid()` when no `claude` ancestor exists (e.g. manual invocation, test environments).

The unread file contains user, repo, count, TTY name, and preview:

```json
{"user": "kai", "repo": "biff", "count": 2, "tty_name": "tty1", "preview": "@eric about auth"}
```

The status line renders: `kai:tty1(2)` (bold yellow when count > 0, plain `biff` when zero). The display uses the username as the primary label (useful for orientation across many tabs) and the TTY name for session identity.

The MCP server auto-assigns a `ttyN` name on startup (same sequential logic as the `/tty` tool), so the status bar always has session identity. Users can override via `/tty custom-name`.

The MCP server is registered **globally** in `~/.claude.json` (not per-project `.mcp.json`) so biff runs in every session and can update counts regardless of which project is open.

### `/mesg n` Suppression

When `biff_enabled=false` (set by `/mesg n`), the status line shows `user:tty(n)` instead of the actual unread count. This is purely visual — the mailbox continues to accumulate messages. `/mesg y` restores the real count immediately. The `biff_enabled` flag is persisted in the PPID-keyed unread JSON and read by the status line process.

### Cleanup

The MCP server deletes its PPID-keyed unread file in the lifespan `finally` block on shutdown. This prevents stale files from accumulating when sessions end.

### DES-011a: Ancestor Walk (2026-02-22)

**New evidence:** Claude Code now interposes an intermediate child process between the main process and MCP servers. The direct-parent PPID assumption from DES-011 no longer holds:

```text
57369 (claude, MCP manager)   ← MCP server parent (os.getppid() = 57369)
├── biff serve (prod)
└── biff serve (dev)
```

The statusline is spawned from a different level, so `os.getppid()` returns a different PID. MCP server writes to `57369.json`; statusline looks for a different PID — file not found, status bar shows bare `biff`.

**Fix:** `find_session_key()` in `src/biff/session_key.py` runs a single `ps -eo pid=,ppid=,comm=` call, parses the full process table into a dict, and walks upward from the current PID to find the topmost ancestor whose `comm` basename is `claude`. Both MCP server and statusline converge on the same root Claude Code PID regardless of intermediate processes.

- Safety bounded to 10 levels (process trees are shallow)
- Result cached per process lifetime (the ancestor PID never changes)
- Falls back to `os.getppid()` if `ps` fails or no `claude` ancestor found

**Verified (2026-02-22):** Live process tree shows `find_session_key()` returns 57369 (the claude process) while `os.getppid()` returns 67618 (a transient shell). Status bar correctly shows `jmf-pobox:tty5(0)` instead of bare `biff`.

### Prior Verification (2026-02-17)

Instrumented both the MCP server and status line to log their PPIDs. Confirmed match under the original direct-child model:

| Claude PID | MCP Server PPID | Status Line PPID | Match |
|-----------|----------------|-----------------|-------|
| 10869 | 10869 | 10869 | ✓ |
| 30757 | 30757 | 30757 | ✓ |

This verification is no longer sufficient — Claude Code's process tree changed after this test. DES-011a addresses the new topology.

Claude Code sends rich session JSON to the status line via stdin (including `session_id`, `cwd`, `session_name`), but this data is not available to the MCP server. The process tree walk is the only shared identifier between the two descendant processes.

### Prior Design: Per-Repo Files (Superseded)

The original design used `~/.biff/unread/{repo-name}.json` with the status line scanning all files. This had three problems:

1. **Multiple sessions stomped each other.** All MCP servers for the same repo wrote to the same file — last writer wins.
2. **No TTY identity.** The status line couldn't show which session had unread messages.
3. **Stale files accumulated.** No cleanup mechanism for dead servers' unread files.

### Alternatives Considered

| Alternative | Rejected Because |
|-------------|-----------------|
| Env var from Claude Code | Claude Code does not expose `session_id` as an env var to MCP server child processes |
| MCP initialize handshake | `clientInfo` contains only `name` and `version`, no session identity |
| Scan all per-TTY files | Status line wouldn't know which file is "mine" — showing all TTYs is noisy and unusable |
| Session ID from stdin JSON | Available to the status line but not to the MCP server — no way to agree on a file key |

### DES-011b: Local Plugin Process Tree Divergence (2026-02-25)

**New evidence:** The ancestor walk from DES-011a assumes the MCP server and status line are both descendants of the same `claude` process. This holds for **marketplace plugins** but breaks for **local plugins** installed via `biff install`.

**Marketplace plugin** — MCP server is a direct child of the session's `claude`:

```text
37600 (claude)                ← session UI + MCP manager (SAME process)
├── 37609 (biff serve)        ← MCP server: find_session_key() → 37600
├── 37859 (quarry serve)
├── /bin/zsh                  ← shells, status line: find_session_key() → 37600
└── ...                       ✓ CONVERGE on 37600
```

**Local plugin** — Claude Code spawns a separate `claude` process for local plugin MCP servers:

```text
81802 (-zsh)
├── 30234 (claude)            ← session UI, runs status line
│   ├── /bin/zsh              ← find_session_key() → 30234
│   ├── quarry serve
│   └── sourcekit-lsp
│
97746 (-zsh)
└── 27528 (claude)            ← separate MCP manager for local plugins
    └── 27538 (biff serve)    ← find_session_key() → 27528
                              ✗ DIVERGE: 30234 ≠ 27528
```

Biff writes to `27528.json`; status line looks for `30234.json`. File not found, status bar shows bare `biff`.

**Verified (2026-02-25):** Single session, all other sessions closed. Confirmed with live process tree walks from both sides. Uninstalling local plugin and installing marketplace version immediately fixed the status line — both sides converged on the same `claude` PID.

**Root cause:** Claude Code runs local plugin MCP servers under a separate `claude` process tree, not under the session's own `claude`. This is an architectural difference in how Claude Code manages local vs marketplace plugins. The ancestor walk cannot bridge two disjoint process trees.

**Impact:** The `biff install` local development workflow produces a broken status line. Marketplace installs work correctly. This means:

1. **Production users** (marketplace install) are unaffected
2. **Developers** testing local changes see bare `biff` instead of `user:tty(N)`
3. The dev/prod isolation strategy (`biff-dev` name) needs a different approach to status line testing

**Status:** OPEN — no fix yet. Potential approaches:

| Approach | Trade-off |
|----------|-----------|
| Write unread file keyed by session_key from NATS (not PID) | Status line can't resolve NATS session key without MCP call |
| MCP server writes PID mapping file on startup | Extra file, cleanup needed |
| Status line scans all unread files, picks freshest for this repo | Returns to the multi-session stomping problem from DES-011 |
| Accept marketplace-only status line during dev | Limits dev testing fidelity |

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

## DES-014: Column-Constrained Table Formatter

**Date:** 2026-02-23
**Status:** SETTLED
**Topic:** Shared 80-column table formatter for all tool output

### Problem

Tool output (`/who`, `/last`, `/read`) used ad-hoc formatting that broke when content — especially the PLAN column — exceeded terminal width. Long plans pushed columns off-screen or created ragged output. Each tool implemented its own formatting logic, duplicating alignment and header code.

### Design

One shared formatter in `_formatting.py` with two primitives:

- **`ColumnSpec`** — frozen dataclass defining header, min width, alignment, and whether the column is fixed or variable.
- **`format_table(specs, rows)`** — renders a constrained-width table with `▶` header prefix and `   ` row prefix (3-char indent).

**Layout algorithm:**

1. Fixed columns grow to fit their content (max of header, min_width, content).
2. Exactly one column per table is marked `fixed=False` (the variable column).
3. The variable column receives the remaining width budget: `80 - prefix - fixed_total - separators`.
4. Variable content that exceeds its budget wraps via `textwrap.wrap()`. Continuation lines indent to the variable column's start offset.

**80-character hard limit.** Biff output is consumed by LLMs in Claude Code, not resizable terminals. 80 columns is the product decision — wide enough for useful data, narrow enough to avoid context waste.

**ANSI awareness.** `visible_width()` strips ANSI escape sequences before measuring, so colored output doesn't inflate column widths.

**DIR truncation.** `last_component()` extracts the final path component (`/Users/kai/biff` → `biff`) to save horizontal space in the DIR column.

### Migration

All three tool modules migrated to shared formatter:

| Tool | Specs constant | Variable column |
|------|---------------|-----------------|
| `/who` | `_WHO_SPECS` | PLAN |
| `/last` | `_LAST_SPECS` | DURATION |
| `/read` | `_READ_SPECS` | MESSAGE |

### Alternatives Considered

| Alternative | Rejected Because |
|-------------|-----------------|
| Per-tool formatters | Duplicates alignment logic; bug fixes must be applied N times |
| Dynamic terminal width detection | LLM consumers have no terminal; fixed width is the correct abstraction |
| Multiple variable columns | Ambiguous budget allocation; one variable column keeps the algorithm simple and predictable |
| Truncation instead of wrapping | Loses information; wrapping preserves full content while constraining width |

## DES-015: Count-Only Unread Summary — Eliminate Poller Consumers

**Date:** 2026-02-23
**Status:** SETTLED
**Topic:** Removing message preview from unread summaries to eliminate steady-state consumers

### Problem

The background poller calls `get_unread_summary()` every 2 seconds. On `NatsRelay`, this used `_peek_subject()` to read actual message bodies and build a preview string (e.g., `"@kai about auth, @eric about lunch"`). Each peek created a durable consumer — 2 per active user (TTY inbox + user inbox). These were the last remaining consumers in the steady-state footprint after the delete-after-use fix on `fetch()`.

### Evidence

The preview was consumed in exactly **one place**: the `read_messages` tool description:

```text
Check messages (2 unread: @kai about auth, @eric about lunch). Marks all as read.
```

The status line does **not** use the preview — `statusline.py` has zero references to it. The `SessionUnread` dataclass omits the field. The unread JSON file wrote the preview but nothing read it.

### Design

Eliminate `_peek_subject()` and `build_unread_summary()`. The `UnreadSummary` model becomes count-only. `get_unread_summary()` uses `stream_info()` for counts (zero consumers) and returns `UnreadSummary(count=total)`.

Tool description becomes: `"Check messages (2 unread). Marks all as read."`

Steady-state consumer footprint drops to **zero per user**.

### Why This Is Sufficient

- `"N unread"` is sufficient signal — the count change triggers `tools/list_changed`, Claude sees the updated description, and proactively mentions it.
- When the user calls `/read`, they see the full messages with sender and body. The preview in the description was never the primary read path.
- The preview was never visible to humans (status line ignores it).

### Impact

| Metric | Before | After |
|--------|--------|-------|
| Steady-state consumers per user | 2 (poller peek) | 0 |
| Max concurrent users (500 limit) | ~250 | Unlimited by consumers |
| Poller operations | `stream_info` + `_peek_subject` | `stream_info` only |

### Alternatives Considered

| Alternative | Rejected Because |
|-------------|-----------------|
| Cache preview, reduce peek frequency | Still creates consumers; complexity for diminishing returns |
| Use stream direct-get API | Not available in nats.py client; would still require consumer-like operations |
| Keep preview on LocalRelay only | Inconsistent behavior across relays; dead code on the dominant deployment path |

## DES-016: Shared NATS Streams — Encryption-Aware Design

**Date:** 2026-02-24
**Status:** SETTLED
**Topic:** Consolidating per-repo NATS streams into shared infrastructure with extension points for E2E encryption (biff-lff)

### Problem

Per-repo NATS streams (DES-007) create 3 JetStream streams per repository:

| Resource | Name Pattern | Type |
|----------|-------------|------|
| Inbox stream | `biff-{repo}-inbox` | JetStream WORK_QUEUE |
| Sessions KV bucket | `biff-{repo}-sessions` | KV (internally `KV_biff-{repo}-sessions` stream) |
| Wtmp stream | `biff-{repo}-wtmp` | JetStream LIMITS |

Synadia Cloud R1 accounts are limited to 25 streams. At 3 per repo: **8 repos max**. This is fatal for any team working across more than a dozen projects.

Separately, the prfaq commits to E2E encryption (biff-lff) as a launch requirement. Encryption needs: public key distribution (via the same KV bucket being restructured), a team shared key slot, and a message envelope format that receivers can distinguish from plaintext. Designing shared streams without considering encryption means a second breaking migration when encryption ships.

### Design

Consolidate to 3 shared streams with subject-based repo isolation. Reserve KV key namespaces and model fields for encryption.

**Shared resource names:**

| Resource | Current (per-repo) | New (shared) |
|----------|-------------------|-------------|
| Inbox stream | `biff-{repo}-inbox` | `biff-inbox` |
| Sessions KV | `biff-{repo}-sessions` | `biff-sessions` |
| Wtmp stream | `biff-{repo}-wtmp` | `biff-wtmp` |

**Stream count: 3 total regardless of repo count** (down from 3N). At 25-stream limit, this leaves 22 streams for other JetStream usage.

**Subject structure — unchanged:**

```text
biff.{repo}.inbox.{user}          # broadcast delivery (3 tokens)
biff.{repo}.inbox.{user}.{tty}    # targeted delivery (4 tokens)
biff.{repo}.wtmp.{user}           # session event log
```

The repo token is already present in every subject. Routing isolation is encoded in the subject hierarchy, not the stream name. Moving the repo discriminator out of the stream name and into subject filtering is the entire change.

**Stream subject filters:**

| Stream | Filter | Rationale |
|--------|--------|-----------|
| `biff-inbox` | `biff.*.inbox.>` | `*` matches exactly one token (the repo). Mutually exclusive with wtmp subjects. |
| `biff-wtmp` | `biff.*.wtmp.>` | Same pattern. `inbox` and `wtmp` are distinct second-level tokens — no overlap. |

Using `biff.*.inbox.>` (narrow) rather than `biff.>` (broad). Two streams cannot both claim the same subject in NATS. Narrow filters partition the namespace cleanly and prevent the inbox stream from accidentally capturing wtmp subjects or future subject trees.

### KV Key Namespace

The shared `biff-sessions` KV bucket holds data for all repos. Keys must be repo-prefixed to avoid collisions.

**Key format:**

| Purpose | Key Pattern | Example |
|---------|-------------|---------|
| Session presence | `{repo}.{user}.{tty}` | `punt-labs__biff.kai.a1b2c3d4` |
| Wall broadcast | `{repo}.wall` | `punt-labs__biff.wall` |
| *Reserved: public keys* | `{repo}.key.{user}` | `punt-labs__biff.key.kai` |
| *Reserved: team key* | `{repo}.team-key` | `punt-labs__biff.team-key` |

The `key.{user}` and `team-key` namespaces are reserved for biff-lff. They are documented here so that the encryption implementation drops into reserved slots without a KV schema migration.

**`get_sessions()` filtering:** The KV stream's internal subject format is `$KV.biff-sessions.{key}`. To query only one repo's sessions, use `subjects_filter=$KV.biff-sessions.{repo}.>`. This returns all keys starting with the repo prefix (sessions, wall, and eventually keys). The caller strips the prefix and parses the remainder. Non-session keys (`wall`, `key.*`, `team-key`) are distinguished by structure and skipped.

### Consumer Name Scoping

WORK_QUEUE retention allows one consumer per filter subject. In a shared stream, consumer names must include the repo to avoid collisions between repos with the same username.

| Consumer | Current | New |
|----------|---------|-----|
| TTY inbox fetch | `inbox-{user}-{tty}` | `{repo}-inbox-{user}-{tty}` |
| User inbox fetch | `userinbox-{user}` | `{repo}-userinbox-{user}` |
| Wtmp peek | `wtmp-peek-{name}` | `{repo}-wtmp-peek-{uuid}` — UUID suffix avoids collision between concurrent sessions of the same user |

Consumer name length limit is 256 characters. With repo slugs like `punt-labs__biff` (16 chars) plus user/tty, the combined name is well within limits.

### Encryption Extension Points

These are not implemented in DES-016. They are design reservations that biff-lff will fill.

**1. `UserSession.public_key` field:**

```python
public_key: str = ""  # Base64-encoded Curve25519 public key; empty = no encryption
```

Default empty string. Zero behavior change. The session KV entry already carries presence data; adding the public key means one KV read reveals both presence and key material. When lff ships, senders look up the recipient's public key from their session to encrypt with NaCl Box.

**2. `Message` encryption envelope:**

```python
encrypted: bool = False        # True when body contains ciphertext
nonce: str = ""                # Base64-encoded 24-byte nonce
sender_pubkey: str = ""        # Base64-encoded Curve25519 public key
encryption_mode: str = ""      # "box" | "secretbox" | ""
```

All default to empty/false. Existing clients produce `encrypted=False` messages. When lff ships, encrypted messages set `encrypted=True` and populate the envelope. Receivers that see `encrypted=True` but lack decryption capability skip the message gracefully instead of crashing on `ValidationError`.

The relay never inspects `body` — it publishes `message.model_dump_json().encode()` as opaque bytes (line 377 of `nats_relay.py`). Encryption changes what goes *into* the body, not how the relay routes it.

**3. KV key reservations:**

- `{repo}.key.{user}` — public key (32 bytes Curve25519, base64 = 44 chars)
- `{repo}.team-key` — team symmetric key (encrypted per-member, NaCl Box wrapped)

These are documented above in the key namespace table.

### Why Encryption-Aware

The code reviewer's position — that encryption only changes the message payload and is therefore decoupled from stream architecture — is technically correct about code paths. But it misses the product reality:

1. **The shared relay carries plaintext by default.** Consolidating from per-repo to shared streams makes this worse: all repos' plaintext messages flow through a single pipe. E2E encryption is the mechanism that makes shared infrastructure acceptable for a communication tool.

2. **The prfaq says encryption is a launch requirement** (stated six times in `prfaq.tex`). Designing shared infrastructure without it is designing a temporary architecture.

3. **The KV bucket is the natural key distribution mechanism.** The session entries that wg4 restructures are the same entries that lff needs for public keys. Getting the key format right once avoids a second migration.

4. **The cost is marginal.** Three reserved key patterns in a documentation table, one field on `UserSession` (empty default), four fields on `Message` (empty/false defaults). No PyNaCl dependency. No encryption code. No key generation. Approximately 3 hours of additional design, 1-2 hours of implementation.

5. **Private servers become the upgrade path.** Shared streams + E2E encryption makes the free demo relay defensible ("your messages are encrypted, the relay is blind"). Teams that want full infrastructure isolation — no shared pipes, no noisy-neighbor risk, no blast-radius concerns — upgrade to a private NATS server. This is a coherent product tier: free shared relay (encrypted) vs. paid private relay (isolated + encrypted).

### Stream Limits

Shared streams accumulate data from all repos. Limits must be raised proportionally.

| Config | Current (per-repo) | New (shared) | Rationale |
|--------|-------------------|-------------|-----------|
| `_STREAM_MAX_BYTES` (inbox) | 10 MiB | 100 MiB | ~10 repos × 10 MiB. POP semantics mean messages are consumed immediately; this is a safety buffer, not expected steady state. |
| `_KV_MAX_BYTES` (sessions) | 1 MiB | 10 MiB | ~10 repos × 1 MiB. Session blobs are small (~500 bytes). |
| `_STREAM_MAX_BYTES` (wtmp) | 10 MiB | 100 MiB | 30-day retention × N repos. |
| `_KV_TTL` | 259,200s (3 days) | 259,200s (3 days) | Unchanged. See TTL note below. |

**TTL note:** DES-008 says 30-day session TTL (2,592,000s) but the code has 259,200s (3 days). This discrepancy predates DES-016. When biff-lff ships, public keys in the same KV bucket will need the heartbeat refresh cadence to prevent TTL expiry. The current 3-day TTL is sufficient for sessions (heartbeats every 60s refresh the key). Public keys can use the same refresh mechanism — the `update_session()` call that refreshes `last_active` will also refresh the KV TTL on the session entry, and lff can refresh the key entry on the same cadence.

### KV Watcher Fan-Out

The wtmp watcher (`_wtmp_watcher_loop` in `app.py`) uses `kv.watchall()` on the sessions bucket. With a shared bucket, every repo's session updates flow through every watcher instance.

**Scale estimate:** 243 users × 60s heartbeats ÷ 60s = ~4 KV updates/second across the bucket. Each watcher processes entries synchronously in the async iterator. At 4/s, this is negligible.

**Repo filtering:** `_kv_key_to_session_key()` (line 139 of `app.py`) currently parses `user.tty` from a KV key with `split(".", maxsplit=1)`. After DES-016, keys are `{repo}.{user}.{tty}`. The function changes to `split(".", maxsplit=2)`, verifies the first token matches `state.config.repo_name`, and returns `None` for other repos' entries. Non-session keys (`wall`, `key.*`, `team-key`) are also filtered out by structure (wrong token count or discriminator).

### Stream Provisioning: Idempotent, Not Delete-and-Recreate

The current `BadRequestError` handling (lines 155-160 and 172-175 of `nats_relay.py`) deletes and recreates on config mismatch. With shared streams, delete-and-recreate is catastrophic — it destroys all repos' data.

**New provisioning logic:**

1. `js.add_stream(config)` — creates if not exists, no-op if config matches.
2. On `BadRequestError`: call `js.update_stream(config)` to update mutable fields (e.g., `max_bytes`).
3. If update also fails: log a warning and continue with the existing stream. Do not delete.
4. Same pattern for KV: `js.create_key_value(config)` then update on mismatch.

No single relay instance has the authority to delete shared infrastructure.

### Scoped Purge

`purge_data()` (line 241) currently calls `js.purge_stream(name)` which purges the entire stream. With shared streams, this destroys all repos' data.

**New purge logic — subject-filtered:**

```python
# Inbox: purge only this repo's messages
await js.purge_stream("biff-inbox", subject=f"biff.{repo}.inbox.>")

# KV: purge only this repo's keys
await js.purge_stream("KV_biff-sessions", subject=f"$KV.biff-sessions.{repo}.>")

# Wtmp: purge only this repo's events
await js.purge_stream("biff-wtmp", subject=f"biff.{repo}.wtmp.>")
```

The nats.py `purge_stream()` accepts a `subject` parameter for filtered purge (verified: signature is `(self, name, seq=None, subject=None, keep=None) -> bool`).

### `delete_infrastructure()` Is Lethal — Becomes Scoped Purge

`delete_infrastructure()` (line 263) deletes the KV bucket and stream entirely. With shared streams, calling this from any relay instance destroys all repos' data. This method is called only by test fixtures.

**Change:** `delete_infrastructure()` becomes `purge_repo_data()` — identical to the scoped `purge_data()` above. It does not delete the shared streams. Test fixtures that need full cleanup (local `nats-server` subprocess tests) can call `delete_infrastructure()` on a separate code path, but the default cleanup is always scoped.

### Migration

**Sessions:** Rebuild on next heartbeat. Old per-repo KV buckets (`biff-{repo}-sessions`) become orphans. The new shared bucket starts empty; `update_current_session()` at server startup populates it immediately. No session data is lost — sessions are transient presence records.

**Messages:** POP semantics. Unread messages in old per-repo streams (`biff-{repo}-inbox`) are permanently lost. This is acceptable: messages are ephemeral notifications, not a chat log. The prfaq and DES-006 both commit to this.

**Orphaned streams:** Old per-repo streams continue to exist on the NATS server but are no longer written to or read from. They count against the 25-stream limit until manually deleted. On startup, after provisioning shared streams, attempt to delete old per-repo streams with `suppress(NotFoundError)`:

```python
# Migration cleanup — remove orphaned per-repo streams
for old_name in [f"biff-{repo}-inbox", f"biff-{repo}-wtmp"]:
    with suppress(NotFoundError):
        await js.delete_stream(old_name)
with suppress(NotFoundError):
    await js.delete_key_value(f"biff-{repo}-sessions")
```

This runs once per server startup. After the old streams are deleted, the suppress catches `NotFoundError` on subsequent startups.

**No migration flag.** A `.biff` config flag like `stream_mode = "shared"` was considered for incremental rollout. Rejected because: (a) the stream layout is an implementation detail, not a user-facing config; (b) maintaining two code paths (per-repo + shared) doubles the surface area; (c) pre-1.0, breaking changes are acceptable.

### Noisy-Neighbor Risk

Per-repo streams were natural failure domain boundaries. Shared streams mean one repo's problem is every repo's problem:

- **Message volume:** A chatty repo could fill the 100 MiB shared inbox. Mitigated by POP semantics — messages are consumed immediately, so steady-state volume is low.
- **KV operations:** High-frequency heartbeats from one repo increase KV churn for all watchers. Mitigated by 60s heartbeat interval and repo-prefix filtering in the watcher.
- **Stream corruption:** A corrupt shared stream affects all repos. No mitigation beyond NATS server reliability.
- **Accidental purge:** `purge_data()` is scoped by subject filter, but operator error (manual NATS CLI purge) could hit the shared stream. Mitigated by documentation and `biff doctor` checks.

**This is the fundamental tradeoff of consolidation.** It is the right call given the 25-stream limit. Teams that want full isolation upgrade to a private NATS server — this is the product tier that makes the tradeoff explicit.

### Impact

| Metric | Before (per-repo) | After (shared) |
|--------|-------------------|----------------|
| JetStream streams at 8 repos | 24 | 3 |
| Max repos per account (25 limit) | 8 | Unlimited by streams |
| Consumer names | `inbox-{user}-{tty}` | `{repo}-inbox-{user}-{tty}` |
| KV keys | `{user}.{tty}` | `{repo}.{user}.{tty}` |
| Purge scope | Full stream | Subject-filtered |
| Provisioning | Delete-and-recreate | Idempotent create-or-update |

### Files Changed

| File | Changes |
|------|---------|
| `src/biff/nats_relay.py` | Shared stream constants, repo-prefixed KV keys, wall key method, consumer name prefixes, subject filters, scoped purge, idempotent provisioning, migration cleanup |
| `src/biff/models.py` | `UserSession.public_key` field, `Message` encryption envelope fields (all empty defaults) |
| `src/biff/server/app.py` | `_kv_key_to_session_key()` parses `{repo}.{user}.{tty}`, filters non-matching repos |
| `tests/test_nats/conftest.py` | Cleanup uses `purge_data()` instead of `delete_infrastructure()` |
| `tests/test_nats_e2e/conftest.py` | Same cleanup change |
| `tests/test_hosted_nats/test_stress.py` | Updated stream/bucket constants, consumer name patterns, KV key queries |

### Stream Namespace Isolation (addendum, 2026-02-24)

The `NatsRelay` constructor accepts a `stream_prefix` parameter (default `"biff"`).  Stream names, subject patterns, and subject prefixes all derive from it:

| `stream_prefix` | Inbox stream | Sessions KV | Wtmp stream | Subject pattern |
|-----------------|-------------|-------------|-------------|-----------------|
| `"biff"` (default) | `biff-inbox` | `biff-sessions` | `biff-wtmp` | `biff.{repo}.inbox.>` |
| `"biff-dev"` (tests) | `biff-dev-inbox` | `biff-dev-sessions` | `biff-dev-wtmp` | `biff-dev.{repo}.inbox.>` |

**Motivation:** Hosted NATS tests must not touch production streams.  Before this change, tests and production shared the same `biff-*` streams, and legacy stream cleanup caused subject overlap errors against Synadia Cloud.  With `stream_prefix="biff-dev"`, test streams are fully isolated — they can be created and destroyed without affecting production data.

**Cost:** One constructor parameter, no production behavior change (default is `"biff"`).

### Wtmp Schema Versioning (addendum, 2026-02-24)

`SessionEvent` now carries a `version: int = 1` field.  This enables forward-compatible schema evolution for the durable wtmp stream.

**Why wtmp needs versioning but inbox does not:**

- **Inbox** (WORK_QUEUE): Messages are consumed within seconds.  A schema change means old messages are gone before the new code reads them.  Purge-and-cutover is sufficient.
- **Wtmp** (LIMITS, 30-day retention): Session history persists.  A breaking schema change would leave historical records that new code cannot deserialize.

**Deserialization strategy:**

```python
event = SessionEvent.model_validate_json(raw.data)
if event.version == 1:
    events.append(event)
else:
    logger.debug("Skipping wtmp v%d (unsupported)", event.version)
```

Records without a `version` field (pre-v1) deserialize with `version=1` (Pydantic default).  Unrecognized versions are skipped with a debug log, not a crash.  When v2 ships, the reader adds a second branch; v1 records remain readable for their 30-day retention lifetime.

### Resilient Consumer Cleanup (addendum, 2026-02-24)

`delete_session()` now suppresses `TimeoutError` and `NatsError` (in addition to `NotFoundError`) when deleting the per-session inbox consumer.  Against hosted NATS under sustained load, the 5-second default timeout for `js.delete_consumer()` can expire.  The consumer's `inactive_threshold` (5 minutes) is the safety net — it auto-expires regardless of whether the explicit delete succeeds.

### Alternatives Considered

| Alternative | Rejected Because |
|-------------|-----------------|
| Ship wg4 without encryption awareness | Second breaking migration when lff ships. KV key namespace collisions. Message format incompatibility. Costs ~3 hours to avoid. |
| Ship wg4 + lff together | 3-4x implementation cost. Blocks P1 stream fix for months. Encryption requires PyNaCl, key generation, key distribution protocol, trust model. |
| `biff.>` as shared stream filter | Two streams cannot both claim `biff.>`. Narrow filters (`biff.*.inbox.>`, `biff.*.wtmp.>`) partition cleanly. |
| `.biff` config flag for migration | Two code paths doubles surface area. Pre-1.0 breaking changes are acceptable. |
| Separate KV bucket for encryption keys | Burns another stream slot, defeating wg4's purpose. Session entries are the natural home for public keys. |

## DES-017: Hook Integration Architecture — biff-* Lifecycle Hooks

**Date:** 2026-02-24
**Status:** SETTLED (architecture), OPEN (implementation phased)
**Topic:** Systematic hook integration that makes biff a continuous presence in the developer workflow, not just a set of slash commands
**Research:** `punt-kit/research/entire-io-hook-architecture.md`

### Problem

Biff has three hooks today:

| Hook | Event | Trigger | What It Does |
|------|-------|---------|-------------|
| `session-start.sh` | SessionStart | Every session | First-run setup: deploy commands, auto-allow MCP tools, install statusline |
| `suppress-output.sh` | PostToolUse | Biff MCP tools | Display formatting: split panel summary vs. model context |
| `pr-announce.sh` | PostToolUse | GitHub PR create/merge | Suggest `/wall` announcement |
| `bead-claim.sh` | PostToolUse | Bash `bd update` | Suggest `/plan` after claiming work |

The first two are infrastructure (display pipeline, DES-001). The second two are nudges. All four are PostToolUse.

This leaves biff blind to the session lifecycle. Biff doesn't know when a session starts working, what branch it's on, when it switches context, when it commits, when it pushes, or when it ends. A teammate running `/who` sees stale data — a session that's been dead for hours still appears online because nothing triggered cleanup. A session that switched branches 30 minutes ago still shows its old plan.

Entire.io (see research doc) solved an analogous problem — session provenance — by hooking into **both** Claude Code lifecycle events (7 hooks across 6 events) and git hooks (4 hooks). Entire proved that deep lifecycle integration is invisible to the user when hooks are fast and fail-safe. The hooks are thin dispatchers that call `entire hooks <agent> <event>` — no logic in the shell script, all logic in the versioned CLI binary.

Biff should follow this pattern. Biff's mission is coordination, not provenance, so the *events* we care about are different, but the *architecture* is the same: thin shell dispatchers, dual-layer capture (Claude Code + git), and fail-open by default.

### Design

#### Principle: Biff Hooks Are the Connective Tissue

Biff commands (`/who`, `/write`, `/plan`, `/wall`) are the *vocabulary*. Hooks are the *nervous system* that keeps the vocabulary current. Without hooks, every biff command shows a stale snapshot. With hooks, biff reflects reality.

The hook architecture has two layers:

1. **Claude Code hooks** (plugin `hooks/hooks.json`) — capture agent lifecycle events
2. **Git hooks** (installed by `biff install`) — capture code lifecycle events

Both layers call `biff hook <event>` — the biff CLI binary is the single dispatcher. Hook scripts are one-liners. All logic lives in versioned Python code.

#### Layer 1: Claude Code Hooks

| Event | Matcher | Hook Script | Biff Action | Output |
|-------|---------|-------------|-------------|--------|
| **SessionStart** | `startup` | `hooks/session-start.sh` | Call `/tty` (auto-assign session name), set initial `/plan` from git branch, check `/read` for unread messages | `additionalContext` with setup summary |
| **SessionStart** | `resume\|compact` | `hooks/session-resume.sh` | Refresh presence heartbeat, re-announce plan | `additionalContext` if unread messages waiting |
| **SessionEnd** | `""` (all) | `hooks/session-end.sh` | Clear presence (immediate, don't wait for TTL) | Silent |
| **PostToolUse** | biff MCP tools | `hooks/suppress-output.sh` | Display formatting (unchanged, DES-001) | `updatedMCPToolOutput` + `additionalContext` |
| **PostToolUse** | GitHub PR tools | `hooks/pr-announce.sh` | Suggest `/wall` (unchanged) | `additionalContext` |
| **PostToolUse** | `Bash` | `hooks/post-bash.sh` | Dispatch: git checkout/switch → suggest `/plan` update; `bd update --status=in_progress` → suggest `/plan` (bead-claim, unchanged) | `additionalContext` |
| **Stop** | — | `hooks/stop.sh` | Refresh presence heartbeat (proves session is alive to remote observers) | Silent |
| **PreCompact** | — | `hooks/pre-compact.sh` | Snapshot current plan to `additionalContext` so it survives compaction | `additionalContext` |

**Not hooked (and why):**

| Event | Why Not |
|-------|---------|
| UserPromptSubmit | Too noisy. Every prompt would trigger a hook. No coordination value. |
| PreToolUse | Biff observes, it doesn't block. PreToolUse's value is blocking. |
| SubagentStart/Stop | Future (biff-60o, agent teams). Not needed until multi-agent coordination ships. |
| Notification | No coordination value. |
| TaskCompleted | Future (when beads is MCP). |
| TeammateIdle | Future (agent teams). |
| ConfigChange | No coordination value. |

#### Layer 2: Git Hooks

Installed by `biff install` into `.git/hooks/`. Each hook calls `biff hook git <event>`. All are **fail-open** (`|| true`) — if biff crashes, git still works. All gate on `.biff` + `.biff.local` enabled.

| Git Hook | Command | Biff Action |
|----------|---------|-------------|
| **post-checkout** | `biff hook git post-checkout "$1" "$2" "$3"` | Update `/plan` with new branch name. If switching from a feature branch to main, clear the plan. If switching to a feature branch, set plan to branch name (which often contains the bead ID). |
| **post-commit** | `biff hook git post-commit` | Update `/plan` with commit subject line (shows progress). Optionally suggest `/wall` for significant commits (merge commits, version bumps). |
| **pre-push** | `biff hook git pre-push "$1"` | Suggest `/wall` announcement for pushes to main/default branch. Silent for pushes to feature branches. |
| **post-merge** | (leave existing beads hook, add biff dispatch) | After `bd sync` runs, refresh `/plan` from current branch. |

**Not hooked (and why):**

| Git Hook | Why Not |
|----------|---------|
| prepare-commit-msg | Biff doesn't mutate commits. That's Entire's job. |
| commit-msg | Same — biff observes, it doesn't mutate. |
| pre-commit | Blocking hook. Biff doesn't block developer actions. |
| pre-rebase | No coordination value. |

#### The Dispatcher Pattern

All hooks are thin dispatchers. No logic in shell:

```bash
#!/usr/bin/env bash
# hooks/session-end.sh — thin dispatcher
biff hook claude-code session-end 2>/dev/null || true
```

```bash
#!/usr/bin/env bash
# .git/hooks/post-checkout — thin dispatcher
# $1=previous HEAD, $2=new HEAD, $3=branch flag
biff hook git post-checkout "$1" "$2" "$3" 2>/dev/null || true
```

The `biff hook` subcommand is a new CLI command group:

```text
biff hook claude-code session-start   # Called by SessionStart hook
biff hook claude-code session-resume  # Called by SessionStart (resume/compact)
biff hook claude-code session-end     # Called by SessionEnd hook
biff hook claude-code post-bash       # Called by PostToolUse Bash hook
biff hook claude-code stop            # Called by Stop hook
biff hook claude-code pre-compact     # Called by PreCompact hook
biff hook git post-checkout           # Called by git post-checkout hook
biff hook git post-commit             # Called by git post-commit hook
biff hook git pre-push                # Called by git pre-push hook
```

Each reads JSON from stdin (Claude Code hooks) or positional args (git hooks), calls the appropriate biff MCP tool or writes directly to the local relay, and outputs JSON to stdout (Claude Code hooks only).

#### Gating: `.biff` + `.biff.local`

All hooks gate on the `.biff` marker file and `.biff.local` enabled flag. This is unchanged from the existing pr-announce.sh and bead-claim.sh pattern:

```bash
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
[[ -f "$REPO_ROOT/.biff" ]] || exit 0
BIFF_LOCAL="$REPO_ROOT/.biff.local"
if [[ -f "$BIFF_LOCAL" ]]; then
  grep -qE '^enabled\s*=\s*true' "$BIFF_LOCAL" || exit 0
else
  exit 0
fi
```

Exception: `session-start.sh` runs unconditionally because it handles first-run setup (deploying commands, installing statusline). It gates the *biff-specific* actions (tty, plan) on `.biff` + `.biff.local`.

#### SessionStart: What Actually Happens

On `startup` (new session):

1. **Existing behavior** (unchanged): deploy commands, auto-allow MCP tools, install statusline
2. **New — auto-assign TTY**: Call `biff hook claude-code session-start` which calls the `tty` MCP tool with no name (auto-assigns `ttyN`). This makes every session immediately visible in `/who`.
3. **New — set initial plan from branch**: Read current git branch. If it matches a bead ID pattern (e.g., `biff-ka4-branch-switch-hook`), set plan to the bead ID and title. Otherwise set plan to the branch name.
4. **New — check unread**: Check unread message count. If > 0, include in `additionalContext`: "You have N unread messages. Use /read to view them."

On `resume` or `compact`:

1. Refresh presence heartbeat (re-publish session to KV so TTL doesn't expire)
2. Re-announce current plan in `additionalContext` (since compaction may have erased the context)
3. Check unread messages (same as startup)

#### SessionEnd: Immediate Cleanup

Currently, sessions expire via KV TTL (3 days, DES-008). This means `/who` shows ghost sessions for up to 3 days after a session ends. SessionEnd hook calls `biff hook claude-code session-end` which deletes the session KV entry immediately. This is best-effort — if biff crashes or the network is down, the TTL is the safety net.

#### Stop: Heartbeat

The `Stop` event fires every time Claude finishes a response. This is a natural heartbeat signal — if a session is active, `Stop` fires every few minutes. The hook refreshes the presence KV entry's TTL. This means sessions that go idle (user walks away without exiting) will eventually expire by TTL, but active sessions stay fresh.

Cost: one KV put per agent turn. At the observed rate of ~1-2 turns per minute during active work, this is negligible.

#### PreCompact: Plan Survival

When context compacts, the model loses all prior context. The PreCompact hook injects the current plan into `additionalContext` so the model remembers what it was working on:

```text
Current biff plan: biff-ka4: Branch-switch hook — nudge dotplan update after git checkout/switch
```

This is not a nudge — it's context preservation.

#### post-checkout: Branch-Aware Plan (biff-ka4)

Git's `post-checkout` hook fires on `git checkout`, `git switch`, and `git worktree add`. It receives three arguments: previous HEAD SHA, new HEAD SHA, and a branch flag (1 for branch checkout, 0 for file checkout).

When the branch flag is 1 (branch checkout):

1. Read new branch name
2. If branch name contains a bead ID pattern (`biff-xxx`), resolve to title via `bd show --json -q`
3. Call `biff plan` with the result
4. Output nothing (git hook, no JSON output)

This is a git hook, not a Claude Code hook. It fires reliably on every branch switch, whether done by Claude or by the human in another terminal. This is more reliable than a PostToolUse Bash hook matching `git checkout` because:

- It catches `git switch`, `git checkout`, and worktree operations
- It doesn't require regex matching of bash commands
- It fires even when the checkout happens outside Claude Code

#### post-commit: Progress Updates

After every commit, update the plan with the commit subject line. This gives `/who` observers a live feed of progress:

```text
/who
▶  NAME    TTY   IDLE  S  HOST    DIR              PLAN
   @kai    tty1  0m    +  MBP-2   punt-labs/biff   feat: auto-assign TTY on session start
```

The commit subject is the most concise summary of what just happened. It's already written, already reviewed, and already describes the work.

#### pre-push: Team Announcement

Before pushing to the default branch (main/master), suggest a `/wall` broadcast. This is the natural announcement point — the work is done, reviewed, and about to be visible to everyone.

Silent on feature branch pushes (those are in-progress work, not announcements).

#### biff-5zq: Plan Auto-Expand

When `/plan` receives a bead ID (e.g., `/plan biff-ka4`), the plan tool resolves the title:

1. Check if the message matches `/^[a-z]+-[a-z0-9]{2,4}$/` (bead ID pattern)
2. If match, shell out to `bd show --json -q <id>` to get the title
3. Set plan to `<id>: <title>` (e.g., `biff-ka4: Branch-switch hook`)
4. If `bd show` fails (not a valid bead), use the raw string as-is

This runs in the plan MCP tool itself, not in a hook. The hook architecture routes events to biff; the plan tool enriches content.

### Plan Semantics: Append vs. Overwrite

The plan is a single string visible in `/who` and `/finger`. Multiple hooks update it (SessionStart, post-checkout, post-commit, bead-claim, manual `/plan`). The question: should hooks *overwrite* the plan or *append* to it?

**Decision: Overwrite with provenance prefix.**

Each source gets a prefix that identifies *how* the plan was set:

| Source | Prefix | Example |
|--------|--------|---------|
| Manual `/plan` | (none) | `reviewing PR #64` |
| Bead claim | (none) | `biff-ka4: Branch-switch hook` |
| Git post-checkout | `→` | `→ feature/biff-ka4-hooks` |
| Git post-commit | `✓` | `✓ feat: auto-assign TTY on session start` |
| SessionStart (branch) | `→` | `→ main` |

**Why overwrite, not append:**

- The plan column in `/who` is 20-40 characters. Appending produces unreadable strings.
- The plan answers "what are you doing *right now*?" — not "what have you done today?"
- Commit history is `/last`. The plan is the present tense.

**Priority: manual > bead > git.** If the user explicitly called `/plan`, git hooks should not overwrite it. Implementation: store a `plan_source` field alongside the plan text. Git hooks only overwrite if `plan_source` is `"auto"`. Manual `/plan` and bead-claim set `plan_source` to `"manual"`. This prevents the annoying pattern where you set a careful plan and a git checkout immediately overwrites it.

### Hint and Marker File Architecture (biff-4b5)

Two categories of files bridge async relay state to synchronous hooks:

**Hint files** are ephemeral signals written by one hook and consumed by another.
Git hooks write them; Claude Code PostToolUse hooks read and delete them.

| File | Writer | Reader | Purpose |
|------|--------|--------|---------|
| `plan-hint` | `git post-checkout`, `git post-commit` | PostToolUse Bash (`check_plan_hint`) | Nudge `/plan` after branch switch or commit |
| `wall-hint` | `git pre-push` | PostToolUse Bash (`check_wall_hint`) | Nudge `/wall` after pushing to default branch |

**Marker files** represent durable state written by MCP tools and read by hooks.
MCP tools write them; SessionStart and PreToolUse hooks read them.

| File | Writer | Reader | Purpose |
|------|--------|--------|---------|
| `plan-active` | `/plan` tool | PreToolUse gate (`has_plan_marker`) | Gate Edit/Write on plan-set |
| `wall-active` | `/wall` tool | SessionStart (`read_wall_marker`) | Inject active wall into startup context |

All files live in `~/.biff/hints/{hash}/` where `{hash}` is `sha256(worktree_root)[:16]`.
Each git worktree gets its own directory to prevent cross-session races.

**Why not query the relay directly?** The relay is async (NATS KV or local JSON behind
an `async def`). Hooks run as synchronous subprocesses via `biff hook claude-code <event>`.
Spinning up an event loop and connecting to the relay adds 200-500ms per hook invocation.
Marker files are near-instant (`is_file()` or `read_text()`).

**Hint vs. marker lifecycle:**

- Hints are **consume-once**: the reader deletes the file after processing. This prevents
  stale nudges from repeating on every Bash command.
- Markers are **maintained**: the writer updates/deletes them as state changes. The
  PreToolUse gate reads `plan-active` on every Edit/Write call; it must reflect current state.

**Staleness and multi-session:**

- `plan-active` is cleared at SessionStart to prevent stale markers from a crashed session.
  In multi-session scenarios (same worktree), one session starting clears the other's marker.
  Collision detection already warns about this case.
- `wall-active` includes an `expires_at` timestamp. `read_wall_marker()` checks expiry
  and auto-cleans expired markers.

**Relationship to Z spec:** The Z spec models `SetPlanAuto` as a single PostToolUse
operation. The implementation splits this into two phases (git hook writes hint,
PostToolUse reads hint) because git hooks fire independently of Claude Code. The
functional result is identical: after a branch checkout, the next Claude Code operation
sees the plan nudge.

### Implementation Phases

#### Phase 1: Session Lifecycle (immediate)

- `biff hook` CLI command group with dispatcher
- SessionStart: auto-assign TTY, set plan from branch, check unread
- SessionEnd: immediate session cleanup
- Update `hooks/hooks.json` with SessionStart matchers and SessionEnd
- biff-5zq: plan auto-expand in the plan tool

**Files:** `src/biff/cli/hook.py` (new), `hooks/hooks.json`, `hooks/session-end.sh` (new), `src/biff/server/tools/plan.py`

#### Phase 2: Git Hooks (next)

- `biff install` deploys git hooks (post-checkout, post-commit, pre-push)
- post-checkout updates plan (biff-ka4)
- post-commit updates plan with commit subject
- pre-push suggests `/wall` for default branch pushes
- Consolidate post-bash.sh (absorbs bead-claim.sh, adds git checkout detection as fallback)

**Files:** `src/biff/cli/hook.py`, `src/biff/installer.py`, git hook templates

#### Phase 3: Heartbeat and Context (after beads MCP)

- Stop: presence heartbeat
- PreCompact: plan survival
- SessionStart resume/compact: presence refresh, plan re-announcement
- PostToolUse on beads MCP tools (replaces Bash regex matching)

**Files:** `hooks/hooks.json`, `hooks/stop.sh`, `hooks/pre-compact.sh`, `hooks/session-resume.sh`

### The biff-* Integration Mental Model

Each external tool biff integrates with is a "biff-*" surface:

| Surface | Hooks | Biff Actions |
|---------|-------|-------------|
| **biff-core** | SessionStart, SessionEnd, Stop, PreCompact | TTY, plan, presence, heartbeat |
| **biff-git** | post-checkout, post-commit, pre-push, PostToolUse(Bash) | Plan updates, wall suggestions |
| **biff-beads** | PostToolUse(Bash→`bd`), future: PostToolUse(beads MCP) | Plan from bead, claim nudge |
| **biff-github** | PostToolUse(GitHub MCP) | Wall suggestions for PR events |
| **biff-entire** | Future: coordinate plan/tty with Entire sessions | Shared session identity |
| **biff-prfaq** | Future: PostToolUse(prfaq tools) | Wall announcements for decisions |

These are not separate codebases or plugins. They are matcher groups in `hooks/hooks.json` and dispatch branches in `biff hook`. The "biff-*" naming is a mental model for organizing which events flow to which biff actions.

### Fail Modes

| Failure | Impact | Mitigation |
|---------|--------|-----------|
| `biff` CLI not installed | Hooks silently exit 0 | `command -v biff` check in dispatcher |
| NATS unreachable | Plan/presence updates fail | Local relay fallback; hooks exit 0 |
| `bd` not installed | Bead ID resolution fails | Graceful fallback to raw string |
| Git hook not installed | Git lifecycle events missed | `biff doctor` checks for missing hooks |
| Session crash (no SessionEnd) | Ghost session in /who | KV TTL (3 days) eventually cleans up |
| Hook takes too long | Claude Code waits | 5-second timeout on all hooks |

### Alternatives Considered

| Alternative | Rejected Because |
|-------------|-----------------|
| Logic in shell scripts (current approach for pr-announce, bead-claim) | Not testable, not versioned with the CLI, duplicates gating logic. Entire.io's dispatcher pattern is superior. |
| Hook only Claude Code, not git | Misses manual git operations (checkout in another terminal). Git hooks are more reliable for code lifecycle events. |
| Hook UserPromptSubmit for activity tracking | Too noisy. Stop event is sufficient for heartbeat (fires once per agent turn, not once per keystroke). |
| PreToolUse for safety checks | Biff observes, it doesn't block. Blocking developer actions is not biff's job. |
| Separate biff-git, biff-beads plugins | Over-engineering. These are matcher groups and dispatch branches, not separate plugin surfaces. |
| Auto-wall on every commit | Too noisy. Only suggest wall for pushes to default branch and PR events. |

## DES-018: Talk v2 — Status-Line Auto-Read

**Date:** 2026-02-25
**Status:** Implemented
**Bead:** biff-q97

### Problem

Talk v1 (v0.9.0) used a blocking `talk_listen` loop: subscribe → check inbox → block
until message → return → prompt LLM → repeat.  Each inbound message required a full
LLM round-trip (2-5s) just to display it.  Outbound messages also required an LLM
round-trip (unavoidable), making the conversation feel sluggish at 4-10s per exchange.

### Decision

Replace blocking `talk_listen` with status-line auto-read.  When both parties `/talk`
each other, incoming messages display on the status bar within 0-2s — no LLM in the
inbound path.  Outbound uses `/write` (one LLM round-trip, unavoidable).

**Key CX distinction from `/write` + `/read`:**

- `/write` + `/read` is a mailbox (async).  Someone has to `/read`.
- `/talk` is mutual presence.  Both agree, then auto-read each other on the status bar.

### Implementation

1. **NATS notifications carry message body** — `_publish_talk_notification` sends
   JSON `{"from": sender, "body": text}` instead of bare `b"1"` wake signal.
2. **Poller manages NATS subscription** — `_manage_talk_subscription` in
   `_descriptions.py` subscribes when `_talk_partner` is set, unsubscribes when
   cleared.  The callback filters messages from the talk partner and sets
   `_talk_message`.
3. **Status line displays talk** — `_talk_segment` renders on line 2 (bold yellow).
   Priority: talk > wall > idle.
4. **Shared state in `_descriptions.py`** — `_talk_partner` and `_talk_message` are
   module-level variables following the existing pattern (wall, tty_name, biff_enabled).
   Written to `unread.json` by the poller, read by the statusline process.

### Alternatives Considered

| Alternative | Rejected Because |
|-------------|-----------------|
| Keep blocking `talk_listen` | 4-10s per exchange, each inbound message hits LLM. Unusable for real-time conversation. |
| Notification hooks (PreToolUse/PostToolUse) | Would need LLM to process the notification — same latency problem. |
| WebSocket/SSE push from MCP server | Claude Code doesn't support server-initiated push outside tool responses and `notifications/tools/list_changed`. |
| Separate talk line on status bar (line 3) | Claude Code status bar supports exactly 2 lines. Must share line 2 with wall. |

---

## DES-019: Persistent NATS Connection — Eliminate POP-Mode Cycling

**Date:** 2026-02-25
**Status:** SETTLED
**Topic:** Status bar latency regression caused by NATS connection cycling during idle

### Problem

After the nap-mode + POP-mode connection conservation was introduced (PR #60), wall
and talk status bar updates took 2+ minutes to appear on idle sessions.  The root cause:
when a session transitioned to "napping" after 120s of idle, the NATS TCP connection was
fully closed.  This killed all KV watches (wall, sessions) and NATS subscriptions (talk
notifications).  The POP cycle reconnected every 10s for a brief fetch, then disconnected
again — meaning updates could only land during the narrow reconnect window.

The key insight: `notify_tool_list_changed()` is the forcing function that makes Claude
Code re-read tool descriptions.  Without a live NATS connection, wall changes cannot
trigger this notification.  Polling alone can never achieve 2s latency because the poller
runs on a 2s tick but the unread file write only helps the human-visible status line —
the model-visible tool description still requires `tools/list_changed` to fire.

**Refinement (DES-020):** The notification alone is necessary but not sufficient.  Claude
Code must see an actual tool description change when it re-reads, or the re-render that
refreshes the status bar does not trigger.  See DES-020 for the full analysis.

### Decision

Eliminate POP-mode connection cycling entirely.  Napping now means "reduce polling
frequency" (30s instead of 2s) but the NATS connection stays open.  This preserves:

1. **KV watches** — wall changes detected instantly via `kv.watchall()`, triggering
   `refresh_wall()` → `_notify_tool_list_changed()` within milliseconds.
2. **NATS subscriptions** — talk notifications delivered in real-time.
3. **Heartbeat** — session liveness maintained without reconnect storms.

### Implementation

1. **`ActivityTracker`** — Simplified.  Renamed `record_pop()` → `record_nap_poll()`,
   `seconds_since_pop()` → `seconds_since_nap_poll()`.  Napping is purely a frequency
   knob, not a connection state.

2. **`poll_inbox()`** — Removed `disconnect()` call from nap transition.  Removed
   `_pop_fetch()` function entirely.  Nap mode now runs `_active_tick` at 30s intervals
   instead of disconnecting.

3. **KV watcher (`_run_kv_watch`)** — Extended to detect wall key changes.  When a
   KV entry matches `{repo}.wall`, calls `refresh_wall()` which fires
   `_notify_tool_list_changed()`.  The watcher no longer exits during nap — it runs
   for the full server lifetime.

4. **Heartbeat loop** — No longer skips during nap.  Heartbeats fire on schedule
   regardless of activity state.

### Why Not Keep Connection Conservation

| Concern | Resolution |
|---------|-----------|
| NATS connection cost during idle | One TCP connection per session is trivial.  NATS is designed for long-lived connections. |
| Server load during nap | Reduced from polling every 2s to every 30s.  KV watches are server-push, zero client-side cost. |
| Bandwidth | KV watch delivers only changed keys.  No polling overhead at all for wall/session changes. |

### Prior Design (Rejected)

POP-mode (PR #60): disconnect after 120s idle, reconnect every 10s for brief POP cycle
(fetch inbox + heartbeat), then disconnect again.  This was designed to conserve NATS
connections but broke the real-time push notification pipeline.  The 10s reconnect window
meant worst-case 10s + processing latency for wall updates — and in practice, the
combination of reconnect time, KV watch restart, and poller timing resulted in 2+ minute
visible latency.

---

## DES-020: Talk Notification Parity with Wall — Tool Description Mutation

**Date:** 2026-02-25
**Status:** SETTLED
**Related:** DES-018 (Talk v2), DES-019 (Persistent NATS)

### Problem

Wall messages appear on the status bar within 0-2 seconds.  Talk messages during an
active `/talk` session do not — they reach the unread JSON file but the status bar
only picks them up on Claude Code's next scheduled status line poll (interval unknown,
observed as multiple seconds to tens of seconds).

Both paths call `_notify_tool_list_changed()`.  Both write the unread file.  The
NATS subscription callback fires correctly (confirmed by inspecting unread files on
disk — `talk_message` is populated).  Yet wall triggers an immediate status bar
refresh and talk does not.

### Root Cause

`_notify_tool_list_changed()` sends the MCP `notifications/tools/list_changed` signal
to Claude Code.  Claude Code responds by re-reading the tool list.  The critical
difference is what Claude Code **sees** when it re-reads:

| Path | Tool Description Mutated? | Claude Code Sees Change? | Re-render? |
|------|--------------------------|-------------------------|------------|
| **Wall** | Yes — `wall` tool description becomes `[WALL] message text...` | Yes | Yes → status bar refreshes |
| **Talk** | No — no tool description changes | No | No → status bar stale |

The notification is necessary but not sufficient.  Claude Code must observe an actual
tool description change to trigger the UI re-render that also refreshes the status bar.
Writing the unread file is invisible to Claude Code's MCP layer — the file is only read
by the external `biff statusline` process, which runs on Claude Code's own schedule.

### Two Notification Channels

There are two independent channels that update the status bar:

1. **MCP protocol** (`notifications/tools/list_changed` → tool re-read → UI re-render)
   — instant when a tool description actually changes.  This is how wall achieves 0-2s.

2. **Status line file** (`~/.biff/unread/{session}.json` → `biff statusline` command)
   — polled by Claude Code at its own interval.  This is the only channel talk uses.

Wall uses both channels.  Talk uses only channel 2.  That is the asymmetry.

### Decision

Mutate the `talk` tool description when a talk message arrives, mirroring the wall
pattern.  New `refresh_talk()` function updates the talk tool description to
`[TALK] @sender: message — Use /write @partner to reply. Use talk_end to close.`
and clears it on `talk_end`.  This gives Claude Code a visible change to react to,
triggering the re-render that refreshes the status bar.

### Implementation

1. **`_TALK_BASE_DESCRIPTION` constant** in `_descriptions.py` — single source of truth
   for the talk tool's default description.  Used by both `talk.py` registration and
   `refresh_talk()` reset.

2. **`refresh_talk(mcp, state)`** — mirrors `refresh_wall()`.  Mutates talk tool
   description, calls `notify_tool_list_changed()` on change, rewrites unread file.

3. **`_manage_talk_subscription(mcp, state, ...)`** — now accepts `mcp` so the
   `_on_talk_msg` NATS callback can call `refresh_talk(mcp, state)` instead of
   the old `_sync_talk_to_file()` + event-based approach.

4. **`_sync_talk_to_file` deleted** — replaced entirely by `refresh_talk()` which
   handles both tool description mutation and unread file write.

5. **`_active_tick` poller** — belt path also calls `refresh_talk()` when talk message
   changes, ensuring any message missed by the NATS callback is still picked up.

### Prior Approach (Rejected)

An `asyncio.Event` bridge was attempted: the NATS callback set an event, a watcher
task awaited it and fired `notify_tool_list_changed()` from a different task context.
This was based on the incorrect hypothesis that `_session.send_tool_list_changed()`
failed from the NATS callback context.  The real issue was that the notification
delivered correctly but Claude Code saw no tool description change, so no re-render
occurred.

---

## DES-021: Suspenders Path Session Capture Bug — `_session` Never Initialized

**Date:** 2026-03-02
**Status:** RESOLVED (PR #96)
**Related:** DES-004 (Belt-and-Suspenders), DES-020 (Talk Description Mutation)
**Bead:** biff-8g0
**Specification:** `docs/notification.tex` (Z specification)

### Problem

Talk push notifications from CLI to MCP session are broken.  The NATS latency
diagnostic (`test_notification_latency.py`) proved core pub/sub delivers reliably
in under 1ms.  DES-020 fixed the tool description mutation gap (Claude Code now
sees a changed description).  Yet `notifications/tools/list_changed` still does
not reach the client.

### Root Cause

The suspenders path in `notify_tool_list_changed()` (line 143 of
`_descriptions.py`) requires a stored `ServerSession` reference:

```python
if _session is not None:
    await _session.send_tool_list_changed()
```

`_session` is **only** set inside the belt path (line 135):

```python
ctx = get_context()
await ctx.send_notification(ToolListChangedNotification())
_session = ctx.session  # <-- only assignment
```

The belt path only runs when `notify_tool_list_changed()` is called from inside
a tool handler (where `get_context()` succeeds).  But
`notify_tool_list_changed()` is only called when a tool description actually
changes (the `if tool.description != old_desc` guard in `refresh_read_messages`,
`refresh_wall`, and `refresh_talk`).

In the talk push notification path, the sequence is:

1. User calls `/plan` (0 unread → description stays `_READ_MESSAGES_BASE` → no
   change → `notify_tool_list_changed()` never called → `_session` stays `None`)
2. User calls `/talk @eric` (talk tool doesn't call `refresh_read_messages` when
   no opening message → `_session` still `None`)
3. Poller tick establishes NATS subscription via `_manage_talk_subscription`
4. External NATS notification arrives → `_on_talk_msg` → `refresh_talk` →
   `notify_tool_list_changed()` → belt path fails (`RuntimeError`: no request
   context in a NATS callback) → suspenders path finds `_session is None` →
   **notification silently dropped**

The description mutation works — the talk tool description DOES update to
`[TALK] @eric: message`.  The client discovers this on the next `list_tools()`
poll.  But the *push* notification that tells the client to re-read never fires.

### Evidence

Five integration tests in `tests/test_nats_e2e/test_talk_push.py` exercise the
full `_on_talk_msg` → `refresh_talk` → `notify_tool_list_changed` chain:

| Test | Result | Proves |
|------|--------|--------|
| `test_notification_updates_talk_description` | PASS | `_on_talk_msg` correctly mutates the talk tool description |
| `test_notification_fires_tool_list_changed` | PASS | Suspenders path delivers notification (was xfail before fix) |
| `test_notification_adds_display_item` | PASS | Display queue receives talk items correctly |
| `test_notification_coalesces_rapid_messages` | PASS | Rapid messages produce exactly 1 display item |
| `test_self_echo_rejected` | PASS | Self-echo filter works (own `from_key` ignored) |

### Why This Was Not Caught Earlier

DES-004 describes `_session` capture as happening "from the first tool call."
This is true only if the first tool call triggers a description change.  The
integration tests in `test_protocol.py` always deliver a message before calling
a tool, guaranteeing a description change → belt path → `_session` captured.
The NATS E2E tests exercise the real startup sequence where no messages exist
yet.

### Impact

The talk tool description updates work (server-side mutation is correct), but
Claude Code is never notified to re-read.  The human sees talk messages on the
status bar (channel 2, the unread file), but the model does not see the changed
tool description until its next tool call triggers `list_tools()`.  This means
Claude cannot proactively respond to incoming talk messages.

### Resolution: Approach A — `SessionCaptureMiddleware`

Approach A (middleware) was chosen over Approach B (unconditional refresh capture).
The middleware is cleaner because it captures the session at the *earliest possible
point* — during `on_initialize`, before any tool call or NATS subscription fires.
This is stronger than Approach B, which would still require a tool call to trigger.

#### Z Specification

The fix was modeled formally in `docs/notification.tex` before implementation.
The specification defines the `CaptureSession` operation (§6) and the key state
invariant:

> `natsConn = natsConnected ⟹ session = sessCaptured`

This invariant makes the bug class structurally impossible.  The specification
was type-checked with fuzz (zero errors) and compiled to PDF.

#### Implementation

**`_SessionCaptureMiddleware`** (`src/biff/server/app.py`):

```python
class _SessionCaptureMiddleware(Middleware):
    async def on_initialize(self, context, call_next):
        result = await call_next(context)
        if context.fastmcp_context is not None:
            capture_session(context.fastmcp_context.session)
        return result
```

**`capture_session()`** (`src/biff/server/tools/_descriptions.py`):

Sets the module-level `_session` reference.  The belt path in
`notify_tool_list_changed()` continues to refresh it on every tool call,
keeping it current if the client reconnects.

**Why `on_initialize` works:** The MCP session does not exist during the server
lifespan (created when the client sends `initialize` after `yield state`).
FastMCP's `MiddlewareServerSession._received_request()` creates a
`Context(session=self)` for `InitializeRequest`, and the `Context.session`
property has a fallback path that returns `self._session` during `on_initialize`.

**Temporal safety:** NATS talk subscriptions are only created during tool calls
(via `_manage_talk_subscription` in `poll_inbox`), which happen after
`initialize`.  So the session is always captured before any callback fires.

#### Startup Sequence (matches Z spec §12)

1. `Init` — all state zeroed, NATS disconnected, session uncaptured
2. `CaptureSession` — middleware captures session during `on_initialize`
3. `EnsureConnected` — NATS connects (invariant satisfied: session captured)
4. Poller starts, subscriptions begin — suspenders path available

#### Audit-Driven Bounds Enforcement

A `/z-spec:audit` against the test suite found two spec-code divergences where
the Z specification declared bounds that the code did not enforce:

| Spec Constant | Code Constant | Enforcement |
|---------------|---------------|-------------|
| `maxQueueSize = 20` | `MAX_QUEUE_SIZE` in `display_queue.py` | `DisplayQueue.add()` evicts oldest item when full |
| `maxUnreadCount = 100` | `MAX_UNREAD_COUNT` in `_descriptions.py` | `_write_unread_file` clamps count |

The audit also identified that the `test_notification_fires_tool_list_changed`
xfail marker should now pass — it was removed.

### Why Approach B Was Rejected

Approach B (capture in `refresh_*` unconditionally) would have been a smaller
diff but weaker guarantee.  It still requires *some* tool call to trigger the
first capture.  In the talk push path, the NATS callback can fire before any
tool call triggers a `refresh_*` function.  Approach A eliminates this race by
capturing during `initialize` — the earliest possible point.

### Test Coverage

**Integration tests** (`tests/test_integration/test_protocol.py`):

| Test | Proves |
|------|--------|
| `test_session_captured_on_initialize` | `_session is not None` after initialize — no tool call needed |
| `test_suspenders_path_before_first_tool_call` | `notify_tool_list_changed()` works outside tool context |

**NATS E2E tests** (`tests/test_nats_e2e/test_talk_push.py`):

| Test | Result | Proves |
|------|--------|--------|
| `test_notification_updates_talk_description` | PASS | `_on_talk_msg` correctly mutates the talk tool description |
| `test_notification_fires_tool_list_changed` | PASS | Suspenders path delivers notification (was xfail, now passes) |
| `test_notification_adds_display_item` | PASS | Display queue receives talk items correctly |
| `test_notification_coalesces_rapid_messages` | PASS | Rapid messages produce exactly 1 display item |
| `test_self_echo_rejected` | PASS | Self-echo filter works (own `from_key` ignored) |

**Bounds tests** (from Z spec audit):

| Test | File | Proves |
|------|------|--------|
| `test_evicts_oldest_when_full` | `test_display_queue.py` | `MAX_QUEUE_SIZE=20` enforced |
| `test_clamps_unread_count_at_max` | `test_descriptions.py` | `MAX_UNREAD_COUNT=100` enforced |

### Test Infrastructure

- **`src/biff/testing/notifications.py`** — Reusable `NotificationTracker`
  (`MessageHandler` subclass that counts `tool_list_changed` notifications).
  Exported from `biff.testing`.
- **`tests/test_nats_e2e/conftest.py`** — `kai_tracked` and `eric_tracked`
  fixtures yielding `(Client, NotificationTracker, ServerState)` tuples with
  NatsRelay backing.
- **`tests/test_nats_e2e/test_talk_push.py`** — Five tests exercising the full
  push notification chain.  Reusable `_publish_talk_notification` and
  `_wait_for_talk_description` helpers.

## DES-022: Library API — Command Extraction via Humble Object Pattern

**Date:** 2026-03-03
**Status:** SETTLED
**Related:** DES-005 (Command Vocabulary), DES-006 (Relay Protocol)

### Problem

All 10 product commands (`who`, `finger`, `write`, `read`, `plan`, `last`,
`wall`, `mesg`, `tty`, `status`) lived as `_*_async` functions inside
`__main__.py`.  Each embedded the same infrastructure: `cli_relay()` context
manager, `_json_output` branching, `print()`, `typer.Exit`.  This made commands:

1. **Untestable without mocking** — every test needed to mock `cli_relay()`,
   capture stdout, and inspect exit codes.
2. **Uncallable from library code** — no way to invoke `who()` from Python
   without going through the CLI boundary.
3. **Duplicated** — 10 copies of the same relay/output/exit plumbing.

### Decision: Humble Object + CommandResult

Each command becomes a **pure async function** in `src/biff/commands/`:

```python
async def who(ctx: CliContext) -> CommandResult:
    sessions = await ctx.relay.get_sessions()
    ...
    return CommandResult(text=format_who(sessions), json_data=[...])
```

A single `_run()` adapter in `__main__.py` handles all CLI plumbing:

```python
def _run(coro_factory: Callable[[CliContext], Awaitable[CommandResult]]) -> None:
    async def _inner() -> None:
        async with cli_relay() as ctx:
            result = await coro_factory(ctx)
            if _json_output:
                data = result.json_data if result.json_data is not None else result.text
                _print_json(data)
            elif result.error:
                print(result.text, file=sys.stderr)
            else:
                print(result.text)
            if result.error:
                raise typer.Exit(code=1)
    asyncio.run(_inner())
```

Typer commands become one-liners:

```python
@app.command()
def who() -> None:
    _run(commands.who)

@app.command()
def finger(user: Annotated[str, typer.Argument(...)]) -> None:
    _run(lambda ctx: commands.finger(ctx, user))
```

### Why This Shape

**`CommandResult` over exceptions.** Commands return errors as
`CommandResult(error=True)` rather than raising.  This keeps error paths
testable with simple assertions (`assert result.error`) instead of
`pytest.raises`.  The `_run()` adapter translates `error=True` to
`typer.Exit(code=1)` at the CLI boundary.

**`CliContext.relay` widened to `Relay` protocol.** Was `NatsRelay` — now
accepts any `Relay` implementation.  This is the key change that makes
`LocalRelay(tmp_path)` work in tests without NATS, mocks, or network.

**No decorator.** A plain `_run()` function is simpler than a decorator.
The typer wiring stays explicit — each `@app.command()` function shows its
argument definitions, and the lambda captures only what the command needs.

**Keyword-only booleans.** `wall(..., *, clear: bool)` and
`mesg(ctx, *, enabled: bool)` satisfy ruff FBT001 and prevent positional
boolean confusion at call sites.

### Alternatives Rejected

**A. Decorator pattern.** Wrapping commands with `@cli_command` that handles
`_run()` plumbing.  Rejected: obscures the typer argument definitions, makes
IDE navigation harder, adds indirection without reducing code.

**B. Return tuples instead of `CommandResult`.** `(text, json_data, error)`
three-tuples.  Rejected: unnamed fields are error-prone, no default for
`json_data`, worse IDE support.

**C. Keep `_*_async` functions, test via subprocess.**  Already have tier 3
subprocess tests.  Rejected: slow (5s vs 0.15s), can't inspect intermediate
state, no library API.

### Key Design Details

**`CommandResult.json_data: object`.**  Not `dict | list | None` — commands
return whatever JSON-serializable structure fits.  `status` returns a dict,
`who` returns a list, `wall` returns a `WallPost` dict or `None`.  The `object`
type is narrowed with `cast()` in tests where needed (pyright requires this
when narrowing `object` through `isinstance`).

**`_run()` owns the relay lifecycle.**  Commands never call `cli_relay()`
themselves — `_run()` creates the context and passes it in.  This means
commands are pure functions of their arguments with no hidden state.

**Validation stays at the boundary.**  The `mesg` command in `__main__.py`
validates `on/off/y/n` input strings and converts to `bool` before calling
`commands.mesg(ctx, enabled=True)`.  The library function takes `bool`, not
strings — CLI parsing is a CLI concern.

### Test Architecture

Tests use `LocalRelay(tmp_path)` with no mocks and no NATS:

```python
@pytest.fixture()
def ctx(relay: LocalRelay) -> CliContext:
    return CliContext(
        relay=relay,
        config=BiffConfig(user="kai", repo_name="test"),
        session_key="kai:abc12345",
        user="kai",
        tty="abc12345",
    )

async def test_who_empty(ctx: CliContext) -> None:
    result = await who(ctx)
    assert result.text == "No sessions."
    assert result.json_data == []
    assert not result.error
```

**`WtmpRelay`** extends `LocalRelay` with in-memory wtmp storage for `last`
command tests.  `LocalRelay.get_wtmp()` returns empty by design (no persistence
layer) — `WtmpRelay` overrides it with a list that tests can populate.

**Multi-user tests** use two fixtures (`ctx` for kai, `ctx_eric` for eric)
sharing the same `LocalRelay` instance, verifying message isolation, wall
visibility, and session independence.

### Coverage

71 tests across 11 files.  100% line coverage on all 12 modules in
`biff.commands` (208/208 statements).

| Module | Stmts | Cover |
|--------|-------|-------|
| `__init__.py` | 13 | 100% |
| `_result.py` | 7 | 100% |
| `finger.py` | 17 | 100% |
| `last.py` | 22 | 100% |
| `mesg.py` | 13 | 100% |
| `plan.py` | 14 | 100% |
| `read.py` | 17 | 100% |
| `status.py` | 22 | 100% |
| `tty.py` | 25 | 100% |
| `wall.py` | 30 | 100% |
| `who.py` | 10 | 100% |
| `write.py` | 18 | 100% |

### Net Effect

- `__main__.py`: −298 lines (366 deleted, 68 added — `_run()` + one-liners)
- `src/biff/commands/`: +208 lines across 12 modules
- `tests/test_commands/`: +71 tests running in 0.15s
- Library API: `from biff.commands import who, CommandResult`
- MCP tools: unchanged (they call the relay directly, not the CLI commands)

## DES-023: E2E Test Harness — Status Line Polling Gap

**Date:** 2026-03-02
**Status:** OPEN (bug characterized, fix pending)
**Related:** DES-004 (Push Notifications), DES-007 (NATS Namespace Scoping), DES-011 (Status Line)
**Script:** `scripts/test-talk-e2e.sh`

### Problem

Talk push notifications are not reaching the status bar in production.  DES-021
fixed the `_session` capture bug (suspenders path now fires correctly), and all
integration and NATS E2E tests pass.  Yet real users do not see talk or wall
notifications in the status bar.

### Approach — Tier 5: Human-Equivalent E2E

Tiers 1–4 test components in isolation with increasing transport fidelity, but
all bypass Claude Code's status line polling.  A new tier was needed:

| Property | Value |
|----------|-------|
| Transport | Real Claude Code sessions (marketplace plugin, no mocks) |
| Driver | `tmux send-keys` feeding slash commands |
| Assertion | `tmux capture-pane` for status bar, `~/.biff/unread/*.json` for delivery |
| Recording | Asciinema `.cast` file (automatic, kept on failure) |

The script creates two isolated clones of the biff repo, launches Claude Code
in each tmux pane, drives `/tty`, `/wall`, `/write`, and `/talk` commands, and
checks for a unique needle string in the receiving pane's terminal output and
unread files.

### Key Decisions

#### 1. Same directory name, different parents (DES-007)

NATS namespaces are scoped by `repo_root.name` (`config.py:396-397`):
`sanitize_repo_name(repo_slug or repo_root.name)`.  Two clones with different
directory names (e.g. `biff-a`, `biff-b`) land in different NATS namespaces —
sessions cannot see each other.

**Fix:** Clone to `.../a/biff/` and `.../b/biff/`.  Both get `repo_root.name =
"biff"`, sharing one NATS namespace.  The parent directories provide isolation.

#### 2. `CLAUDECODE` environment variable

Claude Code sets `CLAUDECODE=1` in its shell environment.  Launching `claude`
from within a Claude Code session triggers nested-session detection:
`"Error: Claude Code cannot be launched inside another Claude Code session."`

**Fix:** `unset CLAUDECODE && claude` in each tmux pane.

#### 3. Trust prompt for new directories

Claude Code shows "Is this a project you created or one you trust?" on first
visit to an unknown directory.  This blocks startup until the user responds.

**Fix:** After launching, poll `capture-pane` for "trust" and send Enter to
accept.

#### 4. Slash command autocomplete picker

Claude Code's slash command input shows an autocomplete picker on first Enter.
A second Enter is required to execute the selected command.

**Fix:** `send_slash()` function: send text + Enter (opens picker), sleep 1s,
send Enter again (selects and executes).

#### 5. Three-level assertion model

Status bar visibility depends on Claude Code polling `biff statusline` at the
right moment.  A binary pass/fail misses the critical middle state where NATS
delivered the message but the UI didn't render it.

**Levels:**

| Result | Meaning |
|--------|---------|
| **PASS** | Needle visible in `tmux capture-pane` (user would see it) |
| **DELIVERED** | Needle found in `~/.biff/unread/*.json` but not in pane capture |
| **FAIL** | Needle not found anywhere (NATS delivery or file write broken) |

DELIVERED is the most informative failure — it isolates the bug to the
Claude Code ↔ status line boundary.

#### 6. Asciinema re-exec wrapper

The script re-executes itself under `asciinema rec` on first invocation.
The recording captures all progress output, pane dumps, and results.
On success the `.cast` file is deleted; on failure it is preserved for
post-mortem analysis.  The `_BIFF_E2E_REC` environment variable prevents
infinite re-exec.

### Findings

Three consecutive runs produced identical results:

```text
WALL:  DELIVERED  (file ok, not rendered)
TALK:  DELIVERED  (file ok, not rendered)
```

**Evidence from `statusline.log`:**

```text
17:00:17.054 key=20607 exists=True items=[] display=''
17:00:20.460 key=20605 exists=True items=[wall:@jmf-pobox (ttyA): ...] display='▶ ...'
```

Session A's status line correctly read the wall item (key 20605).  Session B's
status line (key 20607) showed `items=[]` — the unread file had not yet been
written at the time of its last poll.  After that, no more polls occurred.

**Evidence from `~/.biff/unread/` files:**

```json
{
  "user": "jmf-pobox",
  "repo": "biff",
  "count": 1,
  "tty_name": "ttyB",
  "biff_enabled": true,
  "display_items": [
    {"kind": "talk", "text": "@jmf-pobox: e2e-talk-..."}
  ]
}
```

The unread file for session B contained the correct talk display item.  The
file was written correctly by the NATS callback → `_write_unread_file` path.
The status bar never rendered it because Claude Code stopped polling.

### Root Cause

Claude Code polls `biff statusline` (via the status line shell command) on
an internal schedule.  During idle periods — when no tool calls are in flight
and no user input is arriving — **polling stops entirely**.  The status line
shell command is only invoked when Claude Code's UI refresh loop runs, which
appears to be gated on activity.

This is a Claude Code runtime behavior, not a biff bug.  Biff correctly:

1. Receives the NATS message
2. Writes the unread file with `display_items`
3. Returns the correct display string when `biff statusline` is called

The gap is that `biff statusline` is never called during idle.

### Implications

- **Wall and talk are equally affected.** Both produce DELIVERED, not PASS.
  This is not a talk-specific bug.
- **The suspenders path fix (DES-021) is necessary but not sufficient.**
  `notify_tool_list_changed()` fires correctly, but Claude Code's status line
  is a separate channel that doesn't respond to tool-list-changed notifications.
- **The unread file is a write-only artifact during idle.**  It is written
  correctly but never read until the next user interaction.

### Next Steps

1. **File upstream:** Report the status line polling gap to Claude Code.  The
   status line should poll on a timer independent of tool-call activity, or
   respond to file-system change notifications on the unread file.
2. **Workaround investigation:** Determine if `notify_tool_list_changed()` can
   trigger a status line refresh (it triggers tool description re-reads, but
   the status line is a separate shell command channel).
3. **Expand test harness:** The script infrastructure (`send_slash`, `wait_for`,
   `needle_in_unread`) is reusable for future E2E tests beyond talk.

### Bugs Found During Harness Development

| Bug | Symptom | Fix |
|-----|---------|-----|
| `CLAUDECODE` env var | Nested session detection error | `unset CLAUDECODE` before launch |
| Trust prompt | Startup blocked indefinitely | Poll for "trust", send Enter |
| Slash command picker | Command not executed on first Enter | Double-Enter via `send_slash()` |
| NATS namespace scoping | Sessions invisible to each other | Same dir name, different parents |
| `needle_in_unread` subshell | `return 0` inside `find \| while` exits subshell, not function | Replaced with `find -exec grep -lF` |
| `dump_pane` empty lines | `tail -30` showed blank lines from TUI buffer | Filter with `grep '.'` first |

## DES-024: Fire-and-Forget MCP Side-Effect Tools

**Date:** 2026-03-06
**Status:** SETTLED
**Related:** DES-004 (Push Notifications), punt-kit §Sync vs Async

### Problem

MCP side-effect tools (`write`, `wall`, `talk`) awaited their relay call before
returning.  This blocked the caller for the full relay round-trip (NATS publish +
JetStream ack).  Per punt-kit §Sync vs Async, side-effect-only operations should
return immediately and complete asynchronously.

### Decision

Side-effect relay calls use `fire_and_forget()` from `_tasks.py`.  The tool
returns the user-facing confirmation string immediately; the relay call completes
in the background.

**Ordering constraint:** `await refresh_*()` MUST run BEFORE `fire_and_forget()`
to avoid racing `relay._ensure_connected()`.  The refresh call triggers the first
relay connection; if the background task races it, both paths call
`_ensure_connected()` concurrently.

### Implementation — `_tasks.py`

Shared module at `src/biff/server/tools/_tasks.py`.  Key design choices:

1. **GC-safe task set.** `asyncio.create_task()` returns a normal `Task`, but the
   event loop only weakly references background tasks.  A module-level `set[Task]`
   holds strong references to prevent premature garbage collection; the done
   callback calls `discard()` to avoid unbounded growth.
2. **Structured error logging.** The done callback checks `task.exception()`
   and logs with `exc_info=exc` for full stack traces.
3. **Single extraction point.** Extracted after Copilot review flagged
   triplication across `messaging.py`, `wall.py`, and `talk.py`.

### Testing

Unit tests that call tool handlers directly must `await asyncio.sleep(0)` after
the tool call to yield the event loop for the background task.  Without this, the
relay `.deliver()` never executes and inbox assertions fail.

### Alternatives Rejected

- **`TaskGroup`:** Overkill for independent fire-and-forget operations with no
  structured cancellation needs.
- **Awaiting inline:** The previous design.  Correct but unnecessarily slow for
  the caller.

## DES-025: CI Notification Workflow — Standalone `workflow_run` Trigger

**Date:** 2026-03-08
**Status:** SETTLED
**Related:** DES-017 (Hook Integration), PR #120 (inline CI notifications)

### Problem

PR #120 added CI failure notifications to biff's own `test.yml` as an inline
step.  This worked but required manual YAML editing per-workflow per-repo.  For
biff to serve as team infrastructure, any repo should get CI notifications by
running `biff enable` — no workflow editing.

### Decision

Deploy a standalone `.github/workflows/biff-notify.yml` that uses GitHub's
`workflow_run` trigger with `types: [completed]`.  This file fires after *any*
workflow completes, checks for failure + push event, and posts `biff wall`.

**Key design choices:**

1. **`workflow_run` over per-workflow steps.** A single observer file replaces
   N inline steps across N workflows.  Adding new workflows to a repo
   automatically gets notification coverage with zero editing.
2. **Push-only filter.** `github.event.workflow_run.event == 'push'` prevents
   fork PR spam — external contributors' PRs don't trigger wall broadcasts.
3. **`sparse-checkout: .biff`** — only checks out the team config file, keeping
   the checkout step fast regardless of repo size.
4. **`uvx punt-biff`** — runs biff as a standalone tool without `uv sync`,
   avoiding dependency installation overhead.
5. **2-minute timeout** — notification is fire-and-forget; if NATS is down the
   step should fail fast, not hold a runner.
6. **Template-as-data** — the YAML lives in `src/biff/data/biff-notify.yml`,
   shipped via `importlib.resources`.  `check_ci_workflow()` compares the
   deployed file against the bundled template to detect staleness.

### Integration with `biff enable`/`biff disable`

`enable` calls `deploy_ci_workflow()` and `ensure_github_actions_member()`.
`disable` calls `remove_ci_workflow()`.  `doctor` reports workflow status as
an informational check (not required — the workflow is useful but not essential
for core biff functionality).

`ensure_github_actions_member()` adds `github-actions` to the `.biff` team
roster via targeted regex edit (not parse-serialize) to preserve existing TOML
formatting.  The bot user must be in the roster for `wall` to accept its
messages.

### Module structure

`ci_workflow.py` mirrors `git_hooks.py`: three public functions
(`deploy_ci_workflow`, `remove_ci_workflow`, `check_ci_workflow`) with the same
`repo_root: Path | None` parameter shape and idempotency guarantees.

### Alternatives Rejected

- **Reusable workflow (called workflow):** Requires the caller to add a `uses:`
  job referencing the shared workflow.  Still requires per-workflow editing.
- **GitHub App / webhook:** External infrastructure to maintain.  The
  `workflow_run` trigger achieves the same result with zero servers.
- **Keep inline steps:** Works but doesn't scale.  Every new workflow in every
  repo needs manual editing — the opposite of `biff enable` doing it for you.

---

## DES-026: PreToolUse Hook Deny Reason — `permissionDecisionReason` Not `reason`

**Date:** 2026-03-09
**Status:** Settled

### Problem

Agents blocked by the PreToolUse workflow gate (plan + bead required) could not
see *why* they were denied.  The deny reason contained actionable instructions
("Set a plan with /plan first", "Claim a bead with bd update") but the model
only saw a generic denial.

### Root Cause

The hook output used `"reason"` as the JSON field name:

```python
# WRONG — silently ignored by Claude Code
{"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "reason": "Set a plan with /plan before editing files."
}}
```

Claude Code requires `"permissionDecisionReason"`.  The `"reason"` field is
silently ignored — no error, no warning, no documentation of the correct field
name.  The denial still fires (the tool is blocked), but the model receives no
explanation of *why* or *how to unblock*.

### Fix

Rename the field to `"permissionDecisionReason"`:

```python
# CORRECT — reason visible to the model
{"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "Set a plan with /plan before editing files."
}}
```

### Evidence

Observed in production: an agent was blocked by the gate, saw only "denied"
with no context, and could not self-correct.  After the fix, agents see the
full deny reason and can follow the instructions to unblock (set plan, claim
bead).

### Alternatives Rejected

None — this is a bug fix, not a design choice.  The only correct field name is
`"permissionDecisionReason"`.

### Impact

This is a **silent failure** — the hook appears to work (tool is denied) but
the critical feedback loop (agent reads reason → agent self-corrects) is broken.
Any project using PreToolUse deny hooks must use `"permissionDecisionReason"`,
not `"reason"`.  Propagated to `punt-kit/standards/hooks.md` as a common bug.

## DES-027: Non-Blocking Hook Stdin — `os.read` + `select` Loop

**Date:** 2026-03-09
**Status:** Settled
**Versions:** v1.3.1–v1.3.2

### Problem

Session resume hung indefinitely at "resuming session" on two machines
after updating to v1.3.0.  Disabling biff (removing `.biff.local`)
unblocked resume; re-enabling reproduced the hang.

### Root Cause

`_read_hook_input()` called `sys.stdin.read()`, which blocks until the
writer closes the pipe (EOF).  Claude Code pipes event JSON to hook
subprocesses but does **not** always close the pipe promptly for
`SessionStart` resume/compact events.  The read blocked forever.

Four handlers called `_read_hook_input()` without using the result:

| Handler | Comment |
|---------|---------|
| `cc_session_start` | `data` param had `# noqa: ARG001` |
| `cc_session_resume` | `"consume stdin even if unused"` |
| `cc_session_end` | `"consume stdin"` |
| `cc_stop` | `"consume stdin"` |

The "consume stdin" pattern was defensive (prevent buffering), but
created the exact hang it intended to prevent.

### Decision

1. **Never call `sys.stdin.read()` in hooks.**  Use `os.read(fd, 65536)`
   which returns available bytes without waiting for EOF.
2. **Gate every read with `select.select([fd], [], [], timeout)`.**
   100ms initial timeout, 50ms inter-chunk timeout.  If no data arrives,
   return `{}` immediately.
3. **Remove `_read_hook_input()` from handlers that don't use the data.**
   Four handlers pruned.  `handle_session_start()` signature changed to
   take no arguments.

### Pattern: Non-Blocking Stdin Read

```python
import os
import select

def _read_hook_input() -> dict[str, object]:
    fd = sys.stdin.fileno()
    if not select.select([fd], [], [], 0.1)[0]:
        return {}
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:  # EOF
            break
        chunks.append(chunk)
        if not select.select([fd], [], [], 0.05)[0]:
            break
    raw = b"".join(chunks).decode()
    return json.loads(raw) if raw.strip() else {}
```

### Alternatives Rejected

- **`sys.stdin.readline()`** — also blocks if no newline arrives.
- **`fcntl` `O_NONBLOCK`** — changes fd state globally, requires
  cleanup, complex error handling for `EAGAIN`.
- **`select` + `sys.stdin.read()`** (first attempt, v1.3.1) — reviewers
  correctly flagged: `select` returning readable only guarantees one byte
  won't block; `sys.stdin.read()` still waits for EOF after reading
  initial data.  This was the half-fix that Copilot and Bugbot caught.

### Cross-Project Impact

Any MCP plugin hook that reads stdin with `sys.stdin.read()` is
vulnerable to the same hang.  The quarry plugin had the same pattern
and was independently identified as the actual trigger for the user's
original report.  Propagated the pattern to quarry.

### Test Coverage

Five tests in `TestReadHookInput`:

| Test | Scenario |
|------|----------|
| `test_empty_stdin_returns_empty` | EOF with no data |
| `test_valid_json_parsed` | Normal operation |
| `test_no_eof_does_not_hang` | **Regression**: data on pipe, no EOF |
| `test_no_data_no_eof_returns_empty` | Open pipe, no data, no EOF |
| `test_invalid_json_returns_empty` | Malformed input |

## DES-028: Hook Import Tax — Lightweight Entry Point

**Date:** 2026-03-10
**Status:** Settled
**Version:** v1.3.4

### Problem

Every biff hook shell script invoked `biff hook claude-code <event>`,
which resolved to the `biff` console\_scripts entry point
(`biff.__main__:app`).  This imported the entire application before
dispatching to the hook handler:

```text
biff (uv wrapper) → __main__.py
  → biff/__init__.py → commands, cli_session, config, models, nats_relay, relay
  → typer, nats, pydantic, fastmcp, rich
  → biff.hook (actual handler — needs only stdlib)
```

Measured cold cost (installed binary, M2 MacBook Air):

| Path | Time |
|------|------|
| `biff hook claude-code session-start` | 3.7s |
| Shell + 1 Python hook (startup) | 4.7s |
| Shell + 2 Python hooks (startup + resume) | 5.6s |

With all Punt Labs plugins (biff + quarry + vox + lux) firing
SessionStart hooks serially, observed wall time was ~15s.

### Root Cause

Three layers of unnecessary imports on the hook path:

1. **`biff/__init__.py`** eagerly imported 8 submodules including
   `nats_relay`, `cli_session`, and `models` — every `from biff.X`
   triggered the full package load.
2. **`__main__.py`** imported the server, commands, and CLI framework
   before dispatching to the hook subcommand.
3. **Handler lazy imports** pointed at heavy modules (`biff.config`
   imports pydantic via `biff.models`; `biff.server.tools.plan`
   imports the full server tool chain) even though the handler only
   needed stdlib functions trapped in those modules.

### Solution

Three corresponding fixes:

**1. `biff/_stdlib.py`** — extract stdlib-only functions from heavy
modules so hook handlers can import them without triggering pydantic,
nats, or the server dependency tree:

- `find_git_root`, `get_repo_slug`, `sanitize_repo_name`,
  `is_enabled`, `load_biff_local` (from `config.py`)
- `expand_bead_id` (from `server/tools/plan.py`)
- `active_dir`, `remove_active_session`, `sentinel_dir`
  (from `server/app.py`)

Source modules import from `_stdlib` — no duplication, single source
of truth.

**2. `biff/_hook_entry.py`** — lightweight entry point registered as
`biff-hook` console script.  Parses `sys.argv` directly, imports
`biff.hook` handler functions (which only need typer + stdlib at
module level), dispatches.  Bypasses `__main__.py` entirely.

**3. Lazy `biff/__init__.py`** — PEP 562 `__getattr__` replaces eager
imports.  `from biff import BiffConfig` still works (resolves on first
access), but `from biff._hook_entry import main` no longer triggers
the full package load.

All Claude Code hook shell scripts (`hooks/*.sh`) updated: `biff hook` →
`biff-hook`.  Git hooks deployed by `src/biff/git_hooks.py` into
`.git/hooks/` still use `biff hook` — these invoke the full CLI and are
not on the latency-critical SessionStart path.

### Measured Result

Cold start, installed baseline vs new path (M2 MacBook Air, Python 3.13):

| Scenario | Old | New | Ratio |
|----------|-----|-----|-------|
| 1 Python hook | 3.7s | 0.29s | 13x |
| Shell + 1 Python hook | 4.7s | 0.43s | 11x |
| Shell + 2 Python hooks | 5.6s | 0.56s | 10x |

### Alternatives Rejected

- **Lazy imports in `__main__.py`** — would help but doesn't eliminate
  the fundamental problem: the `biff` entry point exists to run the
  full CLI, not lightweight hooks.  A separate entry point is cleaner.
- **`python -m biff.hook`** — requires knowing which Python to invoke
  (the one in biff's tool venv).  Console scripts handle this.
- **Moving `_hook_entry.py` outside the `biff` package** — would avoid
  the `__init__.py` problem without making it lazy, but creates a
  non-standard package layout and makes the module harder to find.
- **Merging 3 SessionStart hooks into 1** — the matchers already
  ensure only 2 fire per event (shell + one Python).  With the import
  fix reducing Python cost to ~0.3s, merging saves ~0.3s more — not
  worth the complexity.

### Invariant

Every function in `biff/_stdlib.py` uses only stdlib imports.  Adding
a third-party import there defeats the entire purpose.  The module
docstring states this explicitly.
