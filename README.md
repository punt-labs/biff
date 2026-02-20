# biff

<img src="docs/biff.png" alt="The original biff mail notification app" width="116" align="right">

> Team communication for engineers who never leave the terminal.

Named after the Berkeley dog whose 1980 mail notification program was part of the same BSD family as `talk`, `wall`, `finger`, `who`, and `mesg`.

Biff resurrects the Unix communication vocabulary as MCP-native slash commands. It runs inside your Claude Code session — no separate app, no browser tab, no context switch.

## Why

Engineers using AI coding tools are shipping faster than ever. But every time they need to coordinate with a teammate — or with another agent — they context-switch to Slack or Discord. Tools designed for managers, not makers in deep focus. Biff keeps communication where the code already lives.

## Quick Start

```bash
git clone https://github.com/punt-labs/biff.git
cd biff
uv tool install --editable .
biff install
biff doctor
```

Restart Claude Code. Type `/who` to see your team.

## What It Looks Like

### See who's online

```text
> /who

▶  NAME    TTY   IDLE  S  HOST       DIR                        PLAN
   @kai    tty1  0:03  +  m2-mb-air  /Users/kai/code/myapp      refactoring auth module
   @eric   tty2  1:22  +  m2-mb-air  /Users/eric/code/myapp     reviewing PR #47
   @priya  tty1  0:00  +  priya-mbp  /Users/priya/code/myapp    writing integration tests
   @dana   tty1  3:45  -  dana-mbp   /Users/dana/code/myapp     (no plan)
```

`S` is message status: `+` means accepting messages, `-` means do not disturb.

### Send a message

```text
> /write @kai "auth module looks good, just one nit on the error handling"

Message sent to @kai.
```

### Check your inbox

```text
> /read

▶  FROM   DATE              MESSAGE
   kai    Sat Feb 15 14:01  hey, ready for review?
   eric   Sat Feb 15 13:45  pushed the fix for the flaky test
   priya  Sat Feb 15 12:30  can you look at the migration script?
```

### Check what someone is working on

```text
> /finger @kai

▶  Login: kai                              Messages: on
   On since Sat Feb 15 14:01 (UTC) on tty1, idle 0:03
   Host: m2-mb-air  Dir: /Users/kai/code/myapp
   Plan:
    refactoring auth module
```

### Set your status

```text
> /plan "debugging the websocket reconnect logic"

Plan: debugging the websocket reconnect logic
```

### Go do-not-disturb

```text
> /mesg n

is n
```

Your status bar shows `(n)` instead of the unread count while messages are off. Messages still accumulate — `/mesg y` or `/read` reveals them.

## Commands

| Command | Origin | Purpose |
|---------|--------|---------|
| `/write @user "text"` | BSD `write` | Send a message |
| `/read` | BSD `from` | Check your inbox |
| `/finger @user` | BSD `finger` | Check what someone is working on |
| `/who` | BSD `who` | List active sessions |
| `/plan "text"` | BSD `.plan` | Set your status |
| `/tty "name"` | BSD `tty` | Name the current session |
| `/mesg y` \| `/mesg n` | BSD `mesg` | Control message reception |

## Status Bar

Biff appends to your existing Claude Code status line — it never replaces it. If you already have a status line command, biff wraps it and adds unread counts at the end:

```text
your-existing-status | kai:tty1(3)
```

Three states: `kai:tty1(0)` when caught up, **`kai:tty1(3)`** (bold yellow) with unreads, `kai:tty1(n)` when messages are off.

`biff install` includes status bar setup. For standalone management: `biff install-statusline` / `biff uninstall-statusline`.

## Agents Welcome

Because biff speaks MCP, it does not distinguish between human and agent sessions. An autonomous coding agent can `/plan` what it's working on, `/write` a human when it needs a decision, and show up in `/who` alongside everyone else.

But presence is just the beginning. When you have multiple agents working in the same codebase — on the same machine, in the same directory — they need to coordinate to avoid stepping on each other's files. Biff solves this with two coordination planes:

**Logical plane (cross-machine):** What work is everyone doing? `/plan` shows the task each agent is working on. `/who` shows all plans across all machines. This prevents duplicate work.

**Physical plane (same-machine):** Are we sharing a filesystem? `/who` shows host and directory per session. When two agents share the same machine and directory, they coordinate via `/write` and create git worktrees to work in isolation.

Biff is the communication layer for the entire hive of humans and agents building software together.

## Vision

Biff is built on a simple thesis: the terminal is the new center of gravity for software engineering, and the communication tools haven't caught up. Slack was built for the open-office, always-online workplace. Biff is built for the deep-focus, AI-accelerated one.

Every command implies intent. There are no channels to monitor, no threads to catch up on, no emoji reactions to parse. Communication is pull-based: you decide when to engage.

As engineering teams grow to include both humans and autonomous agents, coordination becomes the bottleneck. Biff provides the primitives — presence, messaging, broadcast, targeted delivery — that let a mixed team of humans and agents work together without stepping on each other.

## Roadmap

### Shipped

Core communication is live: presence (`/who`, `/finger`, `/plan`), messaging (`/write`, `/read`), and availability control (`/mesg`) — all working over a NATS relay for cross-machine communication.

TTY sessions (`/tty`) give each agent a distinct identity — one user with 3 sessions shows 3 entries in `/who`, targetable via `/write @user:tty`. Enriched presence shows host and directory per session. Per-session status bar with `user:tty(N)` format. `/mesg n` suppresses the unread count on the status line.

### Next: Agentic Coordination

| Feature | What It Enables |
|---------|----------------|
| **`/wall` broadcast** | Time-sensitive announcements visible on every terminal's status bar with automatic expiry. |
| **Plan auto-expand** | `/plan biff-bf8` auto-expands to show the task title. Everyone sees what you're working on. |
| **Workflow hooks** | Claiming a task auto-sets your plan. Creating a PR triggers an announcement. |
| **Project opt-in** | `/biff y` enables the coordination workflow per project via AGENTS.md. |

### Future: Real-Time and Security

| Phase | What Ships |
|-------|-----------|
| **Network relay** | E2E encryption (NaCl/libsodium), GitHub identity and auth, per-repo NATS credentials |
| **Real-time** | `/talk` for live conversation, `/pair` for session sharing with explicit consent |
| **Hosted relay** | Managed service with admin controls, audit logs, team isolation |

---

## Setup

Biff requires a git repo and a GitHub identity. Your username and display name are resolved automatically from `gh auth` — no manual configuration needed.

### 1. Create a `.biff` file

Commit a `.biff` file in your repo root (TOML format):

```toml
[team]
members = ["kai", "eric", "priya"]

[relay]
url = "tls://connect.ngs.global"
```

Biff ships with a shared demo relay so your team can start immediately. When you're ready for your own relay, see the [relay configuration](#relay-configuration) section below.

`biff install` (from Quick Start) registers the MCP server, installs slash commands, and enables the plugin — there is no separate "start the server" step.

## Development

```bash
uv sync --extra dev        # Install dependencies
uv run ruff check .        # Lint
uv run ruff format .       # Format
uv run mypy src/ tests/    # Type check
uv run pytest              # Test (unit + integration)
uv run pytest -m nats      # NATS tests (requires local nats-server)
uv run pytest -m hosted    # Hosted NATS tests (see below)
```

### Hosted NATS tests

Tests against a real hosted NATS account (Synadia Cloud or self-hosted):

```bash
BIFF_TEST_NATS_URL=tls://connect.ngs.global \
BIFF_TEST_NATS_CREDS=/path/to/user.creds \
    uv run pytest -m hosted -v
```

Environment variables (set exactly one auth var, or none for anonymous):

| Variable | Purpose |
|----------|---------|
| `BIFF_TEST_NATS_URL` | Required. Server URL (e.g. `tls://connect.ngs.global`) |
| `BIFF_TEST_NATS_TOKEN` | Token auth |
| `BIFF_TEST_NATS_NKEYS_SEED` | Path to NKey seed file |
| `BIFF_TEST_NATS_CREDS` | Path to credentials file |

### Relay configuration

The demo relay works out of the box. To run your own NATS relay, update `.biff`:

```toml
[relay]
url = "tls://your-nats-server:4222"

# Authentication (pick at most one):
# token = "s3cret"                          # shared secret
# nkeys_seed = "/path/to/user.nk"          # NKey seed file
# user_credentials = "/path/to/user.creds" # JWT + NKey creds (Synadia Cloud)
```

Use `nats://` for unencrypted local connections, `tls://` for encrypted remote connections.

## License

MIT
