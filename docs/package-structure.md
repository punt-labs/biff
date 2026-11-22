# Biff Package Structure

Status: ratified 2026-11-22 (DES-088).

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
| `plugins` | The plugin contract (`plugins/contract.py`): the interfaces every AI tool's adapter implements, and the registry that holds them | `kernel`, `wire`, `engine` |
| `service` | Install, enable, `biff doctor`, git hooks, the CI notification workflow, optional integrations such as vox | `kernel`, `wire`, `engine`, `render`, `plugins` |
| `plugins/<tool>` | One Python adapter per AI tool: `claude_code`, `codex`, `opencode`, `pi`, `devin`. Each implements the parts of the contract its tool needs | the above, plus `commands`, `service`; never another tool's adapter |
| `cli` | The `biff` command, the interactive shell, command-line sessions; registers each tool's adapter at startup | everything above |

`cli` sits on top because it starts every other piece, including `biff mcp`,
and it is the one place that knows which adapters exist.

### Python adapters versus shipped plugins

Two different things carry a tool's name, and this standard governs only the
first:

| | Python adapter | Shipped plugin |
|--|----------------|----------------|
| Where | `src/biff/plugins/<tool>/`, inside the `punt-biff` Python package | A directory at the repo root, e.g. `plugin/` for Claude Code today, `devin-plugin/` later; or a separate package (the opencode and pi plugins are TypeScript) |
| What | Python code biff runs for that tool: installing its files, delivering arrivals to it, its session-start text | What the tool loads: manifest, hook scripts, slash commands, extension code |
| Ships through | PyPI (`punt-biff`) | The tool's own marketplace or package registry |

A shipped plugin calls biff through its public entry points (the `biff`
command, `biff mcp`, `biff watch`) and never imports a Python adapter. A
Python adapter may install or check a shipped plugin, but never ships inside
it.

### Why biff needs Python adapters

Lux has no package for one AI tool; it treats its Claude Code plugin as a way
to install the MCP server. Biff needs adapters because about a tenth of its
code (2,647 of 24,827 lines) sits in modules that exist only for Claude Code:
the hook handlers, the status line, the unread file the status line reads, and
the CLAUDE.md and permission files biff installs. More is mixed into the MCP
server's tool-description module, and the Claude Code channel work (DES-087)
adds more. codex will need the same kind of code: biff writes codex's config
and runs `codex queue` to deliver arrivals. Building the other tools' support
requires the contract below, so it is designed now from Claude Code's code and
the per-tool findings in `research/harness-compatibility.md`.

### What stays out of the engine

The engine never knows which AI tool is on the other end. In particular it
never writes the status-line file, never builds Claude Code tool descriptions,
and never edits CLAUDE.md or permission settings. When the engine has news (a
message arrived, a wall was posted), it produces an arrival summary and hands
it to the registered `DeliverySink`s; each plugin decides how to show it.

## The plugin contract

Biff itself works the same for every AI coding tool. Each tool differs in
three ways: what has to be installed for it, how a new arrival reaches its
model, and what it shows at session start. `plugins/contract.py` names those
three differences as interfaces, and each tool's Python adapter fills them in.

| Interface | What it does | Called by |
|-----------|--------------|-----------|
| `Installer` | Writes, removes and checks what one tool needs in a repo or on the machine: config entries, hook files, instruction files, permission rules, the shipped plugin | `biff enable`, `biff disable`, `biff doctor` (in `service`) |
| `DeliverySink` | Takes an arrival summary from the engine and gets it in front of that tool's model | The engine, when the relay pushes an arrival |
| `SessionStart` | Returns the text a session sees when it starts: orientation, and warnings such as "channels are not live" | The tool's session-start hook |

A registry maps each tool name to its adapter. `cli` fills it at startup;
`service` and the engine look adapters up through it and never import one
directly, so adapters depend on biff and never the reverse.

What each adapter implements, from Claude Code's code today and
`research/harness-compatibility.md`:

| Tool | `Installer` | `DeliverySink` | `SessionStart` |
|------|-------------|----------------|----------------|
| Claude Code | Hooks, CLAUDE.md, permission rules, status line | Channel event (DES-087); `/biff:read Xm` polling where channels are off | Session-start hook context, including the channel warning |
| codex | `~/.codex/config.toml` MCP entry, `~/.codex/hooks.json`, prompt files, an AGENTS.md fragment | Runs `codex queue` (or `thread/queue/add`) for the session's thread | codex's hook engine accepts Claude Code's hook output |
| opencode | The `mcp` entry and plugin reference in `opencode.json` | Its TypeScript plugin reads `biff watch` and calls `client.session.prompt` | Its TypeScript plugin |
| pi | Checks the `@punt-labs/biff-pi` extension is installed | Its TypeScript extension reads `biff watch` and calls `pi.sendMessage` | Its TypeScript extension |
| Devin | The `biff mcp --user` entry (DES-081) | To be designed | To be designed |

Two pieces follow:

- **`biff watch`** (PL-235), a `cli` command that prints one JSON line per
  arrival summary. It is how every shipped plugin running outside biff's process
  (opencode, pi) receives arrivals, so none of them reads biff's internal
  files.
- **An adapter is only as big as its tool needs.** The opencode and pi
  adapters are small, because their tools' real code lives in their
  TypeScript plugins. The Claude Code and codex adapters hold real code.

The interfaces' exact method signatures are settled by building the first two
adapters, Claude Code and codex, against them; a third adapter must fit
without changing the contract.

## Room for biffd

`biffd` is a planned per-user daemon that will own the NATS connection, inbox
and unread state, presence, plans, talk state and the fan-out of arrivals.
The MCP server, the `biff` command, the hooks and out-of-process plugins then
become thin clients that reach it over local IPC (PL-225,
`docs/requirements-biffd-multihost.md`, requirements R-D.1 to R-D.8). This
standard does not design `biffd`. It keeps today's code shaped so that
extracting `biffd` moves the engine into a new process instead of rewriting
the code around it:

1. **Entry points reach the engine only through `commands` and the plugin
   contract.** `server`, `cli` and each `plugins/<tool>` adapter call command
   objects and registry interfaces, not relay or talk-state internals. Under
   `biffd` those calls become IPC requests; the callers do not change.
   Measured on `968e4c5`: 41 direct engine imports in 24 entry-point modules,
   counted as a separate, non-gating number until `biffd`'s design decides
   which of them, such as reading configuration at startup, stay.
2. **Arrivals leave the engine only through `DeliverySink`.** That is the
   notification subscription `biffd` will serve to its clients (R-D.5), and
   `biff watch` (PL-235) is its command-line form.
3. **`wire` types stay plain data.** They will cross the IPC boundary, so they
   hold no relay connections, file handles or callbacks.
4. **The engine holds no per-tool state.** Message semantics and delivery
   policy are the same for every tool (R-D.4); anything one tool needs lives
   in its adapter.

Left to `biffd`'s own design: the `daemon` and `client` packages, the IPC
protocol, how the daemon starts and stops, and how several sessions share
it. When that design lands, those packages join the table above, and `server`
and `cli` may import `client` instead of `engine`.

## Where new code goes

Ask these in order and stop at the first yes:

1. Does it exist only because of one AI tool? It goes in that tool's adapter,
   `plugins/<tool>/`. If biff itself needs to call it, add or extend an
   interface in `plugins/contract.py` and call it through the registry.
2. Does it install, enable, diagnose or uninstall biff, the same for every
   tool? `service`.
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

`punt-oo-ratchet` 0.4.0 already checks layers in its advisory `design` gate,
which reads `[tool.oo-ratchet.layers]` in `pyproject.toml`. Biff's current
config there sorts modules into four fixed layers (`entry`, `adapters`,
`application`, `domain`) and never fails a build. The gating checker extends
that same layer code to accept this standard's package names and import table;
when it ships, biff's `[tool.oo-ratchet.layers]` is rewritten to the packages
above and becomes the single layer configuration.

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

In this table `claude_code` means `plugins/claude_code/`.

Two placements in this count are judgment calls and may change at
ratification: `formatting` and `_formatting` form their own `render` package,
and `cli_context` (the context every command receives) belongs to `commands`.
`biff.data` (bundled files, no code) is not assigned to a layer.

One tool-specific leak the count does not show: `session_lock.py` names an
`opencode-` lock file, so opencode-specific code already sits in a module the
standard places in `kernel`. It moves to `plugins/opencode/` or becomes
tool-neutral.

## Migration

Lux's order, applied to biff:

1. Ratify this document as a DES entry.
2. Add the layer checker with today's 25 violations as its baseline, so
   nothing new slips in.
3. Add `plugins/contract.py` and the registry, and move the Claude Code
   modules into `plugins/claude_code/` behind it. This removes the five
   Claude Code imports from the engine and server and the three from
   `service`, and it is the contract the other adapters build on.
4. Move one package per PR after that. Each PR moves its modules into the
   package directory, fixes the imports it can, and lowers the count. Fix the
   `commands` → `server` imports early, because the channel work touches them.
5. Create new modules in their target package from the start. The channel
   sender and start-up warning go in `plugins/claude_code/`; the arrival
   summaries go in `engine`; `biff watch` goes in `cli`.
6. Build the codex adapter next: it is the second adapter with real Python
   code, and it settles the contract's signatures.

The repo-root `plugin/` directory (the shipped Claude Code plugin) does not
move; this migration only reorganizes `src/biff/`.

Moves carry no compatibility aliases: when a module moves, every caller
changes in the same PR.
