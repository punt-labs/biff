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

```
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
| Wtmp peek | `wtmp-peek-{name}` | Unchanged — `{name}` already includes repo via `biff-{repo}-{user}` |

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

### Alternatives Considered

| Alternative | Rejected Because |
|-------------|-----------------|
| Ship wg4 without encryption awareness | Second breaking migration when lff ships. KV key namespace collisions. Message format incompatibility. Costs ~3 hours to avoid. |
| Ship wg4 + lff together | 3-4x implementation cost. Blocks P1 stream fix for months. Encryption requires PyNaCl, key generation, key distribution protocol, trust model. |
| `biff.>` as shared stream filter | Two streams cannot both claim `biff.>`. Narrow filters (`biff.*.inbox.>`, `biff.*.wtmp.>`) partition cleanly. |
| `.biff` config flag for migration | Two code paths doubles surface area. Pre-1.0 breaking changes are acceptable. |
| Separate KV bucket for encryption keys | Burns another stream slot, defeating wg4's purpose. Session entries are the natural home for public keys. |
