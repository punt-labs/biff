# Biff Package Structure

Status: proposed standard, awaiting operator ratification.

This document says where code goes in `src/biff/`, which packages may import
which, and how biff moves from today's layout to this one. It follows the
platform package standard Lux is migrating to
(`lux/docs/architecture/target/target.md`) and the org rules in
`punt-kit/standards/python.md` and `.claude/rules/python-package-architecture.md`.

## The rule

Biff is split into packages arranged in layers. **A package may import only
from the packages below it.** Nothing imports upward, and no package reaches
into another package's private modules.

Two ideas decide where a module belongs:

- **Biff versus its plugins.** Biff itself (the relay, the `biff` command
  line, the `biff mcp` server) is the same for every AI coding tool. A plugin
  adapts biff to one tool: Claude Code, Devin, opencode, codex or pi. Code that
  exists only because of one tool lives in that tool's package, never in the
  shared engine.
- **One code path.** Every biff operation (`who`, `write`, `wall`, `talk`, and
  the rest) is one command object in `commands/`. The command line and the MCP
  server are thin adapters that call it. Neither carries its own logic.

## The packages

From the bottom up:

| Package | Holds | May import |
|---------|-------|------------|
| `kernel` | Small helpers with no biff dependencies: session ids, locks, chunking, logging setup, usage counters, NATS connection helpers | nothing in biff |
| `wire` | The data types biff passes around: messages, sessions, talk records, session routing, unread summaries | `kernel` |
| `engine` | The relays (local and NATS), talk state, session registry, identity, configuration, tty names; the arrival summaries | `kernel`, `wire` |
| `render` | Text formatting that both the command line and the MCP server print: tables, `finger` output, message lists | `kernel`, `wire`, `engine` |
| `commands` | One command object per biff operation, plus the context each command receives | `kernel`, `wire`, `engine`, `render` |
| `server` | The MCP server and its tools, the same for every AI tool that connects | the above, plus `commands` |
| `service` | Install, enable, `biff doctor`, git hooks, the CI notification workflow, optional integrations such as vox | `kernel`, `wire`, `engine`, `render` |
| `claude_code` | The Claude Code plugin's Python code: hook handlers, the status line, the per-session unread file, the CLAUDE.md and permission installers, the channel sender, the start-up channel warning | the above, plus `commands`, `service` |
| `cli` | The `biff` command, the interactive shell, command-line sessions | everything above |

`cli` sits on top because it starts every other piece, including `biff mcp`.

### Why `claude_code` is its own package

Lux has no package for one AI tool; it treats its Claude Code plugin as a way
to install the MCP server. Biff needs one because about a tenth of its code
(2,647 of 24,827 lines) sits in modules that exist only for Claude Code: the
hooks, the status line, the unread file the status line reads, and the
CLAUDE.md and permission files biff installs. More is mixed into the MCP
server's tool-description module, and the Claude Code channel work (DES-087)
adds more. Keeping all of it in one package
keeps the engine and the MCP server the same for Devin, opencode, codex and pi.

Other plugins mostly live outside this Python package: the opencode plugin is
TypeScript and the pi plugin calls the `biff` command line. A plugin gets a
package here only when it needs Python code inside biff.

### What stays out of the engine

The engine never knows which AI tool is on the other end. In particular it
never writes the status-line file, never builds Claude Code tool descriptions,
and never edits CLAUDE.md or permission settings. When the engine has news (a
message arrived, a wall was posted), it produces an arrival summary; each
plugin decides how to show it.

## Where new code goes

Ask these in order and stop at the first yes:

1. Does it exist only because of one AI tool? It goes in that tool's package
   (`claude_code` today).
2. Does it install, enable, diagnose or uninstall biff? `service`.
3. Is it a biff operation a user or agent can invoke? A command object in
   `commands`, then a thin adapter in `server` and in `cli`.
4. Does it turn biff data into text for a person to read? `render`.
5. Does it hold or move messages, sessions or presence? `engine`.
6. Is it a data type passed between packages? `wire`.
7. Does it depend on nothing else in biff? `kernel`.

## How it is enforced

A layer checker reads every module's imports and reports each import that
breaks the table above. It runs in `make check` against a baseline file that
lists today's known violations:

- A new violation fails `make check`.
- Fixing a violation and not removing it from the baseline also fails, so the
  baseline only shrinks.
- `make update-layers` rewrites the baseline after a fix.

This is the same ratchet Lux uses (`lux/tools/layer_contract.py`,
`.layer-baseline.json`). The checker belongs in the shared `punt-oo-ratchet`
package so both projects run the same tool.

## Where biff stands today

Measured on `main` at `968e4c5`, 2026-11-22: 115 modules, 25 imports that
break the table. Most modules still sit flat in `src/biff/`; the package names
above describe where each one is headed, not today's directories.

| From | To | Count | Example | Why it matters |
|------|----|-------|---------|----------------|
| `commands` | `server` | 4 | `commands/finger.py` imports `server/tools/_session.py` | Commands are shared by every adapter; one must not depend on the MCP server |
| `server` | `service` | 4 | `server/app.py` imports `integration/vox.py` | The MCP server reaches into install-time code |
| `service` | `claude_code` | 3 | `enablement.py` imports `claude_md.py` and `claude_permissions.py` | Enable is hard-wired to Claude Code; it should hand that work to each plugin |
| `server` | `claude_code` | 2 | `server/launch.py` imports `statusline.py` | Claude Code code inside the shared MCP server |
| `commands` | `cli` | 2 | `commands/session.py` imports `cli_session_store.py` | A command depends on its own caller |
| `wire` | `engine` | 2 | `talk_types.py` imports `tty.py` | A data type depends on engine code |
| `engine` | `claude_code` | 1 | `session_open.py` imports `unread_file.py` | The engine writes the Claude Code status-line file |
| `engine` | `server` | 1 | `session_open.py` imports `server/state.py` | The engine depends on the MCP server |
| `kernel` | `engine` | 1 | `_nats_connection.py` imports `relay.py` | A helper depends on the relay |
| `kernel` | `wire` | 1 | `caller_identity.py` imports `session_routing.py` | A helper depends on a data type |
| `kernel` | `claude_code` | 1 | `user_scope.py` imports `claude_md.py` | A helper depends on Claude Code code |
| `wire` | `render` | 1 | `talk_types.py` imports `_formatting.py` | A data type depends on text formatting |
| `server` | `cli` | 1 | `server/entrypoints.py` imports `_cli_runtime.py` | The MCP server depends on the command line |
| `claude_code` | `server` | 1 | `unread_file.py` imports `server/display_queue.py` | Plugin code reaches into the MCP server's internals |

Two placements in this count are judgment calls and may change at
ratification: `formatting` and `_formatting` form their own `render` package,
and `cli_context` (the context every command receives) belongs to `commands`.
`biff.data` (bundled files, no code) is not assigned to a layer.

## Migration

Lux's order, applied to biff:

1. Ratify this document as a DES entry.
2. Add the layer checker with today's 25 violations as its baseline, so
   nothing new slips in.
3. Move one package per PR. Each PR moves its modules into the package
   directory, fixes the imports it can, and lowers the count. Start with
   `claude_code` and the `commands` → `server` imports, because the Claude Code
   channel work (DES-087) lands there.
4. Create new modules in their target package from the start. The channel
   sender and start-up warning go in `claude_code`; the arrival summaries go in
   `engine`.

Moves carry no compatibility aliases: when a module moves, every caller
changes in the same PR.
