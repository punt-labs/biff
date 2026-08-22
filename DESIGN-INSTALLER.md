# Biff Installer Design

This document describes the architecture of biff's installation system — how the PyPI package, CLI commands, Claude Code plugin system, MCP server registration, and status line integration fit together.

## Rules

1. Before proposing ANY change to the installer, consult this document for prior decisions.
2. Do not revisit a settled decision without new evidence.
3. Log the decision, alternatives considered, and outcome.

---

## Installation Architecture

**The diagram below is the original two-phase design and three of its boxes
are now historical.** It is kept because the decisions further down this log
refer to it, but do not read it as current state:

- **There is no bundled plugin inside the wheel.** `src/biff/plugins/biff/`
  and `installer.py` are both gone; the plugin ships through the marketplace,
  so `claude plugin update` is the upgrade path for prompts and hooks. Since
  DES-055 the surface lives in `plugin/` at the repo root
  (`plugin/.claude-plugin/`, `plugin/commands/`, `plugin/hooks/`) and the
  marketplace fetches only that directory via the `git-subdir` source.
- **`biff install` is not a five-step copier.** `install_cmd`
  (`src/biff/__main__.py`) deposits the user-scope agent guide, deploys this
  clone's `.git/hooks` dispatchers, and shells out to
  `claude plugin install biff@punt-labs --scope user`. A missing `claude` is a
  successful CLI-only install, not a partial failure.
- **Top-level `~/.claude/commands/*.md` deployment moved into the plugin.**
  `plugin/hooks/session-start.sh` copies them from `${CLAUDE_PLUGIN_ROOT}/commands/`
  on first run, skipping the `*-dev.md` files and skipping the step entirely
  when the manifest name ends in `-dev` (the prod plugin owns those files).

The statusline stash-and-wrap box is still accurate: `install-statusline`
stashes the original `statusLine` at `~/.punt-labs/biff/statusline-original.json`,
and `plugin/hooks/session-start.sh` treats the absence of that stash as "not
yet installed".

**There is no `.biff` file and no `biff init` command.** Both were removed
in v1.13.1 (see CHANGELOG.md). Per-repo config lives at
`.punt-labs/biff/config.yaml` (shared, committed) and
`.punt-labs/biff/config.local.yaml` (per-user, gitignored) — see DES-037
(config migration) and DES-052 (enablement marker) in `DESIGN.md`. INS-002,
INS-003, INS-005 through INS-008 below describe the old `installer.py` /
`.biff init` architecture and are superseded; each carries a short note
pointing at current behavior. INS-004, INS-009, and INS-010 are unaffected
and still describe current behavior.

**MCP registration has no dual path.** The server is registered entirely
through the plugin's own `mcpServers` entry in
`plugin/.claude-plugin/plugin.json`, which Claude Code reads when the
plugin loads. There is no `claude mcp add` step and no `_ensure_mcp_server()`
reconciliation in `statusline.py` — `install()` there only stashes and
replaces `statusLine`.

```text
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
│  │     ~/.punt-labs/biff/statusline-original.json                               │  │
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
│  │    → reads ~/.punt-labs/biff/unread/*.json for per-project counts           │  │
│  │    → runs stashed original command (if any)                       │  │
│  │    → outputs: "original-output | biff(2) myapp(1)"               │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
```

## File Locations (User's Machine)

| Path | Written By | Purpose |
|------|-----------|---------|
| `~/.claude/plugins/installed_plugins.json` | `claude plugin install` | Plugin registry (marketplace-managed) |
| `~/.claude/plugins/cache/punt-labs/biff/<version>/` | `claude plugin install` | Cached plugin files (commands, hooks, `plugin.json` with `mcpServers`) |
| `~/.claude/commands/` | `install_cmd` (`_deploy_user_commands`) | Top-level user commands (`.md`) |
| `~/.claude/CLAUDE.md` | `install_cmd` (`_register_user_scope`) | Single `@`-import line pointing at the deposited guide |
| `~/.punt-labs/biff/CLAUDE.md` | `install_cmd` (`_register_user_scope`) | Deposited agent-facing guide (INS-010) |
| `~/.claude/settings.json` | `statusline.py` | Status line entry only (plugin enable/registry lives in the two paths above) |
| `~/.punt-labs/biff/statusline-original.json` | `statusline.py` | Stashed original status line |
| `~/.punt-labs/biff/unread/*.json` | MCP server (runtime) | Per-project unread counts |
| `.punt-labs/biff/config.yaml` / `config.local.yaml` (per repo) | user, via `biff enable` / hand-edit | Team roster, relay URL/auth (DES-037, DES-052) |

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

Phase 1 (`pip install` / `uv tool install`) provides the `biff` CLI and the
MCP server code. Phase 2 (`biff install`) deposits the user-scope agent guide
(`~/.punt-labs/biff/CLAUDE.md` + the `@`-import), deploys this clone's git
hooks, and installs the Claude Code plugin via `claude plugin install
biff@punt-labs --scope user` (see the current `install_cmd` in
`src/biff/__main__.py`). MCP server registration is not a separate step —
it's part of what the plugin itself declares in `plugin.json`'s
`mcpServers`, loaded automatically once the plugin is installed.

### Why Two Phases

`pip install` cannot install the Claude Code plugin because:

- `claude` may not be on PATH during pip install (CI, virtualenvs, Docker).
- Writing to user config files from a pip post-install hook is fragile and non-standard.
- The user may install the package before installing Claude Code.

Keeping Phase 2 as a separate explicit command (`biff install`) means it runs when the user is ready and Claude Code is present. A missing `claude` binary makes Phase 2 a successful CLI-only install (the guide and git hooks still land), not a partial failure.

### Idempotency

`biff install` is idempotent: the user-scope guide is overwritten wholesale on
every run, the `@`-import is only added if not already present, git hooks are
redeployed, and `claude plugin install` itself no-ops (with a warning) if the
plugin is already at that version.

### Bootstrap Script

`install.sh` chains all steps for zero-thought setup: install the `biff`
CLI via `uv tool install`, register the `punt-labs` marketplace, install
the plugin, then run `biff doctor` to verify. See `install.sh` at the repo
root for the exact steps, including the CLI-only (`--no-plugin`) path.

---

## INS-002: Plugin Source — importlib.resources, Not Symlinks

**Date:** 2026-02-15
**Status:** SUPERSEDED by DES-055 (`DESIGN.md`)
**Topic:** How plugin files get from the package to `~/.claude/plugins/`

### What Actually Happens Now

There is no bundled plugin in the wheel and no `importlib.resources` copy
step. Since DES-055, the plugin surface lives at `plugin/` in this repo's
root (`plugin/.claude-plugin/`, `plugin/commands/`, `plugin/hooks/`) and is
installed straight from GitHub via `claude plugin install biff@punt-labs`,
which the marketplace resolves through a `git-subdir` source
(`punt-labs/claude-plugins`). Upgrades are `claude plugin update`, not a
new PyPI release plus a re-copy. See DES-055 for the full rationale
(single source of truth for prompts/hooks, no drift between the wheel's
bundled copy and what ships).

### Original Design (historical)

The installer used to read plugin files from
`importlib.resources.files("biff.plugins")` and copy them via
`shutil.copytree` to `~/.claude/plugins/biff/`, sourced from
`src/biff/plugins/biff/` in the wheel. That directory, `installer.py`, and
the `biff@local` registry key this section originally described are all
gone.

---

## INS-003: MCP Server Registration — Via Plugin Manifest, Not `claude mcp add`

**Date:** 2026-02-15
**Status:** SUPERSEDED by DES-055 (`DESIGN.md`)
**Topic:** How the MCP server gets registered in Claude Code

### What Actually Happens Now

There is no `claude mcp add` step and no dual-path reconciliation. The MCP
server is declared once, in `plugin/.claude-plugin/plugin.json`'s
`mcpServers` entry, which Claude Code reads and spawns automatically when
the plugin loads — the same mechanism as any other plugin-provided MCP
server. `statusline.py`'s `install()` only stashes and replaces
`statusLine`; it no longer touches `~/.claude.json` or reconciles an MCP
entry (its own docstring says as much: "The MCP server is registered via
the plugin's `mcpServers` in `plugin.json`, not here").

### Original Design (historical)

The MCP server used to be registered through two converging mechanisms:
`installer.py` running `claude mcp add --scope user biff -- biff serve
--transport stdio`, and `statusline.py` calling a since-removed
`_ensure_mcp_server()` as an idempotent reconciliation on every status
line install. Both wrote to `~/.claude.json` directly. Neither exists
today.

---

## INS-004: Status Line — Stash and Wrap

**Date:** 2026-02-15
**Status:** SETTLED
**Topic:** How biff integrates with Claude Code's status bar without destroying existing config

### Design

The status line installer uses a stash-and-wrap pattern:

1. **Stash** — Read the current `statusLine` value from `~/.claude/settings.json`, save it to `~/.punt-labs/biff/statusline-original.json`.
2. **Replace** — Set `statusLine` to `{"type": "command", "command": "biff statusline"}`.
3. **At runtime** — `biff statusline` reads the stash, runs the original command, appends biff's unread segment: `original-output | biff(2)`.

Uninstall reverses: read the stash, restore the original value, delete the stash file.

### Why Stash, Not Append

Claude Code's `statusLine` is a single command string, not a composable pipeline. There is no "add to status line" API. Biff must own the entire `statusLine` value and delegate to the original internally.

The stash file (`~/.punt-labs/biff/statusline-original.json`) is the proof that biff is installed. Its presence/absence is the install state, not a flag in settings.

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

`biff doctor` runs ten diagnostic checks (`check_environment()` in
`src/biff/doctor.py`):

| Check | Required | What It Tests |
|-------|----------|---------------|
| `gh` CLI | No | GitHub CLI installed and authenticated |
| Plugin | Yes | `biff@punt-labs` present in `~/.claude/plugins/installed_plugins.json` |
| User commands | No | Top-level command files exist in `~/.claude/commands/` |
| Agent guide | No | The `@`-import is registered in `~/.claude/CLAUDE.md` |
| NATS relay | Yes | Can connect to the configured relay URL |
| Config | No | `.punt-labs/biff/config.yaml` exists, or zero-config (defaults from git remote) |
| Enabled | No | `is_enabled()` — reads `config.local.yaml`, or the committed marker (see DES-052) |
| Git hooks | No | This clone's `.git/hooks` dispatchers are deployed |
| CI workflow | No | The biff-notify GitHub Actions workflow is present |
| Status line | No | Status line stash file exists |

Only two checks are required (`required=True`, must pass for exit code 0):
**Plugin** and **NATS relay**. Every other check is informational — it
reports status but never fails the command.

### Why Doctor, Not Install Verification

`biff install` reports step-by-step results, but it only covers what the installer does. `biff doctor` also checks external dependencies (`gh`, NATS connectivity) that the installer cannot control. It is the single command to answer "is everything working?"

### NATS Connectivity Check

Doctor resolves relay config the same way the server does: read
`.punt-labs/biff/config.yaml` + `config.local.yaml` (DES-037), then fall back
to the demo relay with bundled demo credentials if no `relay.url` is set. The
connection test uses a 3-second timeout and `asyncio.run()` (blocking, since
doctor is a CLI command).

---

## INS-006: Identity Resolution — GitHub First

**Date:** 2026-02-15
**Status:** SETTLED, with a correction — there is no `biff init` command
**Topic:** How biff determines the user's identity

### Design

Identity resolution chain (`get_github_identity()` and its callers in
`src/biff/config.py`):

1. `--user` CLI override (explicit), where a command accepts one.
2. `gh api user --jq .login` (GitHub CLI, uses stored OAuth token).
3. `getpass.getuser()` (OS username fallback).

`biff serve` resolves identity to register the session. There is no `biff
init` command — it does not exist in `src/biff/__main__.py` and never
shipped under that name; per-repo config is written by hand or via `biff
enable` (see INS-007's replacement below), not a dedicated init wizard.

### Why Not Stored in Committed Config

Identity is per-user, not per-repo. Storing it in
`.punt-labs/biff/config.yaml` (which is committed) would force all team
members to share identity config. GitHub login is already available via
`gh auth` — no extra config step needed.

---

## INS-007: Per-Repo Config — `.punt-labs/biff/config.yaml`

**Date:** 2026-02-14 (original); corrected 2026-08-22
**Status:** SETTLED — original design (a `.biff` TOML file created by
`biff init`) never shipped this way; see DES-037 in `DESIGN.md` for the
actual migration history
**Topic:** Where per-repo config lives and what it contains

### Design

Per-repo config lives at `.punt-labs/biff/config.yaml` (shared, committed)
and `.punt-labs/biff/config.local.yaml` (per-user, gitignored):

```yaml
# .punt-labs/biff/config.yaml — committed
team:
  members: ["kai", "eric"]
relay:
  url: "tls://connect.ngs.global"
```

```yaml
# .punt-labs/biff/config.local.yaml — gitignored
relay:
  auth:
    credentials: "/path/to/private.creds"
```

Nothing in this repo reads a `.biff` file or a `.biff init` command — both
were removed in v1.13.1 (see CHANGELOG.md) and, per DES-037, replaced by
the schema above before that removal.

### What It Contains

| File | Field | Purpose |
|------|-------|---------|
| `config.yaml` | `team.members` | List of team usernames |
| `config.yaml` | `relay.url` | NATS relay URL |
| `config.local.yaml` | `relay.auth.{token,nkeys_seed,credentials}` | Auth (pick one, per-user, never committed) |

### What It Does NOT Contain

- **User identity** — resolved from GitHub (INS-006).
- **Plugin config** — managed by `biff install`, not this file.
- **Status line config** — managed by `biff install-statusline`.

### Demo Relay Default

If `relay.url` is unset (or the repo has no `config.yaml` at all), biff
defaults to the demo relay (`DEMO_RELAY_URL`, `tls://connect.ngs.global`
via Synadia NGS). Demo NATS credentials are bundled in the package at
`biff/data/demo.creds` and auto-loaded when the relay URL matches the demo
URL and no explicit auth is configured (`_apply_demo_relay_default` in
`src/biff/config.py`).

---

## INS-008: Uninstall — Reverse Every Step

**Date:** 2026-02-15 (corrected 2026-08-22)
**Status:** SETTLED
**Topic:** How `biff uninstall` cleanly removes everything

### Design

`uninstall_cmd` (`src/biff/__main__.py`) reverses the install:

1. **Uninstall plugin** — `claude plugin uninstall biff@punt-labs --scope user` (skipped with a message if `claude` isn't on PATH; a failure here doesn't skip the rest).
2. **Remove this clone's git hooks** — `_remove_repo_git_hooks()`. Only this checkout's `.git/hooks` dispatchers; the committed marker and CI workflow are repo policy, untouched here (`biff disable` owns those).
3. **User-scope teardown** — `UserScope().uninstall()` always runs, regardless of whether step 1 succeeded, so a failed or skipped plugin uninstall never strands the `@`-import: removes the `@~/.punt-labs/biff/CLAUDE.md` line from `~/.claude/CLAUDE.md`. The deposited guide file itself is left in place, dormant (INS-010's §2.9).

`biff uninstall-statusline` is separate (as install is): restores the
stashed original `statusLine`, deletes the stash file.

### What It Does NOT Remove

- `.punt-labs/biff/config.yaml` / `config.local.yaml` (repo config, committed and per-user respectively).
- The deposited `~/.punt-labs/biff/CLAUDE.md` guide itself (left dormant, not deleted).
- The `biff` CLI itself (managed by pip / `uv tool`).
- `~/.punt-labs/biff/unread/` directory (runtime state, harmless).

---

## Installation Flow — End to End

```text
User                     uv/pip           biff CLI           Claude Code / disk
 │                        │                  │                      │
 │  uv tool install       │                  │                      │
 │  punt-biff             │                  │                      │
 ├───────────────────────►│                  │                      │
 │                        │  install pkg     │                      │
 │                        │  + CLI entry     │                      │
 │                        │◄─────────────────┤                      │
 │                        │                  │                      │
 │  claude plugin         │                  │                      │
 │  marketplace add       │                  │                      │
 │  punt-labs/claude-plugins ────────────────────────────────────► │
 │                        │                  │                      │
 │  claude plugin install │                  │                      │
 │  biff@punt-labs        │                  │                      │
 ├────────────────────────────────────────────────────────────────►│
 │                        │                  │  git-subdir fetch    │
 │                        │                  │  of plugin/ from     │
 │                        │                  │  punt-labs/biff      │
 │                        │                  │  → plugin cache +    │
 │                        │                  │    mcpServers entry  │
 │                        │                  │◄─────────────────────┤
 │                        │                  │                      │
 │  biff install          │                  │                      │
 ├──────────────────────────────────────────►│                      │
 │                        │                  │  deposit guide        │
 │                        │                  ├─────────────────────►│
 │                        │                  │  ~/.punt-labs/biff/  │
 │                        │                  │  CLAUDE.md            │
 │                        │                  │                      │
 │                        │                  │  add @-import         │
 │                        │                  ├─────────────────────►│
 │                        │                  │  ~/.claude/CLAUDE.md │
 │                        │                  │                      │
 │                        │                  │  deploy git hooks     │
 │                        │                  ├─────────────────────►│
 │                        │                  │  <clone>/.git/hooks  │
 │  "Restart Claude Code" │                  │                      │
 │◄──────────────────────────────────────────┤                      │
 │                        │                  │                      │
 │  biff doctor           │                  │                      │
 ├──────────────────────────────────────────►│                      │
 │                        │                  │  ○ gh CLI            │
 │                        │                  │  ✓ Plugin            │
 │                        │                  │  ○ User commands     │
 │                        │                  │  ○ Agent guide       │
 │                        │                  │  ✓ NATS relay        │
 │                        │                  │  ○ Config            │
 │                        │                  │  ○ Enabled           │
 │                        │                  │  ○ Git hooks         │
 │                        │                  │  ○ CI workflow       │
 │                        │                  │  ○ Status line       │
 │  "All checks passed"   │                  │                      │
 │◄──────────────────────────────────────────┤                      │
```

(✓ = required, must pass; ○ = informational, reports status only — see INS-005)

---

## Prerequisites

Before `biff install` can succeed, the user needs:

| Prerequisite | Why | How to Get It |
|--------------|-----|---------------|
| Python 3.13+ | Package requires modern Python | `brew install python` or system package manager |
| `pip` / `uv` | Install the PyPI package | Bundled with Python |
| Claude Code (`claude` CLI) | Plugin install/marketplace registration | `npm install -g @anthropic-ai/claude-code` |
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

## INS-010: User-Scope Agent Guide — Bare `@`-Import, Not a Managed Section

**Date:** 2026-07-25
**Status:** SETTLED
**Topic:** How biff's agent-facing guidance reaches every session, per punt-kit `tool-enable-disable.md` §2.4–2.6 (biff-qmd)
**Related:** INS-009 (user commands), the `tool-enable-disable.md` + `integration.md` standards

### Design

Biff is a global tool: its guidance is universal, so `install` registers it once at user scope rather than per repo. `install` does two things beyond the marketplace plugin:

1. **Deposit the guide.** Write the bundled `src/biff/data/user-claude.md` (agent-facing: how to *drive* biff — slash commands, the passive/pull receive model, poll cadence, verbatim-output rule) to `~/.punt-labs/biff/CLAUDE.md`, overwriting wholesale (§2.2 vendored zone). This is the same `importlib.resources` deposit pattern as the CI workflow template.
2. **Register the import.** Add the single bare line `@~/.punt-labs/biff/CLAUDE.md` to `~/.claude/CLAUDE.md`. Claude Code resolves the `@`-import at read time, so the guide loads in every session with no per-repo edit.

`uninstall` prunes the import line and leaves the deposited guide dormant (§2.9 — removal is deliberate, never a toggle side effect). `doctor` reports whether the import is registered (informational).

### Why a Bare Line, Not a Managed Section

The user's `CLAUDE.md` is user-owned prose (§2.1). The only mutation biff makes is adding or removing one `@`-import line pointing at a file biff owns entirely — no marker block, no managed section, no merge algorithm. vox's `GlobalClaudeImports` still carries a marker-delimited managed section; §2.1/§2.11 retire that model, so biff ports only the **write correctness** (`ClaudeMdImport`, from vox's `AtomicFile`), not the markers.

### The §2.4 Write Contract

`ClaudeMdImport` (`src/biff/claude_md.py`) implements the load-bearing details so all 15 punt CLIs produce byte-identical results:

- **Exclusive lock.** `flock` on a sibling lock file for the whole read-modify-write — two parallel `install` runs cannot lose an update to a last-writer-wins race.
- **Atomic + byte-preserving.** Temp file in the target's directory, `fsync`, `os.replace`; read/write with `newline=""` so LF, CRLF, and lone-CR survive verbatim. The appended line uses the host file's own EOL.
- **Symlink-resolving, mode-preserving.** A dotfile-managed symlink is followed to its real target; an existing file's mode is kept, a new file gets `0644`.
- **Idempotent, terminator-insensitive.** Presence is decided net of the line's terminator, so a CRLF host never gets a duplicate. The scan is code-block-aware: a matching line inside a fenced (```` ``` ````/`~~~`) or indented block is inert markdown and is skipped for both append and removal.

### Not in Scope Here

The `.punt-labs/biff/enabled` presence marker (§2.7 + `integration.md` L0) has since **landed** — see DES-052 (runtime `DESIGN.md`). Biff's enablement is now committed, repo-level policy: the git-tracked marker is the single authoritative signal that `is_enabled()` and all ten hook gates read, replacing the old per-user, gitignored `config.local.yaml` `enabled:` key. The `enable` / `disable` verbs (CLI and `/biff`) write and remove it; the per-user preference layer lives separately in `mesg`.
