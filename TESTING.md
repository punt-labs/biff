# Testing

Biff has a five-tier testing pyramid. Each tier tests a different
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
        ┌──┤  │  │  │  Tier 2   │  Integration — FastMCPTransport
        │  │  │  │  │  84 tests │  ~2s total
        │  │  │  │  ├───────────┤
        │  │  │  │  │  Tier 1   │  Unit — direct function calls
        │  │  │  │  │ 870 tests │  ~1s total
        └──┴──┴──┴──┴───────────┘
```

Total: 1,094 tests collected (140 deselected in default run).
Default `uv run pytest` runs tiers 1-2 only: 954 tests in ~5s.

## Tiers

### Tier 1: Unit tests

Direct function calls. No transport, no server, no relay.

| Directory | Tests | What it covers |
|-----------|-------|----------------|
| `tests/test_main.py` | 39 | CLI arg parsing, flag handling, command wiring, no-args help |
| `tests/test_commands/` | 75 | Command logic via `LocalRelay` — formatting, edge cases, multi-user |
| `tests/test_server/` | 258 | MCP tool handlers, storage, data models |
| `tests/test_config.py` | — | Config loading, git root detection, identity resolution |
| `tests/test_statusline.py` | 103 | Status bar rendering, formatting |
| `tests/test_relay.py` | — | `LocalRelay` operations |
| `tests/test_tty.py` | 13 | TTY name generation, hostname detection |
| Other unit files | — | Models, formatting, hooks, dormant relay, doctor, etc. |

**Mock strategy varies by what's under test:**

- `test_main.py` mocks `cli_relay` (no NATS) and `commands.*` functions
  (no relay) to test CLI wiring in isolation. Verifies Typer parsed
  args correctly and forwarded them to the right command function.

- `test_commands/` uses `LocalRelay` backed by `tmp_path` — no mocking,
  no NATS. Tests command logic (formatting, multi-user state, edge
  cases) against real filesystem state.

- `test_server/` calls MCP tool handlers directly with constructed
  `ServerState`. Tests the tool layer without transport.

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

### Tier 3a: Subprocess tests

Spawns real `biff serve --transport stdio` subprocesses connected via
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

**Transport**: Claude Agent SDK → `biff serve --transport stdio` subprocess.
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
| Subprocess | `RecordingClient` | `await kai.call("who")` |
| NATS E2E | `RecordingClient` | `await kai.call("who")` |
| Hosted NATS | `RecordingClient` | `await kai.call("who")` |
| SDK | `SDKClient` | `await kai.prompt('Call the "who" tool.')` |

`RecordingClient` wraps a FastMCP `Client` with transcript capture.
`SDKClient` wraps the Claude Agent SDK `query()` with structured result
parsing. Both record tool interactions into a shared `Transcript`.

Tests marked `@pytest.mark.transcript` auto-save human-readable
transcripts to `tests/transcripts/`.

## Running tests

```bash
# Default: tiers 1-2 (fast, no external dependencies)
uv run pytest

# Subprocess (tier 3a)
uv run pytest -m subprocess

# Local NATS (tier 3b, requires nats-server)
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

Coverage from tiers 1-2 combined (as of v0.15.0):

| Module | Cover | Notes |
|--------|-------|-------|
| `commands/*` (10 modules) | 100% | All command logic fully exercised |
| `commands/write.py` | 92% | 2 lines uncovered |
| `__main__.py` | 61% | Admin commands (install/doctor/uninstall/talk/hook/statusline) untested |
| `cli_session.py` | 70% | NATS relay path only tested at tier 3b+ |
| **CLI total** | **76%** | |

The `__main__.py` gap is admin commands that touch the filesystem,
plugin system, or start long-running processes. Product command
coverage is complete.

### Measuring coverage

```bash
# pytest-cov conflicts with beartype import hooks. Use coverage directly:
COVERAGE_CORE=sysmon uv run coverage run --source=biff -m pytest -q
uv run coverage report -m --include="*/__main__.py,*/commands/*.py"
```

## Known issues

Three `test_config.py` tests fail locally because `TMPDIR` (set via
`.envrc`) points to `.tmp/` inside the biff repo. `find_git_root`
walks up from `tmp_path` and finds biff's own `.git` instead of
returning `None`. These pass in CI where `TMPDIR` is `/tmp`.
