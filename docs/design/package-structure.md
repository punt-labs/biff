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
| `kernel` | Small helpers with no biff dependencies: session ids, locks, chunking, logging setup, usage counters, NATS connection helpers; the bundled data files and the ProB gate script (`prob_gate`), which imports nothing from biff; `supervision`, the service manager that installs, starts and probes biffd and uses only the standard library | nothing in biff |
| `wire` | The data types biff passes around: messages, sessions, talk records, session routing, unread summaries | `kernel` |
| `engine` | The relays (local and NATS), talk state, session registry, identity, configuration, tty names; the arrival summaries and the `DeliverySink` interface they are handed to | `kernel`, `wire` |
| `render` | Text formatting that both the command line and the MCP server print: tables, `finger` output, message lists | `kernel`, `wire`, `engine` |
| `commands` | One command object per biff operation, plus the context each command receives | `kernel`, `wire`, `engine`, `render` |
| `server` | The MCP server and its tools, the same for every AI tool that connects | `kernel`, `wire`, `engine`, `render`, `commands`, `client` |
| `plugins` | The plugin contract (`plugins/contract.py`): the `Installer` and `SessionStart` interfaces, and the registry that holds each tool's adapter | `kernel`, `wire`, `engine` |
| `service` | Install, enable, `biff doctor`, git hooks, the CI notification workflow, optional integrations such as vox | `kernel`, `wire`, `engine`, `render`, `plugins` |
| `plugins/<tool>` | One Python adapter per AI tool: `claude_code`, `codex`, `opencode`, `pi`, `devin`. Each implements the parts of the contract its tool needs, and may own that tool's console script (Claude Code's `biff-hook`) | `kernel`, `wire`, `engine`, `render`, `commands`, `plugins`, `service`; never `server`, `cli` or another tool's adapter |
| `cli` | The `biff` command, the interactive shell, command-line sessions; registers each tool's adapter at startup | every package above |

`testing` (the test helpers in `biff.testing`) is outside this stack. It may
import `kernel`, `wire`, `engine`, `render`, `commands` and `server`; no
production layer may import it.

`server` may import `client` because `biff mcp` becomes a client of biffd
([biffd.md](biffd.md), "Package layout"). The allowance is temporary: the server
still imports `engine` while the in-process server exists, and the last step of
the migration deletes that path and the engine import with it, so that no
client-side package imports `engine`.

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
it to the `DeliverySink`s it was given at startup; each plugin decides how to
show it.

## The plugin contract

Biff itself works the same for every AI coding tool. Each tool differs in
three ways: what has to be installed for it, how a new arrival reaches its
model, and what it shows at session start. `plugins/contract.py` names those
three differences as interfaces, and each tool's Python adapter fills them in.
`Installer` and `SessionStart` are defined there. `DeliverySink` is defined in
`engine`, next to the arrival summary, because the engine is its caller and
may not import `plugins`.

| Interface | What it does | Called by |
|-----------|--------------|-----------|
| `Installer` | Writes, removes and checks what one tool needs in a repo or on the machine: config entries, hook files, instruction files, permission rules, the shipped plugin | `biff enable`, `biff disable`, `biff doctor` (in `service`) |
| `DeliverySink` | Takes an arrival summary from the engine and gets it in front of that tool's model | The engine, when the relay pushes an arrival |
| `SessionStart` | Returns the text a session sees when it starts: orientation, and warnings such as "channels are not live" | The tool's session-start hook |

A registry maps each tool name to its adapter. `cli` fills it at startup and
hands it to `service`, which looks adapters up through it and never imports
one directly. `cli` also passes the registered adapters' `DeliverySink`s to the
engine when it starts the relay, so the engine calls them without importing
`plugins`. Adapters depend on biff and never the reverse.

A tool's own console script is the exception that needs no registry. Claude
Code's hooks run `biff-hook`, whose entry point lives in
`plugins/claude_code/` and starts without `cli`. It already is the Claude Code
adapter, so it calls that adapter's code directly; it builds a registry only if
it ever has to reach another tool's adapter, which a hook for one tool never
does.

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

`biffd`, the planned per-user daemon, is designed in [biffd.md](biffd.md)
(DES-095), including the `daemon` and `client` packages that will join the
table above. Until it lands, three rules keep today's code extractable:

1. **Entry points reach the engine only through `commands`, after startup.**
   `server`, `cli` and each `plugins/<tool>` adapter call command objects, not
   relay or talk-state internals. The one exception is process startup: the
   entry point that starts a process (`cli`, or a tool's own console script)
   reads configuration, constructs the relay and passes it the delivery sinks.
   Measured on `968e4c5`: 41 direct engine imports in 24 entry-point modules,
   counted as a separate, non-gating number.
2. **Arrivals leave the engine only through `DeliverySink`.** `biffd` serves
   that interface to its clients as a subscription, and `biff watch` (PL-235)
   is its command-line form.
3. **`wire` types stay plain data.** They cross the daemon's connection, so
   they hold no relay connections, file handles or callbacks.

The engine also holds no per-tool state; anything one tool needs lives in its
adapter.

## Where new code goes

Ask these in order and stop at the first yes:

1. Does it exist only because of one AI tool? It goes in that tool's adapter,
   `plugins/<tool>/`. If biff itself needs to call it, there are two routes.
   Installing, checking or session-start work: add or extend an interface in
   `plugins/contract.py` and call it through the registry. Delivering an
   arrival: implement `engine`'s `DeliverySink`; `cli` hands it to the engine
   at startup.
2. Does it install, enable, diagnose or uninstall biff, the same for every
   tool? `service`.
3. Is it a biff operation a user or agent can invoke? A command object in
   `commands`, then a thin adapter in `server` and in `cli`.
4. Does it turn biff data into text for a person to read? `render`.
5. Does it hold or move messages, sessions or presence? `engine`.
6. Is it a data type passed between packages? `wire`.
7. Does it depend on nothing else in biff? `kernel`.

## How it is enforced

`oo-ratchet layers src/biff --check` (punt-oo-ratchet 0.5.0) reads every
module's imports and reports each import that breaks the table above. It runs
in `make check` as `make check-layers`, against `.oo-layers-baseline.json`,
which lists the known violations:

- A new violation fails `make check`.
- Fixing a violation and not removing it from the baseline also fails, so the
  baseline only shrinks.
- `make update-layers` rewrites the baseline after a fix and appends a line to
  `.oo-layers-audit.jsonl`. It never adds a violation.
- CI runs `check-layers` beside the other ratchets, against the merge base on
  pull requests and against the previous commit on `main`.

The layers are declared in `pyproject.toml` as `[tool.oo-ratchet.layers.<name>]`
tables. Each has `prefixes` (module names relative to `biff`; the longest match
wins) and `imports` (the layers it may import). The gate fails closed: every
module except the package root must belong to a layer, and every layer must
match a module. Prefixes are exact module names, never globs, so a new module
fails as unassigned until someone places it. Until a package is moved into its
directory its modules are listed by name, so moving one means editing its
prefix in the same PR.

The gate sees imports, not labels, so `tests/test_layer_policy.py` pins the
policy itself:

- `tests/layer_map.json` holds the layer of every module. Moving a module to
  another layer changes which imports are violations without touching any
  code, so it fails the test and is reviewed; regenerate the file in the same
  change.
- Each layer's `imports` must equal the table above.
- Every `plugins/<tool>/` package needs its own layer. The `plugins` prefix
  also matches its submodules, so a new adapter would otherwise land in the
  contract layer; the test fails until the adapter gets a layer of its own.
- Nothing may import the `testing` layer.

Widening a layer's `imports`, or removing a violation by relabeling, is a policy
change. Widening fails `--check` unless the change lands alone with
`--allow-config-change`. In CI, a reviewer adds the pull request label
`layers-policy-change`; the ratchet step then passes that flag. The label is
read from the event payload and never reaches the shell as text. The
main-branch check passes it only for the pull request whose merge commit is the
pushed commit, so the change the pull-request job accepted does not fail again
once it lands; `tools/layers_allowance.py` decides, and a label on any other pull
request does not count. If the pull request cannot be read, the check stays
strict. Locally, `LAYERS_BASE` (and the other `*_BASE` variables)
set in the environment is ignored; pass `LAYERS_BASE=...` on the `make` command
line to use it. `make check-layers` always scans `src/biff`, whatever `SRC` is.

The gate over-approximates on purpose. It counts imports inside functions and
under `TYPE_CHECKING`, `from biff import X` as an import of every module
`biff/__init__.py` takes `X` from, and `importlib.import_module("biff.x")` with
a literal name. Biff's `biff.data` package holds bundled files and one
docstring-only `__init__.py`; it sits in `kernel`, which imports nothing.

This is the same ratchet Lux uses (`lux/tools/layer_contract.py`,
`.layer-baseline.json`), now shipped in the shared `punt-oo-ratchet` package.
The advisory `design` gate reads the same layer tables, so there is one layer
configuration.

## Where biff stands today

Measured on `main` at `a924a7b`, 2026-11-22: 121 modules, 19 imports that
break the table (`.oo-layers-baseline.json`). On `968e4c5` the same table,
before the Claude Code modules moved and the plugin registry landed, counted
25. Most modules still sit flat in `src/biff/`; the package names
above describe where each one is headed, not today's directories.

| From | To | Count | Example | Why it matters |
|------|----|-------|---------|----------------|
| `commands` | `server` | 4 | `commands/finger.py` imports `server/tools/_session.py` | Commands are shared by every adapter; one must not depend on the MCP server |
| `commands` | `cli` | 2 | `commands/session.py` imports `cli_session_store.py` | A command depends on its own caller |
| `server` | `service` | 2 | `server/app.py` imports `integration/vox.py` | The MCP server reaches into install-time code |
| `server` | `claude_code` | 2 | `server/launch.py` imports `statusline.py` | Claude Code code inside the shared MCP server |
| `wire` | `engine` | 2 | `talk_types.py` imports `tty.py` | A data type depends on engine code |
| `engine` | `claude_code` | 1 | `session_open.py` imports `unread_file.py` | The engine writes the Claude Code status-line file |
| `kernel` | `engine` | 1 | `_nats_connection.py` imports `relay.py` | A helper depends on the relay |
| `kernel` | `wire` | 1 | `caller_identity.py` imports `session_routing.py` | A helper depends on a data type |
| `kernel` | `claude_code` | 1 | `user_scope.py` imports `claude_md.py` | A helper depends on Claude Code code |
| `wire` | `render` | 1 | `talk_types.py` imports `_formatting.py` | A data type depends on text formatting |
| `server` | `cli` | 1 | `server/entrypoints.py` imports `_cli_runtime.py` | The MCP server depends on the command line |
| `claude_code` | `server` | 1 | `unread_file.py` imports `server/display_queue.py` | Plugin code reaches into the MCP server's internals |

In this table `claude_code` means `plugins/claude_code/`.

Two placements in this count were settled by DES-088: `formatting` and
`_formatting` form their own `render` package, and `cli_context` (the context
every command receives) belongs to `commands`.
`biff.data` (bundled files, no code) is assigned to `kernel`.

One tool-specific leak the count does not show: `session_lock.py` names an
`opencode-` lock file, so opencode-specific code already sits in a module the
standard places in `kernel`. It moves to `plugins/opencode/` or becomes
tool-neutral.

## Migration

Lux's order, applied to biff:

1. Ratify this document as a DES entry.
2. Add the layer checker with today's violations as its baseline, so
   nothing new slips in. Done: `make check-layers`.
3. Move the Claude Code modules into `plugins/claude_code/`. This is a
   behavior-neutral move; it removes no imports by itself. Done (#589).
4. Add `plugins/contract.py` together with the Claude Code adapter and its
   callers: `biff enable` and `biff doctor` go through `Installer`, and the
   registry is built by `cli`. The contract lands with callers, never as an
   unused interface. Shared result types are not duplicated: the contract
   reuses one definition each for install outcomes and doctor checks, and the
   arrival summary type lives in `engine` so the engine can produce it. This
   removes the three `service` → Claude Code imports; the engine and server
   imports (status-line file, launch) follow as `DeliverySink` and
   `SessionStart` gain their callers. The contract and the `enable`/`doctor`
   routing are done (#590); the engine and server imports remain.
5. Move one package per PR after that. Each PR moves its modules into the
   package directory, fixes the imports it can, and lowers the count. Fix the
   `commands` → `server` imports early, because the channel work touches them.
6. Create new modules in their target package from the start. The channel
   sender and start-up warning go in `plugins/claude_code/`; the arrival
   summaries go in `engine`; `biff watch` goes in `cli`.
7. Build the codex adapter next: it is the second adapter with real Python
   code, and it settles the contract's signatures.

The repo-root `plugin/` directory (the shipped Claude Code plugin) does not
move; this migration only reorganizes `src/biff/`.

Moves carry no compatibility aliases: when a module moves, every caller
changes in the same PR.
