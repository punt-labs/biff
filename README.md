# biff

<img src="docs/biff.png" alt="The original biff mail notification app" width="116" align="right">

> Team communication for engineers who never leave the terminal.

Named after the Berkeley dog whose 1980 mail notification program was part of the same BSD family as `talk`, `wall`, `finger`, `who`, and `mesg`.

Biff resurrects the Unix communication vocabulary as MCP-native slash commands. It runs inside your Claude Code session — no separate app, no browser tab, no context switch.

## Why

Engineers using AI coding tools are shipping faster than ever. But every time they need to coordinate with a teammate, they context-switch to Slack or Discord — tools designed for managers, not makers in deep focus. Biff keeps communication where the code already lives.

## Quick Start

```bash
pip install biff-mcp
biff install-statusline
```

Restart Claude Code. Type `/who` to see your team.

## What It Looks Like

### See who's online

```
> /who

▶  NAME    IDLE   S  PLAN
   @kai    0:03   +  refactoring auth module
   @eric   1:22   +  reviewing PR #47
   @priya  0:00   +  writing integration tests
   @dana   3:45   -  (no plan)
```

`+` means accepting messages, `-` means do not disturb.

### Send a message

```
> /write @kai "auth module looks good, just one nit on the error handling"

Message sent to @kai.
```

### Check your inbox

```
> /read

▶  FROM   DATE              MESSAGE
   kai    Sat Feb 15 14:01  hey, ready for review?
   eric   Sat Feb 15 13:45  pushed the fix for the flaky test
   priya  Sat Feb 15 12:30  can you look at the migration script?
```

### Check what someone is working on

```
> /finger @kai

▶  Login: kai                              Messages: on
   On since Sat Feb 15 14:01 (UTC) on claude, idle 0:03
   Plan:
    refactoring auth module
```

### Set your status

```
> /plan "debugging the websocket reconnect logic"

Plan: debugging the websocket reconnect logic
```

### Go do-not-disturb

```
> /mesg n

is n
```

## Commands

| Command | Origin | Purpose |
|---------|--------|---------|
| `/write @user "text"` | BSD `write` | Send a message |
| `/read` | BSD `from` | Check your inbox |
| `/finger @user` | BSD `finger` | Check what someone is working on |
| `/who` | BSD `who` | List active sessions |
| `/plan "text"` | BSD `.plan` | Set your status |
| `/mesg y` \| `/mesg n` | BSD `mesg` | Control message reception |

## Status Bar

Biff appends to your existing Claude Code status line — it never replaces it. If you already have a status line command, biff wraps it and adds unread counts at the end:

```
your-existing-status | biff(3)
```

This is configured automatically by `biff install-statusline`. To remove and restore your original status line: `biff uninstall-statusline`.

## Agents Welcome

Because biff speaks MCP, it does not distinguish between human and agent sessions. An autonomous coding agent can `/plan` what it's working on, `/write` a human when it needs a decision, and show up in `/who` alongside everyone else. Biff is the communication layer for the entire hive of humans and agents building software together.

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

`biff install-statusline` (from Quick Start) registers the MCP server automatically — there is no separate "start the server" step.

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
