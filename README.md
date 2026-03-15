# biff

<img src="https://raw.githubusercontent.com/punt-labs/biff/main/docs/biff.png" alt="The original biff mail notification app" width="116" align="right">

> Team communication for engineers who never leave the terminal.

[![License](https://img.shields.io/github/license/punt-labs/biff)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/punt-labs/biff/test.yml?label=CI)](https://github.com/punt-labs/biff/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/punt-biff)](https://pypi.org/project/punt-biff/)
[![Python](https://img.shields.io/pypi/pyversions/punt-biff)](https://pypi.org/project/punt-biff/)
[![Working Backwards](https://img.shields.io/badge/Working_Backwards-hypothesis-lightgrey)](./prfaq.pdf)

Named after the Berkeley dog whose 1980 mail notification program was part of the same BSD family as `talk`, `wall`, `finger`, `who`, and `mesg`. Biff resurrects the Unix communication vocabulary as MCP-native slash commands inside Claude Code.

**Platforms:** macOS, Linux

## Quick Start

```bash
curl -fsSL https://raw.githubusercontent.com/punt-labs/biff/be375b6/install.sh | sh
```

Restart Claude Code twice. Type `/who` to see your team.

<details>
<summary>Manual install (if you already have uv)</summary>

```bash
uv tool install punt-biff
biff install
biff doctor
```

</details>

<details>
<summary>Verify before running</summary>

```bash
curl -fsSL https://raw.githubusercontent.com/punt-labs/biff/be375b6/install.sh -o install.sh
shasum -a 256 install.sh
cat install.sh
sh install.sh
```

</details>

## Features

- **MCP-native** --- runs inside Claude Code as slash commands, no separate app
- **Interactive REPL** --- `biff` launches a terminal client with readline, real-time notifications, and modal talk
- **BSD vocabulary** --- `/who`, `/write`, `/talk`, `/wall` --- commands engineers already know
- **NATS relay** --- cross-machine presence and messaging over encrypted connections
- **Agent-first** --- agents show up in `/who` alongside humans, coordinate via `/plan` and `/write`
- **Status bar** --- live unread count, wall broadcasts, talk messages --- wraps your existing status line
- **Zero config** --- installs in one command, activates per-repo with `/biff y`

## What It Looks Like

### See who's online

```text
> /who

▶  NAME    TTY   IDLE  S  HOST       DIR                        PLAN
   @kai    tty1  0:03  +  m2-mb-air  /Users/kai/code/myapp      refactoring auth module
   @eric   tty2  1:22  +  m2-mb-air  /Users/eric/code/myapp     reviewing PR #47
   @priya  tty1  0:00  +  priya-mbp  /Users/priya/code/myapp    writing integration tests
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

<details>
<summary>More examples: /finger, /plan, /last, /wall, /talk, /mesg</summary>

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

Bead IDs auto-expand:

```text
> /plan biff-ka4

Plan: biff-ka4: post-checkout hook: update plan from branch
```

### Session history

```text
> /last

▶  NAME    TTY   HOST       LOGIN             LOGOUT            DURATION
   @kai    tty3  m2-mb-air  Sat Feb 22 14:01  still logged in   -
   @kai    tty2  m2-mb-air  Sat Feb 22 11:30  Sat Feb 22 13:58  2:28
   @eric   tty1  m2-mb-air  Sat Feb 22 09:15  Sat Feb 22 12:45  3:30
```

### Broadcast to the team

```text
> /wall "release freeze --- do not push to main" 2h

Wall posted (2h): release freeze --- do not push to main
```

Every teammate's status bar shows `WALL: release freeze` in bold red. Expires automatically.

### Talk in real time

```text
> /talk @kai "can you review PR #42?"

Waiting for @kai to respond...
Connected to @kai. Type 'end' to return.
```

BSD-style two-phase handshake: the inviter waits, the target accepts. Messages are ephemeral (NATS core pub/sub, no inbox). Either side types `end` to hang up.

### Go do-not-disturb

```text
> /mesg n

is n
```

Your status bar shows `(n)` instead of the unread count. Messages still accumulate --- `/mesg y` or `/read` reveals them.

</details>

## Commands

| Command | Origin | Purpose |
|---------|--------|---------|
| `/write @user "text"` | BSD `write` | Send a message |
| `/read` | BSD `from` | Check your inbox |
| `/finger @user` | BSD `finger` | Check what someone is working on |
| `/who` | BSD `who` | List active sessions |
| `/last` | BSD `last` | Show session login/logout history |
| `/plan "text"` | BSD `.plan` | Set your status |
| `/tty "name"` | BSD `tty` | Name the current session |
| `/talk @user "msg"` | BSD `talk` | Start a real-time conversation |
| `/wall "text"` | BSD `wall` | Broadcast to the team |
| `/mesg y` \| `/mesg n` | BSD `mesg` | Control message reception |

## CLI

Every slash command has a matching `biff` CLI command. The CLI works outside Claude Code --- from any terminal, SSH session, or CI script.

### Interactive REPL

```bash
biff                                       # Launch interactive REPL
```

```text
biff 0.15.1 — kai:tty1
Commands: finger, last, mesg, plan, read, status, tty, wall, who, write, talk, exit

kai:tty1 ▶ who
▶  NAME    TTY   IDLE  S  HOST       DIR              PLAN
   @kai    tty1  0:00  +  m2-mb-air  /code/myapp      debugging auth
   @eric   tty2  1:22  +  m2-mb-air  /code/myapp      reviewing PR #47
kai:tty1 ▶ talk @eric
Waiting for @eric:tty2 to respond... (type 'end' to cancel)
Connected to @eric:tty2. Type 'end' to return to REPL.

kai:tty1 ▶ can you look at the auth fix?
eric:tty2 ▶ on it now
kai:tty1 ▶ end
Talk with @eric:tty2 ended.
kai:tty1 ▶ exit
```

The REPL provides a proper session lifecycle (login/logout events, heartbeat, KV presence), readline (history, tab completion), and real-time notifications (wall broadcasts and message alerts appear while you're idle at the prompt). `biff` with no args is the primary mode; `biff who` is a shortcut for a one-command session.

### Product commands

```bash
biff who                                   # List active sessions
biff finger @kai                           # Check what someone is working on
biff write @kai "review the PR"            # Send a message
biff read                                  # Check your inbox
biff plan "debugging websocket reconnect"  # Set your status
biff last                                  # Session login/logout history
biff last @kai --count 10                  # Filter by user, limit results
biff wall "deploy freeze" --duration 2h    # Broadcast to the team
biff wall                                  # Read active wall
biff wall --clear                          # Remove active wall
biff mesg off                              # Go do-not-disturb
biff mesg on                               # Accept messages again
biff tty dev                               # Name this session "dev"
biff status                                # Connection state + unread count
biff talk @kai "can you review?"           # Real-time conversation
```

### Admin commands

```bash
biff install                # Install plugin via marketplace
biff enable                 # Activate biff in current repo (creates .biff)
biff disable                # Deactivate biff in current repo
biff doctor                 # Check installation health
biff mcp                    # Start MCP server (stdio, called by plugin)
biff serve                  # Start MCP server (HTTP)
biff uninstall              # Remove plugin and clean up
biff version                # Print version
```

### Global flags

```bash
biff --json who             # JSON array of sessions
biff --json status          # JSON object with version, unread, wall
biff --json read            # JSON array of messages
biff --verbose who          # Debug logging to stderr
biff --quiet write @kai "msg"  # Suppress non-JSON output
biff --user github-actions wall "CI failed"  # Identity override for bots
```

Global flags (`--json`, `--verbose`, `--quiet`, `--user`) go before the subcommand.

### Library API

Commands are also importable as pure async functions:

```python
from biff import commands, CliContext, CommandResult
from biff.relay import LocalRelay

relay = LocalRelay(data_dir)
ctx = CliContext(relay=relay, config=config, session_key="kai:abc123", user="kai", tty="abc123")

result: CommandResult = await commands.who(ctx)
print(result.text)       # Human-readable output
print(result.json_data)  # JSON-serializable data
print(result.error)      # True if command failed
```

All 10 product commands (`who`, `finger`, `write`, `read`, `plan`, `last`, `wall`, `mesg`, `tty`, `status`) follow this pattern. See the [design log](DESIGN.md#des-022-library-api--command-extraction-via-humble-object-pattern) for architecture details.

## Setup

Biff requires a git repo and a GitHub identity. Your username and display name are resolved automatically from `gh auth` --- no manual configuration needed.

### Create a `.biff` file

Commit a `.biff` file in your repo root (TOML format):

```toml
[team]
members = ["kai", "eric", "priya"]

[relay]
url = "tls://connect.ngs.global"
```

Biff ships with a shared demo relay so your team can start immediately. When you're ready for your own relay, see [relay configuration](docs/INSTALLING.md#relay-configuration).

`biff install` registers the MCP server, installs slash commands, and enables the plugin. `biff enable` activates biff in the current repo and deploys git hooks. Run `biff doctor` to verify everything is wired up. See [Installing](docs/INSTALLING.md) for the full guide.

## Status Bar

Biff appends to your existing Claude Code status line --- it never replaces it:

```text
Line 1: your-existing-status | kai:tty1(3)
Line 2: ▶ WALL: release freeze until 5pm
```

Three states: `kai:tty1(0)` when caught up, **`kai:tty1(3)`** (bold yellow) with unreads, `kai:tty1(n)` when messages are off. Line 2 shows active `/talk` messages (bold yellow), wall broadcasts (bold red), or an idle marker.

## Agents Welcome

Because biff speaks MCP, it does not distinguish between human and agent sessions. An autonomous coding agent can `/plan` what it's working on, `/write` a human when it needs a decision, and show up in `/who` alongside everyone else.

Biff coordinates hybrid teams across two planes:

- **Logical plane (cross-machine):** `/plan` shows the task each agent is working on. `/who` shows all plans across all machines. This prevents duplicate work.
- **Physical plane (same-machine):** `/who` shows host and directory per session. When two agents share the same machine and directory, they coordinate via `/write` and create git worktrees to work in isolation.

See [Agent Workflow](docs/AGENT_WORKFLOW.md) for patterns and examples.

## Vision

Biff assumes the terminal is where you're already working — so that's where your team communication should be. Every command implies intent. There are no channels to monitor, no threads to catch up on, no emoji reactions to parse. Communication is pull-based: you decide when to engage.

## Roadmap

### Shipped

- Presence: `/who`, `/finger`, `/plan`, `/tty`, `/last`
- Messaging: `/write`, `/read`, `/mesg`
- Broadcast: `/wall` with duration-based expiry
- Real-time: `/talk` with two-phase handshake and mutual hangup
- NATS relay for cross-machine communication
- Per-project activation (`/biff y`) with lazy connection management
- Status bar with live unread count, wall, and talk display
- Workflow hooks: plan auto-expand, session lifecycle, git integration
- CLI parity: every MCP tool available as `biff <command>` with `--json` output
- Interactive REPL: `biff` with readline, real-time notifications, modal talk
- Library API: pure async functions for programmatic use and testing
- Notification deferral: ≤2s latency for wall and talk in all states (active and napping)
- Formal verification: Z specifications for talk and REPL, ProB model-checked

### Next

| Phase | What Ships |
|-------|-----------|
| **Security** | E2E encryption (NaCl/libsodium), GitHub identity and auth, per-repo NATS credentials |
| **Real-time** | `/pair` for session sharing with explicit consent |
| **Hosted relay** | Managed service with admin controls, audit logs, team isolation |

## Documentation

[Installing](docs/INSTALLING.md) |
[Agent Workflow](docs/AGENT_WORKFLOW.md) |
[Claude Setup](docs/CLAUDE_SETUP.md) |
[FAQ](docs/FAQ.md) |
[Troubleshooting](docs/TROUBLESHOOTING.md)

[Design Log](DESIGN.md) |
[Installer Design](DESIGN-INSTALLER.md) |
[Testing Guide](TESTING.md) |
[Changelog](CHANGELOG.md) |
[Contributing](CONTRIBUTING.md)

## Development

```bash
uv sync --extra dev        # Install dependencies
make check                 # Run all quality gates (lint, type, test)
make test                  # Tests only (unit + integration)
make lint                  # Lint and format check
make format                # Auto-format code
make help                  # List all targets
```

## License

MIT
