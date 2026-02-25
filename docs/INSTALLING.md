# Installing Biff

## Requirements

- **Python 3.13+**
- **macOS or Linux** (Windows is not supported)
- **Claude Code** CLI (`claude` on PATH)
- **GitHub CLI** (`gh auth login` completed --- biff resolves your identity from GitHub)

## One-Line Install

```bash
curl -fsSL https://raw.githubusercontent.com/punt-labs/biff/6ce60b3/install.sh | sh
```

This installs [uv](https://docs.astral.sh/uv/) (if missing), installs `punt-biff` as a uv tool, registers the Claude Code plugin, and runs `biff doctor` to verify.

Restart Claude Code twice after installing:

1. First restart --- SessionStart hook runs initial setup
2. Second restart --- slash commands become active (`/who`, `/write`, etc.)

### Verify before running

```bash
curl -fsSL https://raw.githubusercontent.com/punt-labs/biff/6ce60b3/install.sh -o install.sh
shasum -a 256 install.sh
cat install.sh
sh install.sh
```

## Manual Install

If you already have `uv`:

```bash
uv tool install punt-biff
biff install
biff doctor
```

Or with pip:

```bash
pip install punt-biff
biff install
biff doctor
```

## What `biff install` Does

1. Registers the biff MCP server with Claude Code
2. Installs slash commands (`/who`, `/write`, `/read`, etc.)
3. Installs hooks (SessionStart, PostToolUse, SessionEnd)
4. Sets up the status bar (wraps your existing status line)

All files are deployed to `~/.claude/plugins/biff/`. The MCP server runs automatically when Claude Code starts.

## Enabling Biff in a Repo

Biff starts dormant in every repo. To activate:

```text
> /biff y
```

Or from the CLI:

```bash
biff enable
```

This creates a `.biff.local` file (gitignored) and deploys git hooks:

- **post-checkout** --- updates your plan when you switch branches
- **post-commit** --- updates your plan with the latest commit message
- **pre-push** --- suggests a `/wall` announcement when pushing to main

All hooks coexist with existing git hooks and are silent when biff is not enabled.

## Team Configuration

Commit a `.biff` file in your repo root:

```toml
[team]
members = ["kai", "eric", "priya"]

[relay]
url = "tls://connect.ngs.global"
```

The `members` list controls who appears in `/who`. The `relay` section configures the NATS server for cross-machine communication.

Biff ships with a shared demo relay on Synadia Cloud so your team can start immediately.

## Relay Configuration

The demo relay works out of the box. To run your own NATS server:

```toml
[relay]
url = "tls://your-nats-server:4222"

# Authentication (pick at most one):
# token = "s3cret"                          # shared secret
# nkeys_seed = "/path/to/user.nk"          # NKey seed file
# user_credentials = "/path/to/user.creds" # JWT + NKey creds (Synadia Cloud)
```

Use `nats://` for unencrypted local connections, `tls://` for encrypted remote connections.

## Updating

```bash
uv tool install --force punt-biff
biff install
```

Or if installed via the plugin marketplace:

```bash
claude plugin update biff@punt-labs
```

## Uninstalling

```bash
biff uninstall
uv tool uninstall punt-biff
```

This removes the MCP server registration, slash commands, hooks, and status bar integration. Your messages on the relay are ephemeral and expire automatically.

## Verifying Your Installation

Run `biff doctor` at any time to check:

```bash
biff doctor
```

It verifies: Python version, uv installation, Claude Code CLI, MCP server registration, plugin files, status bar configuration, and `.biff` team file.
