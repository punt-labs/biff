# Biff Installer Design

This document records biff's installation-system decisions (INS-001 to
INS-011) and describes the install flow as the code runs it today. Runtime
decisions (relay, sessions, display, identity, trust) live in
[DESIGN.md](../../DESIGN.md). Paths follow DES-091 there, relay selection
follows DES-090, and identity follows DES-076 and DES-094.

## Rules

1. Before proposing ANY change to the installer, consult this document for prior decisions.
2. Do not revisit a settled decision without new evidence.
3. Log the decision, alternatives considered, and outcome.

---

## Installation Architecture

Installation has four separate actions. Each has one owner in
`src/biff/__main__.py`.

| Action | Command | What it does | What it never does |
|--------|---------|--------------|--------------------|
| Install | `biff install` | Creates biff's home tree, then runs `claude plugin install biff@punt-labs --scope user` | Writes no repo file, no `CLAUDE.md`, no settings file, no git hook |
| Enable (per repo) | `biff enable` | Writes the committed marker and CI workflow, deploys this clone's git hooks, adds allow rules to the repo's `.claude/settings.json`, deposits the agent guide and its `@`-import (INS-011) | Never commits; never touches user-scope files |
| Status line (optional) | `biff install-statusline` | Stashes the current `statusLine` and replaces it with `biff statusline` (INS-004) | Is not run by `install` or `uninstall` |
| Uninstall | `biff uninstall` | Removes the plugin, this clone's git hooks, and a legacy user-scope `@`-import if one exists (INS-008) | Does not remove the status line, repo config, the marker, the CI workflow or the CLI |

`biff disable` is the inverse of `enable`: it removes the marker, the CI
workflow, this clone's git hooks, the allow rules, and the guide import.

**Install steps.** `install_cmd` runs `provision_home()` first. It creates
`~/.punt-labs/biff/` and its subdirectories `active/`, `current/`, `logs/`,
`sessions/` and `unread/` at mode `0700` (DES-091 item 3). A symlinked entry
or a failed creation exits 1. It then looks for `claude` on `PATH`:

- Missing: prints "Installed (CLI-only)" and exits 0. A CLI-only install is a success.
- Present: runs the marketplace install and exits 1 if it fails. On success it prints "Installed. Restart Claude Code to activate."

`install.sh` at the repo root is the bootstrap: it installs the CLI with
`uv tool install`, registers the `punt-labs` marketplace, installs the
plugin, and runs `biff doctor`. `--no-plugin` (or `BIFF_NO_PLUGIN=1`) skips the
plugin; the home tree is still created and doctor still runs. Doctor reports
the plugin as installed or missing, whichever it finds; `--no-plugin` never
removes an installed one.

**What the plugin carries.** The marketplace fetches only `plugin/` from
`punt-labs/biff` through a `git-subdir` source (DES-055). That directory
holds `plugin/.claude-plugin/plugin.json` (including the `mcpServers` entry
that runs `biff mcp`), `plugin/commands/` and `plugin/hooks/`. There is no
bundled plugin in the wheel, no `claude mcp add` step, and no write to
`~/.claude.json`. `claude plugin update` is the upgrade path for prompts and
hooks; PyPI carries the CLI and server.

**Top-level commands.** `plugin/hooks/session-start.sh` copies each
`plugin/commands/*.md` into `~/.claude/commands/` when the destination is
missing or differs. It skips `*-dev.md` files and skips the whole step when
`plugin.json` names a `-dev` plugin, because the production plugin owns those
files. The hook edits no settings file.

**Per-repo config.** `.punt-labs/biff/config.yaml` (committed) and
`.punt-labs/biff/config.local.yaml` (per user, gitignored) hold the team
roster and relay settings (DES-037). With neither, biff runs zero-config and
uses the local relay unless `BIFF_RELAY_URL` names one (DES-090, DES-060). The committed marker
`.punt-labs/biff/enabled` is the enablement signal (DES-052). There is no
`.biff` file and no `biff init` command.

**Identity.** Resolution order and the trust rules belong to DES-076 and
DES-094 (`docs/design/trust-model.md`). Installation stores no identity.

**Doctor.** `biff doctor` (`Diagnostics` in `src/biff/doctor.py`) prints the
package version, then one line per check, then exits 1 only if a required
check failed. A malformed shared `config.yaml` stops doctor with that error
before any check prints. Checks run in this order:

| Check | Required | What it tests |
|-------|----------|---------------|
| gh CLI | No | `gh` is installed and authenticated |
| Plugin | Yes | `biff@punt-labs` is in `~/.claude/plugins/installed_plugins.json` |
| User commands | No | The top-level command files exist in `~/.claude/commands/` |
| Relay | Yes | With no relay configured: the local relay directory is writable. With a NATS relay configured (the check is then named "NATS relay"): the server accepts a connection (3-second connect timeout, one retry) |
| Config | No | `.punt-labs/biff/config.yaml` exists, or zero-config applies |
| Enabled | No | The committed marker exists |
| Git hooks | No | This clone's biff hooks are deployed; skipped when biff is not enabled |
| CI workflow | No | `biff-notify.yml` is present and current; skipped when biff is not enabled |
| Status line | No | `settings.json` `statusLine` runs `biff statusline` |
| Channels | No | No environment variable in the current shell blocks channel events |
| Channel | No | An end-to-end check that each running session receives channel events; needs the NATS relay |

**Runtime.** `biff statusline` reads the stash file and the per-session
unread files in `~/.punt-labs/biff/unread/`, runs the stashed original
command if there is one, and appends biff's segment. The model-facing
display pipeline is documented in DESIGN.md.

## File Locations (User's Machine)

| Path | Written by | Purpose |
|------|-----------|---------|
| `~/.claude/plugins/installed_plugins.json` | `claude plugin install` | Plugin registry |
| `~/.claude/plugins/cache/punt-labs/biff/<version>/` | `claude plugin install` | Cached plugin files |
| `~/.claude/commands/*.md` | `plugin/hooks/session-start.sh` | Top-level commands (`/who`, `/write`, ...) |
| `~/.punt-labs/biff/` and `active/`, `current/`, `logs/`, `sessions/`, `unread/` | `biff install` (`provision_home`), later runtime | Biff's home tree, mode `0700` (DES-091) |
| `~/.punt-labs/biff/statusline-original.json` | `biff install-statusline` | Stashed original status line |
| `~/.claude/settings.json` | `biff install-statusline` | `statusLine` entry only |
| `<repo>/.punt-labs/biff/enabled`, `<repo>/.github/workflows/biff-notify.yml` | `biff enable` | Committed enablement artifacts (DES-052) |
| `<repo>/.punt-labs/biff/CLAUDE.md`, `<repo>/CLAUDE.md` | `biff enable` | Agent guide and its `@`-import (INS-011) |
| `<repo>/.claude/settings.json` | `biff enable` | Allow rules for biff's tools and commands |
| The clone's hooks directory, as `git rev-parse --git-path hooks` reports it (`.git/hooks/` by default) | `biff enable` | Per-clone git hook dispatchers |
| `<main checkout>/.punt-labs/local/biff/relay/` | biff at runtime | Local relay state (DES-090, DES-091) |
| `<repo>/.punt-labs/biff/config.yaml`, `config.local.yaml` | the user | Team roster, relay settings (DES-037) |

---

## INS-001: Two-Phase Installation — pip + biff install

**Date:** 2026-02-15
**Status:** SETTLED; Phase 2 now creates the home tree and installs the plugin only — no guide (INS-011), no git hooks, which `biff enable` deploys (2026-11-26)
**Topic:** Why installation is split into `pip install punt-biff` and `biff install`

### Design

Installation is two commands:

```bash
pip install punt-biff    # Phase 1: Python package + CLI
biff install            # Phase 2: Claude Code integration
```

Phase 1 (`pip install` / `uv tool install`) provides the `biff` CLI and the
MCP server code. Phase 2 (`biff install`) installs the Claude Code plugin via `claude plugin install
biff@punt-labs --scope user` (see the current `install_cmd` in
`src/biff/__main__.py`). MCP server registration is not a separate step —
it's part of what the plugin itself declares in `plugin.json`'s
`mcpServers`, loaded automatically once the plugin is installed. Phase 2 also
installs biffd's user service before the plugin (INS-012).

### Why Two Phases

`pip install` cannot install the Claude Code plugin because:

- `claude` may not be on PATH during pip install (CI, virtualenvs, Docker).
- Writing to user config files from a pip post-install hook is fragile and non-standard.
- The user may install the package before installing Claude Code.

Keeping Phase 2 as a separate explicit command (`biff install`) means it runs when the user is ready and Claude Code is present. A missing `claude` binary makes Phase 2 a successful CLI-only install (the CLI and server are installed; only the plugin step is skipped), not a partial failure.

### Idempotency

`biff install` is idempotent: `claude plugin install` itself no-ops (with a warning) if the
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

`biff uninstall` does not touch the status line; run `biff uninstall-statusline` separately (corrected 2026-11-26).

---

## INS-005: Doctor — Post-Install Verification

**Date:** 2026-02-15
**Status:** SETTLED; the check list below is superseded by the Doctor table in Installation Architecture (2026-11-26) — the "Agent guide" check is gone (INS-011) and the status line and channel checks were added
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
| Enabled | No | `is_enabled()` — the committed `.punt-labs/biff/enabled` marker is a regular file (not a symlink) AND `biff` is on PATH (DES-052) |
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
to the local relay if none is configured (DES-090). The
connection test uses a 3-second timeout and `asyncio.run()` (blocking, since
doctor is a CLI command).

---

## INS-006: Identity Resolution — GitHub First

**Date:** 2026-02-15
**Status:** SETTLED, with a correction — there is no `biff init` command; the identity chain below is superseded by DES-076 and DES-094 (2026-11-26)
**Topic:** How biff determines the user's identity
**Authorship:** see DES-094 (`docs/design/trust-model.md`)

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

### Demo Relay Is Opt-In

Superseded by DES-090: if no relay is configured (or the repo has no
`config.yaml` at all), biff uses the local filesystem relay. The demo relay
(`DEMO_RELAY_URL`, `tls://connect.ngs.global` via Synadia NGS) is chosen with
`relay: {type: demo}`. Demo NATS credentials are bundled in the package at
`biff/data/demo.creds` and auto-loaded when the relay URL matches the demo
URL and no explicit auth is configured (`_resolve_relay_fields` in
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
3. **Remove biffd's service** — `_uninstall_service()` stops and removes the unit or LaunchAgent that `biff install` registered (INS-012); a host with no service manager has nothing to remove, and a failure here fails the command after the steps below have run.
4. **User-scope teardown** — `UserScope().uninstall()` always runs, regardless of whether step 1 succeeded, so a failed or skipped plugin uninstall never strands the `@`-import: removes the `@~/.punt-labs/biff/CLAUDE.md` line from `~/.claude/CLAUDE.md`. The deposited guide file itself is left in place, dormant (INS-010's §2.9).

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
User                    uv / pip         biff CLI            Claude Code / disk
 │  uv tool install       │                 │                      │
 │  punt-biff             │                 │                      │
 ├───────────────────────►│  CLI + server   │                      │
 │                        │                 │                      │
 │  claude plugin marketplace add punt-labs/claude-plugins ──────► │
 │  biff install          │                 │                      │
 ├────────────────────────────────────────►│  provision_home():   │
 │                        │                 │  ~/.punt-labs/biff/  │
 │                        │                 │  claude plugin       │
 │                        │                 ├─────────────────────►│
 │                        │                 │  git-subdir fetch of │
 │                        │                 │  plugin/ → cache     │
 │  "Restart Claude Code" │                 │                      │
 │◄────────────────────────────────────────┤                      │
 │  (next session)        │                 │  session-start.sh    │
 │                        │                 │  copies commands to  │
 │                        │                 │  ~/.claude/commands/ │
 │  biff enable (per repo)│                 │                      │
 ├────────────────────────────────────────►│  marker, CI workflow,│
 │                        │                 │  git hooks, allow    │
 │                        │                 │  rules, guide+import │
 │  biff doctor           │                 │                      │
 ├────────────────────────────────────────►│  one line per check  │
 │  "All required checks  │                 │                      │
 │   passed."             │                 │                      │
 │◄────────────────────────────────────────┤                      │
```

The marketplace registration step is run by `install.sh`; `biff install`
itself does not register the marketplace.

---

## Prerequisites

| Prerequisite | Required for | How to get it |
|--------------|--------------|---------------|
| Python 3.13+ (`requires-python` in `pyproject.toml`) | The package | `brew install python` or the system package manager |
| `uv` or `pip` | Installing `punt-biff` | `install.sh` uses `uv tool install` |
| Claude Code (`claude` CLI) | The plugin step of `biff install`; without it the install is CLI-only | `npm install -g @anthropic-ai/claude-code` |
| GitHub CLI (`gh`) | One identity source (DES-076); `biff doctor` reports it but does not require it | `brew install gh && gh auth login` |

`biff doctor` checks `claude`'s plugin registry and `gh`; it fails only on
the Plugin and Relay checks.

---

## INS-009: User Commands — Top-Level Aliases via ~/.claude/commands/

**Date:** 2026-02-16
**Status:** SUPERSEDED by `plugin/hooks/session-start.sh` (2026-11-26) — the plugin's first-session hook copies the commands; `biff install` copies nothing
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
**Status:** SUPERSEDED by INS-011 (install no longer deposits a guide or writes an import; only the uninstall strip survives)
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

`ClaudeMdImport` (`src/biff/plugins/claude_code/claude_md.py`) implements the load-bearing details so all 15 punt CLIs produce byte-identical results:

- **Exclusive lock.** `flock` on a sibling lock file for the whole read-modify-write — two parallel `install` runs cannot lose an update to a last-writer-wins race.
- **Atomic + byte-preserving.** Temp file in the target's directory, `fsync`, `os.replace`; read/write with `newline=""` so LF, CRLF, and lone-CR survive verbatim. The appended line uses the host file's own EOL.
- **Symlink-resolving, mode-preserving.** A dotfile-managed symlink is followed to its real target; an existing file's mode is kept, a new file gets `0644`.
- **Idempotent, terminator-insensitive.** Presence is decided net of the line's terminator, so a CRLF host never gets a duplicate. The scan is code-block-aware: a matching line inside a fenced (```` ``` ````/`~~~`) or indented block is inert markdown and is skipped for both append and removal.

### Not in Scope Here

The `.punt-labs/biff/enabled` presence marker (§2.7 + `integration.md` L0) has since **landed** — see DES-052 (runtime `DESIGN.md`). Biff's enablement is now committed, repo-level policy: the git-tracked marker is the single authoritative signal that `is_enabled()` and all ten hook gates read, replacing the old per-user, gitignored `config.local.yaml` `enabled:` key. The `enable` / `disable` verbs (CLI and `/biff`) write and remove it; the per-user preference layer lives separately in `mesg`.

---

## INS-011: Install Deposits No Guide and Writes No Import

**Date:** 2026-11-12
**Status:** SETTLED
**Topic:** `biff install` is software-only, per punt-kit `tool-enable-disable.md` §2.6 (supersedes INS-010)

### Design

§2.6: `enable` is the sole path that deposits a guide and writes an import line; `install` never touches a `CLAUDE.md`; there is no user-scope import, and `~/.claude/CLAUDE.md` carries no tool guides. `install_cmd` therefore does one thing: `claude plugin install biff@punt-labs --scope user` (a missing `claude` is a successful CLI-only install).

- `UserScope.install()`, `UserScopeResult`, and `_register_user_scope()` are deleted. The bundled `user-claude.md` stays: it is biff's agent guide, now deposited by `enable`.
- `RepoEnablement.enable()` deposits `src/biff/data/user-claude.md` to `<repo>/.punt-labs/biff/CLAUDE.md` (wholesale overwrite, byte-compared so re-runs do not churn) and registers `@.punt-labs/biff/CLAUDE.md` in `<repo>/CLAUDE.md` through `ClaudeMdImport` (§2.3, §2.4, §2.5). `disable()` prunes that import and leaves the guide dormant (§2.9). The CLI verbs and the MCP `biff` tool both call `RepoEnablement`, so they stay equivalent (§2.14). This makes biff §2.5-compliant: before this change it shipped a guide only at user scope.
- `UserScope.uninstall()` stays: §2.6 has `uninstall` strip the legacy `@~/.punt-labs/biff/CLAUDE.md` line an older release wrote. A guide file already deposited stays dormant (§2.9).
- `doctor` drops the "Agent guide" check. It verified an import §2.6 says must not exist, and its remediation (`biff install`) no longer does anything about it.

### Rejected

Repointing the doctor check at the repo-scope guide: left out of this change; the existing `Enabled` check covers repo state.

## INS-012: biffd Is a User Service Installed by `biff install`

**Date:** 2026-11-26
**Status:** SETTLED; the supervision library is a stand-in until punt-service is published
**Topic:** How the biffd daemon (DES-095, `docs/design/biffd.md` "Supervision") is installed, started and checked

### Design

`biff install` provisions the home tree, installs biffd's service, then installs
the plugin. `biff uninstall` removes the service with the plugin and the clone's
hooks. `biff daemon install`, `uninstall`, `start`, `stop`, `restart` and
`status` act on the service alone. All of them go through `BiffdService`
(`src/biff/daemon/lifecycle.py`), which turns each result into one of three
outcomes: done, failed, or no service manager.

- **Linux:** a systemd user unit `biffd.service` in `~/.config/systemd/user`,
  `Restart=on-failure` because the spec sets `restart_on_crash_only`, so a crash
  restarts biffd and a clean `biff daemon stop` stays stopped. **macOS:** the
  LaunchAgent `com.punt-labs.biffd` with `KeepAlive` set to `SuccessfulExit: false`.
- **The executable** is `biffd`, a second console script that runs `biff daemon
  serve`. The unit's `ExecStart` is `~/.local/bin/biffd`, the path `uv tool
  install` creates; install fails naming that path when it is missing.
- **Port guard.** Install refuses when another program holds port 8424; a
  running biffd on it is fine.
- **No service manager** (a container, or a Linux login with no systemd user
  session): `biff install` says biffd is not supervised and succeeds;
  `biff daemon install`, `start`, `stop`, `restart` exit 3 with the same text,
  so a bootstrap can tell "no manager" from 1, a manager that refused.
- **A manager that exists and does not answer** (`systemctl` is installed and
  `systemctl --user show-environment` fails, as in a container that ships
  systemd without running it) is reported with systemctl's own words on stderr.
  `biff install` still exits 0, because that container is the case with no
  manager to supervise biffd; `biff daemon install`, `start`, `stop` and
  `restart` exit 3.
- **Install proves itself.** After registering the unit, install waits for
  `/health` and fails if biffd does not answer, so `Type=simple` "active" is
  never taken for "working". `daemon start` and `restart` wait the same way but
  leave the unit installed when biffd does not answer. A step that fails, or a
  daemon that never answers, removes the unit and disables it again;
  `uninstall` deletes the unit only after systemd reports the service stopped,
  and a supervisor failure is an error, never ignored.
- **The health wait is 30 seconds and `BIFF_DAEMON_HEALTH_WAIT` sets it.** The
  first value, 5 seconds, failed on a macOS laptop at a load average of 150 to
  229: launchd had spawned biffd, never saw it exit, and the install's own
  rollback removed it 5.25 seconds later. Spawn to first `/health` answer was
  measured on a 24-core Linux machine with `biffd --port N`: 0.75 s warm, 1.77 s
  with the bytecode cache deleted; with 24 busy loops (load average 79 on that
  shared machine) 0.99 to 2.34 s warm and 1.38 s cold. biffd imports its relay
  stack before it listens, so the time grows with CPU contention and with
  Python 3.14's cold imports; 30 seconds is more than ten times the worst
  measured value. The variable takes seconds above 0 and at most 600; anything
  else fails the command with the variable named, before the service is touched.
  The nudge keeps 5 seconds: it blocks a client's own call, the longest measured
  start under load (2.34 s) fits, and its error names the commands that find out
  why. A client that must wait longer starts biffd with `biff daemon start`.
- **A timeout says what the supervisor knew.** The error reports how long the
  wait was (not how long biffd ran: it may have exited early and been
  restarted), whether the supervisor shows biffd running now (slow to start, or
  restarted after an exit) or not (it exited or never started, so read
  `biff.log`), and the supervisor's own report: the state, run count and last
  exit code from `launchctl print gui/<uid>/<label>` on macOS, which prints the
  exit code after the program, arguments and environment; the first eight lines
  of `systemctl --user status` on Linux. All of it is read before the rollback,
  which still removes the unit.
- **start and restart refuse a port the service does not hold.** A foreground
  biffd answers `/health` too, so `start` and `restart` apply the install
  guard first: a biffd the service does not supervise, or another program, on
  the port is refused with the command to run after stopping it.
- **A long wait says so.** After 3 seconds without an answer, `install`,
  `start` and `restart` print one line to stderr, so a person does not think a
  command that waits up to 30 seconds has hung.
- **biffd logs its own start.** Its first line in `biff.log` after logging is
  configured gives version, pid, port, run directory and interpreter; a second
  says when it listens. The relay stack is imported after the first line, not
  before it (`biff.daemon` imports nothing at package level), so a daemon that
  is still loading has said it started. Under a service manager they are the
  only record that biffd ran at all.
- **A failed service install** does not skip the plugin step, because nothing
  routes through biffd yet, but it fails the command with exit 1.

The client autostart nudge (`biff.client.nudge.DaemonNudge`) is the one call a
client makes before it connects: `/health` for one second, start the unit or a
detached `biffd`, poll for five seconds, then raise `DaemonNotInstalledError` or
`DaemonDidNotStartError` with the two messages in `biffd.md`. With a service
manager, "installed" means the unit file exists, whatever else is on `PATH`;
without one it means a `biffd` executable on `PATH` or in `~/.local/bin`.

`biff doctor` gains seven biffd lines (unit, running, port, health, token,
version, relay), each naming its repair command. While the unit is not
installed they are informational (`required=False`) and a line says so, so a
machine that has not run `biff install` since upgrading still passes. Once the
unit is installed, a version mismatch and a port held by another program fail
`biff doctor`. The relay line only says relay connections are not reported yet:
the one way to ask biffd opens a session, and a session opened by doctor takes
part in mail routing. A supervisor that hangs or a file that cannot be read is a
failed line, never a traceback.

### Why a stand-in library

`punt-service` (`lux/packages/punt-service`) is not on PyPI, and a path or git
dependency would break `uv tool install punt-biff`. `biff.supervision` holds a
`ServiceSpec` and `ServiceBackend` protocol with the same fields and methods,
and its own systemd and launchd backends. When punt-service is published,
`systemd`, `launchd`, `host` and `runner` are replaced by its backends; the
spec, protocol, manager, nudge, CLI and doctor stay.

### Rejected

- *A system-level unit.* It needs `sudo` (DES-095).
- *Making the doctor lines required now.* They would fail every existing
  install until its owner re-ran `biff install`.
- *Letting a failed service install abort `biff install`.* It strands the
  plugin step for a daemon nothing uses yet.
