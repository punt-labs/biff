# Biff Installer Design

This document describes the architecture of biff's installation system — how the PyPI package, CLI commands, Claude Code plugin system, MCP server registration, and status line integration fit together.

## Rules

1. Before proposing ANY change to the installer, consult this document for prior decisions.
2. Do not revisit a settled decision without new evidence.
3. Log the decision, alternatives considered, and outcome.

---

## Installation Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                        User's Machine                                    │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │                     pip install punt-biff                           │  │
│  │                                                                    │  │
│  │  Installs into site-packages:                                      │  │
│  │    biff/                                                           │  │
│  │    ├── __main__.py          CLI entry (typer)                      │  │
│  │    ├── installer.py         Plugin + MCP installer                 │  │
│  │    ├── statusline.py        Status bar installer                   │  │
│  │    ├── doctor.py            Environment diagnostics                │  │
│  │    ├── config.py            .biff file + identity resolution       │  │
│  │    ├── server/              MCP server (FastMCP)                   │  │
│  │    ├── data/                                                       │  │
│  │    │   └── demo.creds       Bundled NATS demo credentials         │  │
│  │    └── plugins/                                                    │  │
│  │        └── biff/            ◄── Bundled plugin source              │  │
│  │            ├── .claude-plugin/plugin.json                          │  │
│  │            ├── commands/    Slash command prompts (.md)            │  │
│  │            └── hooks/       PostToolUse hook (suppress-output.sh)  │  │
│  │                                                                    │  │
│  │  Creates CLI tool:                                                 │  │
│  │    biff  →  biff.__main__:app                                      │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│           │                                                              │
│           │ biff install                                                  │
│           ▼                                                              │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │                     installer.py (5 steps)                         │  │
│  │                                                                    │  │
│  │  Step 1: Register MCP server                                      │  │
│  │    $ claude mcp add --scope user biff -- biff serve --transport    │  │
│  │      stdio                                                         │  │
│  │    Writes to: ~/.claude.json → mcpServers.biff                    │  │
│  │                                                                    │  │
│  │  Step 2: Copy plugin files                                        │  │
│  │    importlib.resources("biff.plugins.biff")                        │  │
│  │      → shutil.copytree → ~/.claude/plugins/biff/                  │  │
│  │    Copies: .claude-plugin/plugin.json, commands/*.md,             │  │
│  │            hooks/suppress-output.sh                                │  │
│  │                                                                    │  │
│  │  Step 3: Copy user commands                                       │  │
│  │    plugin_source() / "commands" / *.md                            │  │
│  │      → shutil.copy2 → ~/.claude/commands/                         │  │
│  │                                                                    │  │
│  │  Step 4: Register in plugin registry                              │  │
│  │    ~/.claude/plugins/installed_plugins.json                        │  │
│  │    Adds: { "biff@local": [{ scope, installPath, version, ... }] } │  │
│  │                                                                    │  │
│  │  Step 5: Enable in settings                                       │  │
│  │    ~/.claude/settings.json                                         │  │
│  │    Adds: { "enabledPlugins": { "biff@local": true } }             │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│           │                                                              │
│           │ biff install-statusline (optional, separate command)          │
│           ▼                                                              │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │                    statusline.py                                   │  │
│  │                                                                    │  │
│  │  1. Stash original statusLine value                               │  │
│  │     ~/.biff/statusline-original.json                               │  │
│  │                                                                    │  │
│  │  2. Replace statusLine in settings.json                           │  │
│  │     { "type": "command", "command": "biff statusline" }           │  │
│  │                                                                    │  │
│  │  3. Reconcile MCP server in ~/.claude.json (idempotent)           │  │
│  │     Ensures mcpServers.biff entry exists and matches expected      │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│           After restart, Claude Code loads:                               │
│                                                                          │
│  ┌─────────────────────────┐  ┌──────────────────────────────────────┐  │
│  │     MCP Server          │  │         Plugin                        │  │
│  │  (from ~/.claude.json)  │  │  (from ~/.claude/plugins/biff/)      │  │
│  │                         │  │                                       │  │
│  │  Spawns:                │  │  Loads:                               │  │
│  │    biff serve           │  │    commands/*.md  → /who, /finger,   │  │
│  │    --transport stdio    │  │                     /write, /read,   │  │
│  │                         │  │                     /plan, /mesg     │  │
│  │  Provides MCP tools:    │  │    hooks/          → suppress-       │  │
│  │    who, finger, write,  │  │      suppress-       output.sh      │  │
│  │    read_messages, plan, │  │      output.sh       (PostToolUse)  │  │
│  │    mesg, wall           │  │                                       │  │
│  └────────┬────────────────┘  └──────────────┬───────────────────────┘  │
│           │                                   │                          │
│           │  tool call                        │  formats output          │
│           └──────────────────┬────────────────┘                          │
│                              ▼                                           │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │                   Runtime (after restart)                          │  │
│  │                                                                    │  │
│  │  User types /who  →  Skill prompt (who.md) tells model to call    │  │
│  │    who tool  →  MCP server returns session table  →  PostToolUse  │  │
│  │    hook formats output  →  Panel shows "3 online", model emits    │  │
│  │    full table via additionalContext                                │  │
│  │                                                                    │  │
│  │  Status line (if installed):                                      │  │
│  │    Claude Code calls: biff statusline                             │  │
│  │    → reads ~/.biff/unread/*.json for per-project counts           │  │
│  │    → runs stashed original command (if any)                       │  │
│  │    → outputs: "original-output | biff(2) myapp(1)"               │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
```

## File Locations (User's Machine)

| Path | Written By | Purpose |
|------|-----------|---------|
| `~/.claude.json` | `claude mcp add` + `statusline.py` | MCP server registration |
| `~/.claude/plugins/biff/` | `installer.py` | Plugin files (commands, hooks) |
| `~/.claude/commands/` | `installer.py` | Top-level user commands (`.md`) |
| `~/.claude/plugins/installed_plugins.json` | `installer.py` | Plugin registry |
| `~/.claude/settings.json` | `installer.py` + `statusline.py` | Plugin enable + status line |
| `~/.biff/statusline-original.json` | `statusline.py` | Stashed original status line |
| `~/.biff/unread/*.json` | MCP server (runtime) | Per-project unread counts |

---

## INS-001: Two-Phase Installation — pip + biff install

**Date:** 2026-02-15
**Status:** SETTLED
**Topic:** Why installation is split into `pip install punt-biff` and `biff install`

### Design

Installation is two commands:

```bash
pip install punt-biff    # Phase 1: Python package + CLI
biff install            # Phase 2: Claude Code integration
```

Phase 1 (`pip install`) provides the `biff` CLI and the MCP server code. Phase 2 (`biff install`) registers everything with Claude Code: MCP server, plugin files, plugin registry, and plugin settings.

### Why Two Phases

`pip install` cannot run `claude mcp add` or write to `~/.claude/` because:

- `claude` may not be on PATH during pip install (CI, virtualenvs, Docker).
- Writing to user config files from a pip post-install hook is fragile and non-standard.
- The user may install the package before installing Claude Code.

Keeping Phase 2 as a separate explicit command (`biff install`) means it runs when the user is ready and Claude Code is present.

### Idempotency

`biff install` is idempotent. Every step checks for existing state:

- `claude mcp add` reports "already exists" → step passes.
- Plugin files are removed and re-copied (handles upgrades).
- Registry entry is overwritten with current metadata.
- Settings entry is set to `true` (no-op if already enabled).

### Bootstrap Script

`install.sh` chains all steps for zero-thought setup:

```bash
pip install punt-biff
biff install
biff doctor
```

---

## INS-002: Plugin Source — importlib.resources, Not Symlinks

**Date:** 2026-02-15
**Status:** SETTLED
**Topic:** How plugin files get from the package to `~/.claude/plugins/`

### Design

The installer reads plugin files from `importlib.resources.files("biff.plugins")` and copies them via `shutil.copytree` to `~/.claude/plugins/biff/`. The source is the `src/biff/plugins/biff/` directory bundled in the wheel.

### Why Copy, Not Symlink

- Symlinks break when the virtualenv moves, the package upgrades, or `pip uninstall` runs.
- `importlib.resources` provides a stable, version-correct path to package data regardless of installation method (editable, wheel, sdist).
- Copying creates a self-contained plugin directory that survives package changes.

### Why Not the Local Marketplace Pattern

Claude Code has a "local marketplace" pattern with symlinks + `marketplace.json`. This was rejected because:

- It requires maintaining a marketplace manifest separate from the package.
- The symlink approach ties the plugin to a specific filesystem location.
- The copy approach is simpler: one step, no manifest, no symlink management.

The `biff@local` key in the registry mimics the local plugin convention without the marketplace indirection.

---

## INS-003: MCP Server Registration — Dual Path

**Date:** 2026-02-15
**Status:** SETTLED
**Topic:** How the MCP server gets registered in Claude Code

### Design

The MCP server is registered through two mechanisms that converge on the same target:

1. **`installer.py`** calls `claude mcp add --scope user biff -- biff serve --transport stdio`. This writes to `~/.claude.json` (the global MCP config).

2. **`statusline.py`** calls `_ensure_mcp_server()` which directly writes the MCP server entry to `~/.claude.json`. This runs during `biff install-statusline` and on every status line install as an idempotent reconciliation.

Both paths produce identical entries:

```json
{
  "mcpServers": {
    "biff": {
      "type": "stdio",
      "command": "/path/to/biff",
      "args": ["serve", "--transport", "stdio"]
    }
  }
}
```

### Why Dual Path

The `claude mcp add` CLI is the "proper" way to register but has failure modes (claude not on PATH, version differences). The direct-write path in `statusline.py` serves as a reconciliation mechanism — if the entry is missing or stale, installing the status line fixes it.

### Command Resolution

Both paths resolve the `biff` command identically via `shutil.which("biff")`:

- **Found:** Use the absolute path to `biff` (e.g., `/Users/kai/.local/bin/biff`).
- **Not found:** Fall back to `sys.executable -m biff` (Python module invocation).

This handles editable installs, `uv tool install`, and standard pip installs.

---

## INS-004: Status Line — Stash and Wrap

**Date:** 2026-02-15
**Status:** SETTLED
**Topic:** How biff integrates with Claude Code's status bar without destroying existing config

### Design

The status line installer uses a stash-and-wrap pattern:

1. **Stash** — Read the current `statusLine` value from `~/.claude/settings.json`, save it to `~/.biff/statusline-original.json`.
2. **Replace** — Set `statusLine` to `{"type": "command", "command": "biff statusline"}`.
3. **At runtime** — `biff statusline` reads the stash, runs the original command, appends biff's unread segment: `original-output | biff(2)`.

Uninstall reverses: read the stash, restore the original value, delete the stash file.

### Why Stash, Not Append

Claude Code's `statusLine` is a single command string, not a composable pipeline. There is no "add to status line" API. Biff must own the entire `statusLine` value and delegate to the original internally.

The stash file (`~/.biff/statusline-original.json`) is the proof that biff is installed. Its presence/absence is the install state, not a flag in settings.

### Separation from `biff install`

Status line installation is a separate command (`biff install-statusline`) because:

- It modifies global UI (the status bar visible in every session).
- Some users may not want it.
- The README calls it out as optional: "This is optional and separate from `biff install`."

`biff uninstall` does call `uninstall_statusline()` to clean up both in one shot.

---

## INS-005: Doctor — Post-Install Verification

**Date:** 2026-02-15
**Status:** SETTLED
**Topic:** How `biff doctor` validates the installation

### Design

`biff doctor` runs six diagnostic checks:

| Check | Required | What It Tests |
|-------|----------|---------------|
| `gh` CLI | Yes | GitHub CLI installed and authenticated |
| MCP server | Yes | `biff` registered in `claude mcp list` |
| Plugin commands | Yes | `~/.claude/plugins/biff/commands/` exists with `.md` files |
| NATS relay | Yes | Can connect to the configured relay URL |
| `.biff` file | No | `.biff` exists in the current git repo |
| Status line | No | Status line stash file exists |

Required checks must all pass (exit code 0). Informational checks (`.biff`, status line) report status but don't fail the command.

### Why Doctor, Not Install Verification

`biff install` reports step-by-step results, but it only covers what the installer does. `biff doctor` also checks external dependencies (`gh`, NATS connectivity) that the installer cannot control. It is the single command to answer "is everything working?"

### NATS Connectivity Check

Doctor resolves relay config the same way the server does: `.biff` file → demo relay fallback → bundled demo credentials. The connection test uses a 3-second timeout and `asyncio.run()` (blocking, since doctor is a CLI command).

---

## INS-006: Identity Resolution — GitHub First

**Date:** 2026-02-15
**Status:** SETTLED
**Topic:** How `biff init` and `biff serve` determine the user's identity

### Design

Identity resolution chain:

1. `--user` CLI override (explicit).
2. `gh api user --jq .login` (GitHub CLI, uses stored OAuth token).
3. `getpass.getuser()` (OS username fallback).

`biff init` resolves identity to display it during setup. `biff serve` resolves identity to register the session. Both use the same `get_github_identity()` function.

### Why Not Stored in `.biff`

Identity is per-user, not per-repo. Storing it in `.biff` (which is committed) would force all team members to share identity config. GitHub login is already available via `gh auth` — no extra config step needed.

---

## INS-007: `.biff` File — Per-Repo Config

**Date:** 2026-02-14
**Status:** SETTLED
**Topic:** What `biff init` creates and why

### Design

`biff init` creates a `.biff` TOML file at the git repo root:

```toml
[team]
members = ["kai", "eric"]

[relay]
url = "tls://connect.ngs.global"
```

The file is committed to the repo. All team members share it.

### What It Contains

| Section | Field | Purpose |
|---------|-------|---------|
| `[team]` | `members` | List of team usernames |
| `[relay]` | `url` | NATS relay URL |
| `[relay]` | `token` / `nkeys_seed` / `user_credentials` | Auth (pick one) |

### What It Does NOT Contain

- **User identity** — resolved from GitHub (INS-006).
- **Plugin config** — managed by `biff install`, not the `.biff` file.
- **Status line config** — managed by `biff install-statusline`.

### Demo Relay Default

`biff init` defaults the relay URL to `tls://connect.ngs.global` (Synadia NGS). Demo NATS credentials are bundled in the package at `biff/data/demo.creds` and auto-loaded when the relay URL matches the demo URL and no explicit auth is configured.

---

## INS-008: Uninstall — Reverse Every Step

**Date:** 2026-02-15
**Status:** SETTLED
**Topic:** How `biff uninstall` cleanly removes everything

### Design

`biff uninstall` reverses the install in order:

1. **Disable plugin** — Remove `biff@local` from `settings.json` `enabledPlugins`.
2. **Unregister plugin** — Remove `biff@local` from `installed_plugins.json`.
3. **Remove plugin files** — `shutil.rmtree(~/.claude/plugins/biff/)`.
4. **Remove user commands** — Delete biff command files from `~/.claude/commands/`.
5. **Remove MCP server** — `claude mcp remove biff`.
6. **Remove status line** — Restore stashed original, delete stash file, remove MCP entry from `~/.claude.json`.

### What It Does NOT Remove

- The `.biff` file (repo config, committed to git).
- The `biff` CLI itself (managed by pip).
- `~/.biff/unread/` directory (runtime state, harmless).

---

## Installation Flow — End to End

```
User                     pip              biff CLI           Claude Code Files
 │                        │                  │                      │
 │  pip install punt-biff  │                  │                      │
 ├───────────────────────►│                  │                      │
 │                        │  install pkg     │                      │
 │                        │  + CLI entry     │                      │
 │                        │◄─────────────────┤                      │
 │                        │                  │                      │
 │  biff install          │                  │                      │
 ├──────────────────────────────────────────►│                      │
 │                        │                  │  claude mcp add      │
 │                        │                  ├─────────────────────►│
 │                        │                  │  ~/.claude.json      │
 │                        │                  │                      │
 │                        │                  │  copytree plugins    │
 │                        │                  ├─────────────────────►│
 │                        │                  │  ~/.claude/plugins/  │
 │                        │                  │                      │
 │                        │                  │  write registry      │
 │                        │                  ├─────────────────────►│
 │                        │                  │  installed_plugins   │
 │                        │                  │                      │
 │                        │                  │  enable plugin       │
 │                        │                  ├─────────────────────►│
 │                        │                  │  settings.json       │
 │                        │                  │                      │
 │  ✓ MCP server          │                  │                      │
 │  ✓ Plugin files        │                  │                      │
 │  ✓ User commands       │                  │                      │
 │  ✓ Plugin registry     │                  │                      │
 │  ✓ Plugin enabled      │                  │                      │
 │  "Restart Claude Code" │                  │                      │
 │◄──────────────────────────────────────────┤                      │
 │                        │                  │                      │
 │  biff doctor           │                  │                      │
 ├──────────────────────────────────────────►│                      │
 │                        │                  │  ✓ gh CLI            │
 │                        │                  │  ✓ MCP server        │
 │                        │                  │  ✓ Plugin commands   │
 │                        │                  │  ✓ NATS relay        │
 │                        │                  │  ○ .biff file        │
 │                        │                  │  ○ Status line       │
 │  "All checks passed"   │                  │                      │
 │◄──────────────────────────────────────────┤                      │
```

---

## Prerequisites

Before `biff install` can succeed, the user needs:

| Prerequisite | Why | How to Get It |
|--------------|-----|---------------|
| Python 3.13+ | Package requires modern Python | `brew install python` or system package manager |
| `pip` / `uv` | Install the PyPI package | Bundled with Python |
| Claude Code (`claude` CLI) | MCP server registration via `claude mcp add` | `npm install -g @anthropic-ai/claude-code` |
| GitHub CLI (`gh`) | Identity resolution | `brew install gh && gh auth login` |

`biff doctor` validates all of these post-install.

---

## INS-009: User Commands — Top-Level Aliases via ~/.claude/commands/

**Date:** 2026-02-16
**Status:** SETTLED
**Topic:** How top-level slash commands (`/who`, `/mesg`) get deployed alongside namespaced commands (`/biff:who`, `/biff:mesg`)

### Design

The installer copies the same `.md` command files from `plugin_source() / "commands"` to two locations:

1. **Plugin commands** (existing) — `~/.claude/plugins/biff/commands/` → namespaced as `/biff:who`, `/biff:mesg`, etc.
2. **User commands** (new) — `~/.claude/commands/` → top-level as `/who`, `/mesg`, etc.

Both are `shutil.copy2()` from the same bundled source. The installer owns both targets; hand-editing either is overwritten on next install.

### Why Copy to Both

Claude Code resolves commands from two paths: per-plugin (`plugins/<name>/commands/`) and global user (`~/.claude/commands/`). Plugin commands are namespaced with the plugin name; user commands are top-level. Users expect `/who`, not `/biff:who`. Both must exist because some users may disable the plugin but still want the MCP server + top-level commands.

### Uninstall

`_uninstall_user_commands()` only removes files whose names match the bundled command filenames. It does not touch non-biff files that may exist in `~/.claude/commands/`. This is safe because command filenames are distinctive (`who.md`, `finger.md`, `mesg.md`).

### Doctor

`_check_user_commands()` is informational (`required=False`). Missing user commands are not a hard failure — the namespaced plugin commands still work.
