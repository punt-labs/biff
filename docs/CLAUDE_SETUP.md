# Claude Code Setup

Biff is a Claude Code plugin that provides MCP tools and slash commands. This guide covers how biff integrates with Claude Code and how to configure it.

## How It Works

Biff runs as an MCP server inside your Claude Code session. When you type `/who`, Claude Code routes the command to biff's MCP server, which queries the NATS relay and returns formatted output.

The architecture:

```text
Claude Code session
  ├── MCP server (biff)
  │     ├── NATS relay connection (presence, messaging)
  │     ├── Status bar integration (unread count, wall, talk)
  │     └── Tool handlers (/who, /write, /read, etc.)
  ├── Hooks
  │     ├── SessionStart (auto-setup: tty, plan, unread check)
  │     ├── PostToolUse (display formatting)
  │     └── SessionEnd (presence cleanup)
  └── Slash commands (/who, /write, /read, /talk, etc.)
```

## Plugin Files

After `biff install`, these files exist:

| Path | Purpose |
|------|---------|
| `~/.claude/plugins/biff/` | Plugin root (commands, hooks, agents) |
| `~/.claude/plugins/biff/.claude-plugin/plugin.json` | Plugin manifest |
| `~/.biff/` | Runtime state directory |
| `~/.biff/unread/` | Per-session unread status files (for status bar) |
| `~/.biff/statusline-original.json` | Stashed original status line (if any) |

## Status Bar

Biff wraps your existing Claude Code status line. If you had a custom `statusLine` command before installing biff, it's stashed and its output is preserved --- biff appends its segment at the end.

The status bar shows two lines:

```text
Line 1: your-existing-status | kai:tty1(3)
Line 2: ▶ @eric: can you review PR #42?
```

**Line 1** segments:

- `kai:tty1(0)` --- caught up, no unreads
- `kai:tty1(3)` --- 3 unread messages (bold yellow)
- `kai:tty1(n)` --- messages off (`/mesg n`)

**Line 2** priority:

1. Active `/talk` message (bold yellow)
2. Active `/wall` broadcast (bold red)
3. Idle marker (`▶`)

### Managing the Status Bar

```bash
biff install-statusline    # Install (done automatically by biff install)
biff uninstall-statusline  # Remove biff from status bar, restore original
```

## Hooks

Biff installs three Claude Code hooks:

### SessionStart

Runs when a Claude Code session begins. Automatically:

- Assigns a TTY identifier
- Sets your plan from the current git branch
- Checks for unread messages

### PostToolUse

Runs after every biff MCP tool call. Formats output for display (the `▶` prefix, table alignment, etc.).

### SessionEnd

Runs when a Claude Code session closes. Cleans up the presence entry immediately instead of waiting for TTL expiry.

## Per-Repo Activation

Biff starts dormant in every repo. No NATS connection, no consumers, no status bar updates. To activate:

```text
> /biff y
```

This creates `.biff.local` (automatically gitignored). To deactivate:

```text
> /biff n
```

## Git Hooks

`biff enable` deploys git hooks into `.git/hooks/`:

- **post-checkout** --- updates your `/plan` when you switch branches
- **post-commit** --- updates your `/plan` with the commit message
- **pre-push** --- suggests a `/wall` announcement when pushing to main

All hooks:

- Coexist with existing git hooks (including beads)
- Gate on `.biff.local` --- silent when biff is not enabled
- Are lightweight shell scripts that call `biff` CLI commands

## Identity

Biff resolves your identity from GitHub:

```bash
gh auth status   # Your GitHub username becomes your biff identity
```

Your display name is pulled from your GitHub profile. No separate biff account or configuration is needed.

## Troubleshooting

See [Troubleshooting](TROUBLESHOOTING.md) for common issues and solutions.
