# Testing

Biff has a six-tier testing pyramid. Each tier tests a different
boundary, at a different speed, with a different transport.

## Pyramid

```text
                    ┌───────────┐
                    │  Tier 4   │  SDK — Claude picks the tools
                    │   3 tests │  ~30s each, ~$0.02/call
                    ├───────────┤
                 ┌──┤  Tier 3c  │  Hosted NATS — Synadia Cloud
                 │  │  33 tests │  ~10s total, manual-only
                 │  ├───────────┤
              ┌──┤  │  Tier 3b  │  Local NATS — nats-server
              │  │  │  49 tests │  ~3s total
              │  │  ├───────────┤
           ┌──┤  │  │  Tier 3a  │  Subprocess — stdio pipes
           │  │  │  │  22 tests │  ~5s total
           │  │  │  ├───────────┤
        ┌──┤  │  │  │  Tier 2b  │  CLI multi-user — cli_session + NATS
        │  │  │  │  │  (planned)│  ~3s total
        │  │  │  │  ├───────────┤
        │  │  │  │  │  Tier 2   │  Integration — FastMCPTransport
        │  │  │  │  │  84 tests │  ~2s total
        │  │  │  │  ├───────────┤
        │  │  │  │  │  Tier 1   │  Unit — direct function calls
        │  │  │  │  │  935 tests│  ~3s total
        └──┴──┴──┴──┴───────────┘
```

Total: 1,215 tests collected (140 deselected in default run).
Default `uv run pytest` runs tiers 1-2 only: 1,075 tests in ~5s.

## Tiers

### Tier 1: Unit tests

Direct function calls. No transport, no server, no relay.

| Directory | Tests | What it covers |
|-----------|-------|----------------|
| `tests/test_main.py` | 39 | CLI arg parsing, flag handling, command wiring, REPL launch |
| `tests/test_commands/` | 75 | Command logic via `LocalRelay` — formatting, edge cases, multi-user |
| `tests/test_server/` | 258 | MCP tool handlers, storage, data models |
| `tests/test_dispatch.py` | 31 | REPL command dispatcher — all 10 commands + arg validation |
| `tests/test_repl_talk.py` | 37 | Talk subsystem — drain, handshake, accept, publish (Z spec coverage) |
| `tests/test_repl_loop.py` | 18 | REPL loop — prompt gate, dispatch, mode transitions, sync |
| `tests/test_repl_notify.py` | 22 | NotifyState — unread count, wall change, sync, boundaries |
| `tests/test_repl_readline.py` | 5 | Readline — completer, history |
| `tests/test_cli_session.py` | 10 | Session lifecycle — heartbeat, registration, wtmp, cleanup |
| `tests/test_config.py` | — | Config loading, git root detection, identity resolution |
| `tests/test_statusline.py` | 103 | Status bar rendering, formatting |
| `tests/test_relay.py` | — | `LocalRelay` operations |
| `tests/test_tty.py` | 13 | TTY name generation, hostname detection |
| Other unit files | — | Models, formatting, hooks, dormant relay, doctor, etc. |

**Mock strategy varies by what's under test:**

- `test_main.py` mocks `cli_session` (no NATS) and `commands.*` functions
  (no relay) to test CLI wiring in isolation. Verifies Typer parsed
  args correctly and forwarded them to the right command function.

- `test_commands/` uses `LocalRelay` backed by `tmp_path` — no mocking,
  no NATS. Tests command logic (formatting, multi-user state, edge
  cases) against real filesystem state.

- `test_server/` calls MCP tool handlers directly with constructed
  `ServerState`. Tests the tool layer without transport.

- `test_repl_loop.py` mocks `dispatch` and `_handle_repl_talk`, feeds
  lines via asyncio queue, and verifies prompt gate state and loop
  termination. No stdin thread, no NATS.

- `test_repl_talk.py` uses pre-loaded asyncio queues to test drain
  functions, handshake detection, and accept checking. Tests derived
  from Z specification partition analysis (docs/talk.tex).

- `test_cli_session.py` mocks `NatsRelay` to test session lifecycle
  (registration, wtmp events, heartbeat, cleanup on failure).

### Tier 2: Integration tests

Full MCP server connected via `FastMCPTransport` (in-memory). Two users
(kai, eric) share a `tmp_path` data directory. Tests MCP protocol
compliance, tool discovery, cross-user state interactions.

```text
tests/test_integration/
├── conftest.py              # kai/eric fixtures via FastMCPTransport
├── test_e2e_presence.py     # Presence: who, finger, plan across users
├── test_last.py             # Session history
├── test_protocol.py         # MCP protocol: tool listing, error handling
├── test_talk.py             # Real-time talk sessions
├── test_tty_sessions.py     # Multi-session TTY management
├── test_tty.py              # TTY naming
├── test_wall.py             # Broadcast: post, read, clear, duration
└── test_workflows.py        # Multi-step workflows across commands
```

**Transport**: `FastMCPTransport` — in-process, no serialization overhead.
Same MCP protocol as production but without stdio or HTTP.

### Tier 2b: CLI multi-user tests (planned — biff-s8d)

Two `cli_session()` instances sharing a local NATS server. Tests
multi-user scenarios using `biff.commands` pure async functions —
the same code path as the interactive REPL, but without stdin threads
or display concerns.

```python
async with cli_session(user="kai") as kai:
    async with cli_session(user="eric") as eric:
        await commands.write(kai, "@eric", "review the PR")
        result = await commands.read(eric)
        assert "review the PR" in result.text
```

This tier fills the gap between tier 2 (LocalRelay, no NATS) and
tier 3b (full MCP server over NATS). It exercises real NATS paths
(JetStream messaging, KV presence, talk notifications) at a fraction
of the complexity of MCP E2E tests.

**Scenarios**: presence, messaging, wall broadcasts, plan visibility,
talk handshake, session cleanup, wtmp history, mesg off.

**Transport**: `cli_session()` → `NatsRelay` → local `nats-server`.
No MCP protocol, no subprocess overhead.

```bash
uv run pytest -m nats  # Shares the local NATS marker
```

### Tier 3a: Subprocess tests

Spawns real `biff mcp` subprocesses connected via
`StdioTransport`. Tests wire protocol, CLI argument parsing, process
lifecycle, and graceful shutdown.

```bash
uv run pytest -m subprocess
```

**Transport**: `StdioTransport` — real process, real stdio pipes. Uses
`--relay-url ""` to force `LocalRelay` (no NATS dependency).

### Tier 3b: Local NATS E2E

Full MCP servers backed by `NatsRelay`, connected via
`FastMCPTransport`. Requires a local `nats-server` binary.

```bash
uv run pytest -m nats
```

Tests presence propagation, messaging, wall broadcasts, talk sessions,
KV watch survival, and notification latency — all over real NATS
JetStream and KV.

**Cleanup**: An autouse fixture deletes NATS streams after each test
for full isolation.

### Tier 3c: Hosted NATS E2E

Same as 3b but against Synadia Cloud (or any hosted NATS server).
Manual-only — not in CI because session-scoped NATS connections hang in
GitHub Actions' asyncio environment.

```bash
BIFF_TEST_NATS_URL=tls://connect.ngs.global \
BIFF_TEST_NATS_CREDS=src/biff/data/demo.creds \
uv run pytest -m hosted -v
```

**Connection budget**: Hosted accounts have low connection limits (e.g. 5
on Synadia starter). Fixtures use session-scoped relays — two NATS
connections total, reused across all tests.

**Cleanup**: Purges KV keys and stream messages but keeps infrastructure
intact. Avoids propagation delays from rapid create/delete cycles on
hosted servers.

Run locally before merging any relay code changes.

### Tier 4: SDK tests

Drives real Claude Code sessions via the Claude Agent SDK. Claude
discovers biff's MCP tools, decides which to call, and results flow
back through the full stack.

```bash
uv run pytest -m sdk
```

**Transport**: Claude Agent SDK → `biff mcp` subprocess.
Claude is the caller — tests validate that tool descriptions are clear
enough for the model to use correctly.

**Cost**: ~$0.02 per test, ~30s per test. Requires `ANTHROPIC_API_KEY`.

## Fixture model

Every tier above unit provides `kai` and `eric` fixtures — two users
sharing state through whatever transport that tier exercises.

| Tier | Fixture type | Key method |
|------|-------------|------------|
| Commands | `CliContext` + `LocalRelay` | `await commands.who(ctx)` |
| Integration | `RecordingClient` | `await kai.call("who")` |
| CLI multi-user | `CliContext` + `NatsRelay` | `await commands.who(kai)` |
| Subprocess | `RecordingClient` | `await kai.call("who")` |
| NATS E2E | `RecordingClient` | `await kai.call("who")` |
| Hosted NATS | `RecordingClient` | `await kai.call("who")` |
| SDK | `SDKClient` | `await kai.prompt('Call the "who" tool.')` |

`RecordingClient` wraps a FastMCP `Client` with transcript capture.
`SDKClient` wraps the Claude Agent SDK `query()` with structured result
parsing. Both record tool interactions into a shared `Transcript`.

Tests marked `@pytest.mark.transcript` auto-save human-readable
transcripts to `tests/transcripts/`.

## Z Specifications

Two formal Z specifications drive test generation via TTF partition
analysis:

| Spec | Entities | Partitions | Unit Coverage |
|------|----------|-----------|---------------|
| `docs/talk.tex` | Talk handshake, conversation, hangup | 55 | 91% (50/55) |
| `docs/repl.tex` | Session lifecycle, dispatch, prompt gate, notifications | 52 | 81% (42/52) |

Both specs are type-checked with `fuzz` and model-checked with
`probcli` (no counter-examples, no deadlocks). Use `make fuzz`
and `make prob` to verify.

Remaining uncovered partitions are integration-level (require full
REPL loop + NATS subscription running together). Tier 2b will
address these.

## Running tests

```bash
# Default: tiers 1-2 (fast, no external dependencies)
uv run pytest

# Subprocess (tier 3a)
uv run pytest -m subprocess

# Local NATS (tier 3b + future 2b, requires nats-server)
uv run pytest -m nats

# Hosted NATS (tier 3c, local only)
BIFF_TEST_NATS_URL=tls://connect.ngs.global \
BIFF_TEST_NATS_CREDS=src/biff/data/demo.creds \
uv run pytest -m hosted -v

# SDK (tier 4, requires ANTHROPIC_API_KEY)
uv run pytest -m sdk

# Everything local
uv run pytest -m "not hosted and not sdk"
```

## CI

GitHub Actions runs **Lint** and **Tests** (tiers 1-2) on every
push and PR. The **Hosted NATS E2E** workflow is manual-only
(`workflow_dispatch`) because session-scoped NATS connections hang in
GitHub Actions' asyncio environment.

## Coverage

Line coverage from tiers 1-2 (as of biff-1di):

| Area | Cover | Notes |
|------|-------|-------|
| `commands/*` (10 modules) | 100% | All command logic fully exercised |
| Models, formatting, tty | 97-100% | Core data types |
| CLI session | 88% | Lifecycle, heartbeat, cleanup |
| Config | 97% | Identity resolution |
| Relay (LocalRelay) | 96% | Filesystem operations |
| Statusline | 96% | Rendering |
| REPL notify | 97% | NotifyState check/sync |
| Dispatch | 77% | All commands, some arg paths |
| CLI entry (`__main__`) | 50% | Interactive REPL/talk loops |
| MCP server (`app.py`) | 57% | Lifespan, KV watchers |
| NATS relay | 25% | Requires live NATS (tier 3b+) |
| Hook system | 71% | Claude Code hook dispatchers |
| **Overall** | **74%** | |

The 74% floor is `nats_relay.py` (25%) and `__main__.py` (50%).
NATS relay requires a live NATS server (tier 3b). `__main__.py`
contains interactive REPL/talk loops that can't be fully driven
from unit tests. Tier 2b (CLI multi-user) will lift NATS relay
coverage significantly.

### Measuring coverage

```bash
# pytest-cov conflicts with beartype import hooks. Use coverage directly:
COVERAGE_CORE=sysmon uv run coverage run --source=biff -m pytest -q
uv run coverage report -m --omit="*/testing/*,*/data/*"
```
